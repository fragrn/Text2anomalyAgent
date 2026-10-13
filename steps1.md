# DBA Incident Reproduction Agent：M10 之后修订开发步骤

## 修订原则

系统保留两个 Agent 入口，但共享同一套复现内核：

```text
Chain Agent:
TargetPropagationChain
    ↓
Chain Validation
    ↓
ActionPlan Generation
    ↓
Propagation Reproduction

Post Agent:
DBA Post
    ↓
IncidentSpec
    ↓
Information HITL
    ↓
Direct / Propagation Strategy
    ↓
Direct 或 Post-specific Propagation Reproduction
```

两条路径共享：

```text
ContextBuilder
→ ActionGenerator
→ Pydantic
→ ActionValidator
→ SafetyChecker
→ Executor
→ Metrics
→ Evidence
→ Evaluation
```

数据库环境信息采集必须使用确定性的 Database Tool，不能通过 ReAct 循环让 LLM
自行探索数据库。LLM 只能消费已经采集并带来源的环境 Context。

全局 AnomalyGraph 保存可复用的节点、边和 Evidence Rule；Post-specific
AnomalyGraph 是当前帖子对应的子图或实例，不与全局规则冲突。

---

# M10：双入口共享的 Action Generation

## 目标

让 Agent 根据已经确定的目标节点、传播链上下文和数据库环境，直接生成最终
`ActionPlan`。这一阶段不负责理解 DBA Post，也不负责自动构图。

## 代码

```text
agent/
├── llm_client.py
├── context_builder.py
├── action_generator.py
├── chain_agent.py
└── post_agent.py       # 先建立接口，后续阶段逐步接入能力
```

新增两个稳定入口：

```python
class ChainAgent:
    def reproduce_chain(self, request: ChainReproductionRequest) -> ExperimentResult:
        ...

class PostAgent:
    def reproduce_post(self, request: PostReproductionRequest) -> ExperimentResult:
        ...
```

M10 中 `PostAgent` 只保留接口，不进入真实帖子分析；`ChainAgent` 可以调用已有
的 `ActionGenerator`。

## Context

Context 由 Database Tool 和 Workload Tool 预先收集，包括：

- Schema、索引、数据规模；
- EXPLAIN 和运行状态；
- 锁、慢查询、临时表、Redo、Buffer Pool 等相关指标；
- BenchBase 状态；
- Action 时长、并发和安全限制；
- 上一轮 Action 和 Reflection 结果。

## Chain Agent Prompt 结构

Chain Agent 的 Prompt 必须由固定的 System Prompt 和结构化 Context 组成，至少包含：

1. Agent 角色与本次复现目标；
2. 确定的异常传播链；
3. 数据库环境 Context；
4. Workload 状态；
5. 当前允许生成的 Action 类型和范围；
6. 时长、并发和 Safety 限制；
7. 输入中明确指定的主动注入或执行节点；
8. 输入中明确指定的下游观测节点；
9. 严格的 `ActionPlan` JSON Schema；
10. 禁止直接执行 Action、禁止修改已生成的 SQL、禁止使用 ReAct 探测数据库。

Prompt 不得让 LLM 自行决定未在输入中指定的注入范围。数据库环境信息必须由确定性的
Database Tool 预先采集，再通过 `ContextBuilder` 写入 Prompt。

期望数据流：

```text
TargetPropagationChain
    + Database Environment Context
    + Workload Status
    + Action / Safety Constraints
    ↓
Chain Agent Prompt
    ↓
ActionPlan JSON
    ↓
Pydantic → ActionValidator → SafetyChecker
```

## 测试与验收

- 四类 Action 都能解析为标准 `ActionPlan`；
- Action 必须经过 Pydantic、ActionValidator、SafetyChecker；
- 不允许把 LLM 原始 JSON 直接交给 Executor；
- 100 个 Action 统计结构化成功、Validation、执行成功和 Evidence 命中率。

---

# M11：Reflection 与 Action Retry

## 目标

Action 或传播边未命中时，生成诊断和下一轮完整 ActionPlan。

## 代码

```text
agent/reflector.py
models/reflection.py
```

Reflection 输入：失败节点、失败边、Metrics Summary、Node/Edge 结果、历史
Action、剩余预算。

Reflection 输出：失败原因、建议变化、保留的成功动作。下一轮必须重新生成完整
Action，并重新经过 Validation 和 Safety，禁止 Runtime 静默修改 SQL。

## 测试与验收

- 能区分 Action 执行失败和 Evidence 未命中；
- 能针对低并发、锁持有时间不足、观察窗口不足给出不同调整；
- Artifact 记录每一轮 Action、Reflection 和结果；
- 超过 `max_retry_rounds` 后停止重试并输出失败原因。

---

# M12：Chain Agent Propagation-only Gate

## 目标

在没有 DBA Post、没有自动构图的情况下，验证 Chain Agent 能够复现人工指定的
传播链。这是进入 Post Agent 的硬门槛。

## 代码

```text
agent/chain_agent.py
graph/chain_validator.py
reproduction/propagation_runner.py
```

Chain Agent 输入：

```text
TargetPropagationChain
Database Environment Context
Workload Profile
```

Chain Agent 根据输入的确定传播链生成并执行所需 Action；下游节点的命中情况由
Metrics、Evidence 和传播链 Evaluation 判断。

## 测试与验收

- 验证节点和边是全局图中的合法路径；
- 多次运行统计 Node Hit Rate、Edge Hit Rate、Full Graph Success Rate；
- 输出 first_failed_edge、每个节点 first_trigger_time；
- 至少若干代表性链在限定 attempts 内稳定复现；
- Metrics 连续采样和 Graph Evaluation 必须确定性通过。

---

# M13：Post Agent Incident Analyzer

## 目标

Post Agent 先只回答“帖子描述了什么”，暂时不选择 Direct/Propagation，也不自动
生成 Action。

## 代码

```text
agent/incident_analyzer.py
incident/parser.py
incident/provenance.py
prompts/incident_analysis.txt
```

输出 `IncidentSpec`，区分：

- Post fact；
- Post author hypothesis；
- Answer/reply hypothesis；
- Agent inference；
- Missing information。

## 测试与验收

- 使用人工标注的 DBA Post fixture；
- 检查 DBMS、Schema、SQL、数据规模、索引、配置、并发、症状和根因；
- 事实冲突不能静默覆盖，必须保留 provenance；
- 缺失信息必须明确标记 critical 与 impact。

---

# M14：Experiment Checkpoint、Pause 与 Resume

## 目标

为 Information HITL 和 Strategy HITL 提供跨进程恢复能力。

## 代码

```text
runtime/checkpoint.py
runtime/orchestrator.py
models/experiment_state.py
```

复用 M0 `ArtifactStore`，保存：

```text
experiment_state.json
phase_history.jsonl
```

支持：

```text
save_state()
load_state()
pause()
resume()
```

暂停时不能继续运行实验，且必须确认没有残留 workload、事务、锁或 ChaosBlade。

## 测试与验收

- 在任意稳定 phase 保存后模拟进程退出；
- resume 后不重复执行已完成阶段；
- phase history 能完整还原状态；
- 暂停状态下不执行 Action。

---

# M15：Information Sufficiency 与 Information HITL

## 目标

Post Agent 判断帖子缺失信息是否阻止复现，只在关键缺失时进入 HITL。

## 代码

```text
incident/sufficiency.py
hitl/information_gate.py
hitl/response_parser.py
hitl/incident_merger.py
```

结果：

```text
SUFFICIENT
NEED_HUMAN_INPUT
BLOCKED
```

可以通过 Database Tool 获取的信息不询问人；无法可靠恢复且影响复现机制的信息
才生成 HITL 问题。

## 测试与验收

- 缺数据规模时进入 HITL，补充后重新检查；
- Human 输入保存为 `HUMAN_INPUT` provenance；
- `unknown`、abort 和无效回答有明确处理；
- HITL 前后可通过 checkpoint resume；
- HITL 期间没有后台 workload、事务或 ChaosBlade。

---

# M16：Post Agent Strategy Selection

## 目标

InformationSpec 足够后，Post Agent 判断使用 Direct 还是 Propagation，并说明理由。

## 代码

```text
agent/strategy_selector.py
models/strategy.py
prompts/strategy_selection.txt
```

输出：

```text
incident_summary
anomalies
has_propagation
proposed_propagation
recommended_mode
rationale
confidence
```

Post Agent 不直接修改传播图，也不直接执行 Action。

## 测试与验收

- Direct fixture；
- 明确 Propagation fixture；
- 多异常但无因果关系 fixture；
- 因果方向不明确 fixture；
- 统计 Mode Selection Accuracy、Propagation Precision、Propagation Recall。

---

# M17：Strategy HITL

## 目标

由人审批 Post Agent 的 Direct/Propagation 判断。

## 代码

```text
hitl/strategy_gate.py
```

支持：

```text
APPROVE
OVERRIDE_DIRECT
OVERRIDE_PROPAGATION
REVISE
ABORT
```

## 测试与验收

- approve 使用 Agent 方案；
- override direct 进入 Direct Agent；
- override propagation 进入 Chain Agent；
- revise 重新调用 Strategy Selector；
- abort 进入终止状态并保存 Artifact。

---

# M18：双 Agent CLI 与统一编排入口

## 目标

将两个 Agent 的输入边界明确暴露给 CLI 和 Orchestrator。

## 代码

```text
agent/post_agent.py
agent/chain_agent.py
runtime/orchestrator.py
cli.py
```

支持：

```text
--mode auto
--mode direct
--mode propagation
```

语义：

- `auto`：Post Agent 分析帖子、触发 Information HITL、选择策略并等待 Strategy HITL；
- `direct`：直接调用 Direct Reproduction，不调用 Strategy Selector；
- `propagation`：输入确定传播链，直接调用 Chain Agent；
- 所有模式仍可触发 Information HITL，但 Chain Agent 不需要 DBA Post 分析。

## 测试与验收

- 同一 Post 分别运行 auto、direct、propagation；
- 确认 direct 不调用 Graph Builder；
- 确认 propagation 不调用 Incident Analyzer；
- 两种 Agent 产出的实验 Artifact 格式统一。

---

# M19：Post-specific AnomalyGraph Builder

## 目标

Strategy 选择为 Propagation 后，Post Agent 根据 IncidentSpec 和全局 Graph 构造当前
帖子的专属传播子图。

## 代码

```text
agent/post_graph_builder.py
graph/post_graph.py
graph/validator.py
```

Builder 只能使用：

- IncidentSpec 中的事实和推断；
- 全局 AnomalyGraph 已注册的节点、边和 Evidence Rule；
- Runtime 已采集的环境信息。

如果无法确定边的方向或机制，必须标记不确定性或进入 HITL，不能伪造确定边。

## 测试与验收

- 从帖子生成正确的节点和边；
- provenance 和 confidence 保留；
- 非法节点、非法边和时间关系被拒绝；
- 生成的子图可被 Chain Agent 接收。

---

# M20：Post Agent 环境重建

## 目标

根据 IncidentSpec、Post-specific Graph 和 Environment Context 重建可复现实验环境。

## 代码

```text
preparation/schema.py
preparation/data.py
preparation/index.py
preparation/configuration.py
preparation/workload.py
```

所有环境变更都必须经过 ActionValidator、SafetyChecker，并记录前后配置和来源。

## 测试与验收

- Schema、数据规模、索引和配置可重复建立；
- 不覆盖原始实验库；
- 重建失败能区分 SYSTEM_ERROR 和 EXPERIMENT_MISS；
- cleanup 后环境状态可恢复或有明确 Artifact。

---

# M21：Post Agent Direct Reproduction E2E

## 目标

打通无传播关系 DBA Post：

```text
Post
→ IncidentSpec
→ Information HITL
→ Direct Strategy
→ Environment Reconstruction
→ DirectRunner
→ Evidence / Result
```

## 测试与验收

- 使用已人工验证的 slow-query Post；
- 验证 Post fact、Human input、Agent inference provenance；
- 验证 Action、Metrics、Evidence、Cleanup 全链路；
- 报告中说明选择 Direct 的理由。

---

# M22：Post Agent Propagation Reproduction E2E

## 目标

打通包含传播关系的 DBA Post：

```text
Post
→ IncidentSpec
→ Information HITL
→ Strategy HITL
→ Post-specific Graph
→ Chain Agent
→ PropagationRunner
→ Node / Edge / Chain Result
```

## 测试与验收

- 帖子事实不足时先暂停，不得直接脑补执行；
- 只注入允许主动注入的 root 节点；
- 下游节点仅通过 Metrics 和 Evidence 判断；
- 输出 first_failed_edge、节点触发时间、完整传播链结果；
- 失败时能定位是帖子理解、Graph、Action、Metrics 还是 Evidence 问题。

---

# M23：Auto Mode、Benchmark 与消融实验

## 目标

评估系统能否从 DBA Post 自动完成：

```text
理解帖子
→ 补全关键环境信息
→ 选择 Direct / Propagation
→ 生成 Graph / Action
→ 执行并评估
```

## 评估指标

- Incident extraction accuracy；
- Missing information precision / recall；
- Mode selection accuracy；
- Graph node / edge accuracy；
- Structured Output Success Rate；
- Validation Pass Rate；
- Execution Success Rate；
- Anomaly Hit Rate；
- Direct reproduction success rate；
- Propagation full graph success rate；
- Average HITL rounds；
- Average retry attempts。

## 消融实验

```text
w/o Information HITL
w/o Strategy HITL
w/o Reflection
forced Direct
forced Propagation
w/o provenance
```

## 最终验收

- Chain Agent 可以独立复现确定传播链；
- Post Agent 可以分别进入 Direct 和 Propagation；
- 两个 Agent 共享 Executor、Metrics、Evidence 和 Artifact 协议；
- 环境采集不使用 ReAct；
- 所有 Action 都经过 Pydantic、Validation 和 Safety；
- 所有实验结果可通过 Artifact 复盘。
