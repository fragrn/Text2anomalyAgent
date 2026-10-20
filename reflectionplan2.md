# M11：隔离式 Reflection 与多轮 Action 重新生成设计

## 1. 设计目标

M11 为 Chain Agent 和 Post Agent 提供多轮失败反馈能力，但两个 Agent 的 Reflection 策略必须完全隔离：

```text
ChainAgent
    ↓
ChainReflector
    ↓
Chain ActionGenerator

PostAgent
    ↓
PostReflector
    ↓
Post ActionGenerator
```

两类 Reflection 只共享数据协议、Artifact 存储和重试基础设施，不共享具体诊断规则，不互相调用，也不互相修改对方的 Reflection 数据。

## 2. 文件结构

```text
db_repro_agent/
├── models/
│   └── reflection.py
├── agent/
│   ├── reflection_protocol.py
│   ├── chain_reflector.py
│   ├── post_reflector.py
│   ├── chain_agent.py
│   └── post_agent.py
└── runtime/
    └── artifact_store.py
```

不使用一个包含两套业务逻辑的通用 `reflector.py`，避免 Chain 和 Post 的规则逐渐耦合。

## 3. 共享 Reflection 协议

`models/reflection.py` 只保存跨 Agent 通用的数据结构，不实现业务判断。

### ReflectionContext

```python
class ReflectionContext(BaseModel):
    source: Literal["chain", "post"]
    target: str
    previous_actions: list[dict[str, Any]]
    action_results: list[dict[str, Any]]
    metrics_summary: dict[str, Any]
    node_results: dict[str, Any]
    edge_results: list[dict[str, Any]]
    failed_node: str | None
    failed_edge: str | None
    environment: dict[str, Any]
    remaining_budget: float
```

### ReflectionResult

```python
class ReflectionResult(BaseModel):
    source: Literal["chain", "post"]
    attempt: int
    diagnosis: str
    recommended_changes: dict[str, Any]
    keep_successful_actions: list[str]
    retryable: bool
    failed_node: str | None
    failed_edge: str | None
```

共享模型只负责字段、类型和来源标识校验，不判断失败原因，也不修改 Action。

## 4. Chain Reflection

新增 `agent/chain_reflector.py`：

```python
class ChainReflector:
    def reflect(self, context: ReflectionContext) -> ReflectionResult:
        ...
```

`ChainReflector` 只处理确定异常传播链中的问题：

- Action 执行失败与 Evidence 未命中区分；
- failed node；
- failed edge；
- 节点之间的时间关系；
- lock holder 时间不足；
- 并发或 workload 不足；
- SQL duration 短于观察窗口；
- Action duration 超过 injection window；
- Metrics 没有达到传播链节点阈值。

建议必须针对当前失败原因，只调整相关参数，不对所有参数无条件翻倍。例如：

```json
{
  "source": "chain",
  "recommended_changes": {
    "holder_lock_seconds": 10,
    "reason": "row_lock evidence was not observed before holder released the lock"
  }
}
```

`ChainReflector` 不分析 DBA Post 文本、provenance、缺失信息或根因假设。

## 5. Post Reflection

新增 `agent/post_reflector.py`：

```python
class PostReflector:
    def reflect(self, context: ReflectionContext) -> ReflectionResult:
        ...
```

`PostReflector` 只处理帖子复现相关问题：

- Post fact 与当前数据库环境不一致；
- SQL、Schema、数据规模或索引信息缺失；
- 原帖事实与 Agent 推断冲突；
- 当前复现策略与帖子描述不一致；
- Direct 与 Propagation 复现策略选择不合适；
- 需要补充的信息或人工确认；
- Post-specific Action 生成失败。

`PostReflector` 不直接判断传播链边、不调整 Chain Agent 的 holder/waiter 参数，也不读取或修改 `ChainReflector` 的诊断结果。

## 6. Agent 调用边界

### ChainAgent

```python
class ChainAgent:
    reflector: ChainReflector

    def reproduce_chain_with_reflection(...):
        ...
```

`ChainAgent` 只调用 `ChainReflector`：

```text
生成 Chain ActionPlan
→ Validation
→ Safety
→ 执行与 Evidence
→ ChainReflector
→ 生成下一轮完整 Chain ActionPlan
```

### PostAgent

```python
class PostAgent:
    reflector: PostReflector

    def reproduce_post_with_reflection(...):
        ...
```

`PostAgent` 只调用 `PostReflector`：

```text
IncidentSpec / Post Strategy
→ 生成 Post ActionPlan
→ Validation
→ Safety
→ 执行与 Evidence
→ PostReflector
→ 生成下一轮完整 Post ActionPlan
```

两个 Agent 不允许通过依赖注入传入对方 Reflector，也不允许根据 `source` 在运行时动态切换 Reflector。

## 7. Action 重新生成规则

无论 Chain 还是 Post：

1. Reflection 只输出诊断和建议；
2. 下一轮必须重新调用对应 Agent 的 ActionGenerator；
3. 下一轮必须重新经过 Pydantic、ActionValidator 和 SafetyChecker；
4. Runtime 不得静默修改上一次 SQL 或 Action；
5. 达到 `max_retry_rounds` 后停止；
6. 已成功的 Action 通过 `keep_successful_actions` 记录，避免无意义重复生成。

两类 Reflection 的建议必须带有 `source`，防止将 Chain 建议误传给 Post Prompt，或将 Post 建议误传给 Chain Prompt。

## 8. Artifact 隔离

继续复用现有 `ArtifactStore`，但在 Artifact 中区分来源：

```text
attempt_001/
├── chain/
│   ├── action_plan.json
│   ├── action_result.json
│   ├── evaluation.json
│   └── reflection.json
└── post/
    ├── action_plan.json
    ├── action_result.json
    ├── evaluation.json
    └── reflection.json
```

如果一次实验只运行一种 Agent，也可以只创建对应目录，但 Reflection 文件必须包含：

```json
{
  "source": "chain",
  "attempt": 1,
  "failed_node": "row_lock",
  "failed_edge": "row_lock->slow_query",
  "diagnosis": "...",
  "recommended_changes": {},
  "keep_successful_actions": []
}
```

## 9. 测试文件

新增 `test/test_m11.py`，不调用真实 LLM、MySQL、BenchBase 或 ChaosBlade。

### ChainReflector 测试

- Action 执行失败与 Evidence 未命中能够区分；
- terminals 太低时只建议增加 workload 并发相关参数；
- holder 时间太短时只建议增加持锁时间；
- SQL duration 太短时只建议增加 duration；
- duration 超过 injection window 时建议缩短 duration；
- 正确输出 failed node 和 failed edge；
- 不会无条件翻倍所有参数。

### PostReflector 测试

- Schema、数据规模或索引缺失时给出信息补充建议；
- Post fact 与环境冲突时保留冲突，不静默覆盖；
- Direct/Propagation 策略不匹配时给出策略调整建议；
- 不生成 Chain 专属 holder/waiter 调整建议。

### 隔离性测试

- `ChainAgent` 只调用 `ChainReflector`；
- `PostAgent` 只调用 `PostReflector`；
- Chain Reflection 不会进入 Post Prompt；
- Post Reflection 不会进入 Chain Prompt；
- 两个 Reflection 的 `source` 不可错误互换。

### 多轮流程测试

- Attempt 1 失败后产生对应 Reflection；
- Attempt 2 生成完整新 ActionPlan；
- Attempt 2 再次经过 Validation 和 Safety；
- 成功后停止重试；
- 超过 `max_retry_rounds` 后停止并保留最终失败原因；
- Artifact 能还原每轮 Action、结果和对应 Reflection。

## 10. 验收标准

```text
ChainAgent → ChainReflector → Chain ActionPlan
PostAgent  → PostReflector  → Post ActionPlan
```

必须满足：

- 两个 Agent 的 Reflection 实现和依赖关系相互隔离；
- 共享模型只定义协议，不包含业务判断；
- 每轮 Action 都重新经过 Pydantic、Validation 和 Safety；
- Reflection 不修改原始 Action；
- Chain 失败只触发 Chain 规则；
- Post 失败只触发 Post 规则；
- Artifact 能明确区分 Chain Reflection 和 Post Reflection；
- 不会出现两个 Reflector 互相覆盖、互相调用或共享错误上下文的情况。
