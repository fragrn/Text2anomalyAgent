M11：Reflection 与多轮 Action 重新生成

## Summary

新增确定性 Reflection 模块，并将其接入 `ChainAgent` 的多轮生成流程：

```text
ActionPlan
→ Validation
→ Safety
→ Attempt 执行结果
→ Evidence / Node / Edge 结果
→ Reflection
→ 重新生成完整 ActionPlan
→ 再次 Validation / Safety
```

不新增 LLM Reflection 调用；Reflection 由规则确定性生成，下一轮 ActionPlan 仍由现有 `ActionGenerator` 生成。

## 主要修改

### 1. Reflection 数据模型

新增 `models/reflection.py`：

- `ReflectionContext`
  - `target`
  - `previous_actions`
  - `action_results`
  - `metrics_summary`
  - `node_results`
  - `edge_results`
  - `failed_node`
  - `failed_edge`
  - `environment`
  - `remaining_budget`

- `ReflectionResult`
  - `diagnosis`
  - `recommended_changes`
  - `keep_successful_actions`
  - `retryable`
  - `attempt`
  - `failed_node`
  - `failed_edge`

`recommended_changes` 使用结构化字典表达，例如：

```json
{
  "duration_sec": 10,
  "concurrency": 4,
  "holder_lock_seconds": 10,
  "reason": "current lock holding time is shorter than observation window"
}
```

Reflection 只提出调整建议，不直接修改 Action 或 SQL。

### 2. 确定性 Reflector

新增 `agent/reflector.py`：

```python
class Reflector:
    def reflect(self, context: ReflectionContext) -> ReflectionResult:
        ...
```

规则按以下优先级执行：

1. Action 执行失败：
   - 诊断为 `ACTION_EXECUTION_FAILED`；
   - 不判断为 Evidence 未命中；
   - 建议重新生成 Action；
   - 保留已经成功的 Action。

2. Evidence 未命中且并发不足：
   - 建议提高 `concurrency`、`terminals` 或 workload rate；
   - 不无条件翻倍，按剩余预算和安全上限递增。

3. 锁冲突未命中且 holder 时间不足：
   - 建议增加 holder 持锁时间；
   - 同时确保 waiter timeout 覆盖观察窗口。

4. 慢查询未命中且 SQL duration 不足：
   - 建议增加 SQL 执行时长；
   - 不超过剩余 injection window。

5. Background workload 过轻：
   - 建议提高 terminals 或 rate；
   - 只调整导致失败的参数。

6. Action duration 超过 injection window：
   - 建议将 duration 调整到可用窗口内；
   - 不修改 SQL 内容。

如果没有足够证据定位原因，返回通用诊断，并标记为可重试但不自动扩大所有参数。

### 3. ChainAgent 多轮流程

在 `ChainAgent` 中新增独立多轮入口，保留现有 M10 单轮接口不变：

```python
def reproduce_chain_with_reflection(
    self,
    request: ChainReproductionRequest,
    *,
    attempt_runner: AttemptRunner,
    validator: ActionValidator,
    safety_checker: SafetyChecker,
    max_retry_rounds: int,
    artifact_store: ArtifactStore | None = None,
    experiment: str | Path | None = None,
) -> ReflectionRunResult:
    ...
```

流程：

1. 使用当前 request 生成完整 ActionPlan；
2. 每轮都执行 Pydantic、ActionValidator 和 SafetyChecker；
3. 通过 `attempt_runner` 获取 Action、Metrics、Node、Edge 结果；
4. 成功则停止；
5. 失败则创建 `ReflectionContext`；
6. 生成 `ReflectionResult`；
7. 将 Reflection 放入下一轮 `previous_reflection`；
8. 重新生成完整 ActionPlan；
9. 达到 `max_retry_rounds` 后停止并返回最终失败结果。

禁止：

- Runtime 直接修改 SQL；
- Runtime 直接修改 Action 字段；
- 只局部修补上一次 Action；
- 跳过下一轮 Validation 或 Safety；
- 超过配置的 retry 上限。

新增 `ReflectionRunResult`，记录：

- `success`
- `attempts`
- `final_action_plan`
- `attempt_results`
- `reflections`
- `final_failed_node`
- `final_failed_edge`
- `message`

### 4. Artifact 记录

复用现有 `ArtifactStore`，每轮记录：

```text
attempt_001/
├── action_plan.json
├── action_result.json
├── evaluation.json
└── reflection.json

attempt_002/
├── action_plan.json
├── action_result.json
├── evaluation.json
└── reflection.json
```

至少保证可以还原：

```text
Attempt 1
→ failed node / edge
→ Reflection diagnosis
→ recommended changes
→ Attempt 2 ActionPlan
→ Attempt 2 result
```

不新增第二套文件保存逻辑。

## 测试与验收

新增 `test/test_m11.py`，全部使用 Fake ActionGenerator、Fake AttemptRunner 和合成结果，不调用真实 LLM、MySQL、BenchBase 或 ChaosBlade。

覆盖：

- Action 执行失败与 Evidence 未命中能够区分；
- terminals 太低时只建议增加 workload 并发相关参数；
- holder 时间太短时只建议增加持锁时间；
- SQL 运行 2 秒未达到慢查询条件时建议增加 duration；
- workload 过轻时建议提高 terminals/rate；
- duration 超过 injection window 时建议缩短 duration；
- 不会把所有参数无条件翻倍；
- Attempt 2 生成的是完整新 ActionPlan；
- Attempt 2 仍经过 Pydantic、ActionValidator 和 SafetyChecker；
- 成功后停止重试；
- 超过 `max_retry_rounds` 后停止；
- Artifact 中包含每轮 Action、结果和 Reflection；
- `keep_successful_actions` 正确保留已命中的 Action；
- `failed_node` 和 `failed_edge` 正确进入 Reflection。

验收标准：

```text
Attempt 1 failed
→ Reflection 给出针对性诊断
→ Attempt 2 生成新 ActionPlan
→ Attempt 2 再次 Validation/Safety
→ 成功或达到 retry 上限
```

且任何情况下都不会由 Runtime 静默修改 LLM 生成的 SQL 或 Action。
