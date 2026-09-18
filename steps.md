# DBA Incident 自动复现 Agent：详细开发与验证计划

> 本文在总体设计方案基础上，进一步细化每一个开发阶段。核心原则是：**每一步只引入一种主要能力，每一步完成后都必须能够脱离后续模块单独测试和验收**。这样当最终
> DBA Post
> 复现失败时，可以明确区分是帖子理解、HITL、策略选择、动态图构建、Agent
> 动作生成、执行、指标采集还是 Evaluation 出现问题。

## 一、开发总原则

系统最终支持两条复现路径：

-   **Direct
    Reproduction**：帖子没有需要显式复现的异常传播关系时，直接重建帖子描述的数据库状态并复现目标异常。
-   **Propagation Reproduction**：帖子包含异常传播关系时，由 Agent
    针对当前帖子构建 `PostAnomalyGraph`，再按照传播图复现节点和边。

系统不预先维护一张大型全局异常传播图。`AnomalyGraph` 是当前 DBA Incident
的动态结构化表示。为了避免 Agent
自己定义"什么算成功"，系统仍维护一个较小的、确定性的
`Evidence Rule Registry`。

在 `--mode auto` 下，Agent
需要先概括帖子描述的异常，展示识别出的异常、可能的传播关系、推荐的复现方式和理由，再进入
**Strategy HITL** 由人审批。用户也可以通过 `--mode direct` 或
`--mode propagation` 直接指定模式。

开发顺序必须遵守下面的依赖关系：

``` text
基础数据结构 / Tool / Executor
        ↓
Validation / Safety / Metrics / Evidence
        ↓
Direct Reproduction（人工 Action）
        ↓
Propagation Reproduction（人工 Graph + 人工 Action）
        ↓
Agent 直接生成 SQL / Action
        ↓
Reflection
        ↓
Propagation-only Gate
        ↓
DBA Post 理解
        ↓
Information HITL
        ↓
Strategy Selection + Strategy HITL
        ↓
Post-specific Graph Builder
        ↓
环境重建
        ↓
Direct / Propagation DBA Post E2E
        ↓
Auto Mode + Benchmark
```

特别注意：**在传播链本身还不能稳定复现以前，不要把 DBA Post、自动构图和
HITL 耦合进来。**
否则实验失败后很难判断是传播注入策略有问题，还是帖子理解/构图有问题。

------------------------------------------------------------------------

# M0：建立工程骨架、配置系统和统一 Artifact

## 这一步是在做什么

第一步不实现任何 Agent 能力，也不调用
LLM。目标是把整个项目的"地基"搭好：代码放在哪里、配置如何加载、日志如何记录、一次实验的结果保存在哪里、错误如何表示。

后续每个模块都依赖这些基础协议。如果一开始没有统一约定，后面很容易出现
`planner.py`、`runtime.py`、`executor.py`
互相传裸字典，字段名不断变化，最终难以定位问题。

## 需要开发什么

创建基础目录：

``` text
db_repro_agent/
├── main.py
├── cli.py
├── config.py
├── models/
├── runtime/
│   └── artifact_store.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── propagation/
│   └── e2e/
└── pyproject.toml
```

实现：

1.  `config.py`
    -   DB 连接配置。
    -   LLM 配置。
    -   BenchBase 配置。
    -   实验时间窗口。
    -   `max_attempts`。
    -   `max_hitl_rounds`。
    -   Artifact 根目录。
2.  `models/common.py`
    -   `ExperimentId`
    -   `ResultStatus`
    -   时间戳模型。
    -   通用错误模型。
3.  `runtime/artifact_store.py`
    -   `create_experiment()`
    -   `write_json()`
    -   `write_jsonl()`
    -   `read_json()`
    -   `create_attempt_dir()`
4.  统一 logging：
    -   experiment id。
    -   phase。
    -   attempt。
    -   module。
    -   action id。

## 如何测试

### 单元测试

测试配置能否从 YAML / 环境变量加载：

``` bash
pytest tests/unit/test_config.py
```

测试 Artifact round-trip：

``` python
store.write_json("incident.json", incident)
loaded = store.read_json("incident.json")
assert loaded == incident
```

测试同一个 Post 的 `attempt_001 / attempt_002` 是否正确递增。

### 集成测试

启动一个最简单的 MySQL Docker：

``` bash
docker compose up -d mysql
python -m db_repro_agent.main doctor
```

`doctor` 只检查配置和数据库连通性，不运行实验。

## 如何验收

这一阶段通过的标准：

-   新机器根据 README 可以创建环境。
-   `pytest tests/unit` 全部通过。
-   能创建一个实验目录。
-   JSON/JSONL 能正确写入和读取。
-   日志中能够看到 experiment/attempt/phase。
-   后续模块不需要自己创建实验目录或自己实现 JSON 保存逻辑。

------------------------------------------------------------------------

# M1：定义 IncidentSpec、Provenance 和缺失信息模型

## 这一步是在做什么

这一阶段建立"系统如何描述一个 DBA
Incident"的统一语言。虽然现在还不解析真实帖子，但要先把帖子中的事实、Agent
推断、人补充的信息以及缺失信息分开。

这是后面 HITL 的基础。如果不保存
provenance，最终很难知道某个并发数到底来自原帖、Agent 猜测还是人工补充。

## 需要开发什么

创建：

``` text
models/
├── incident.py
└── common.py
```

核心结构：

``` python
class InformationSource(StrEnum):
    POST_FACT = "post_fact"
    AGENT_INFERENCE = "agent_inference"
    RUNTIME_OBSERVATION = "runtime_observation"
    HUMAN_INPUT = "human_input"

class EvidenceItem(BaseModel):
    field: str
    value: Any
    source: InformationSource
    evidence: str | None
    confidence: float | None

class MissingInformation(BaseModel):
    field: str
    reason: str
    critical: bool
    impact: str

class IncidentSpec(BaseModel):
    incident_summary: str
    dbms: EvidenceItem | None
    schema_facts: list[EvidenceItem]
    data_facts: list[EvidenceItem]
    sql: list[EvidenceItem]
    configuration_facts: list[EvidenceItem]
    workload_facts: list[EvidenceItem]
    symptoms: list[EvidenceItem]
    root_cause_hypotheses: list[EvidenceItem]
    missing_information: list[MissingInformation]
```

另外实现 provenance merge 规则。

例如：

``` text
帖子：users 有约 1350 行
Human：实际复现实验希望使用 1500 行
```

不能直接覆盖掉原帖事实，而应该保留两条不同来源的信息。

## 如何测试

人工构造三个 Incident fixture：

1.  信息完整的 slow query。
2.  缺数据规模的 slow query。
3.  帖子事实与 Agent inference 冲突的案例。

验证：

-   序列化/反序列化一致。
-   `POST_FACT` 不会被 `AGENT_INFERENCE` 静默覆盖。
-   Human 输入能够单独追踪。
-   缺失字段能够表达 `critical=True/False`。

## 如何验收

给定任意一个人工 Incident，可以回答：

-   原帖明确说了什么？
-   Agent 推断了什么？
-   人后来补充了什么？
-   还有哪些关键信息缺失？

如果这些信息只能从日志或 prompt 中猜，则这一阶段没有通过。

------------------------------------------------------------------------

# M2：数据库 Tool Layer

## 这一步是在做什么

这一阶段只解决一个问题：**程序能不能可靠地查看和操作数据库。**

Agent 后面需要知道 Schema、索引、数据规模、当前 Runtime、SQL 的
EXPLAIN。如果这些工具本身不稳定，那么 Agent 再聪明也无法复现异常。

因此 Tool Layer 必须先脱离 LLM 独立验证。

## 需要开发什么

目录：

``` text
tools/database/
├── base.py
├── mysql.py
├── postgres.py
├── schema_probe.py
├── runtime_probe.py
└── explain.py
```

第一阶段重点实现 MySQL。

接口包括：

``` python
probe_schema(database)
probe_indexes(database, table)
probe_row_counts(database)
probe_runtime()
execute_readonly(sql)
explain_sql(sql)
```

`ToolResult` 必须包含：

``` text
success
data
error_code
error_message
duration_ms
started_at
finished_at
```

数据库原始错误必须保留，例如 MySQL 1040
`Too many connections`，不能只保存 `"execution failed"`。

## 如何测试

在 Docker MySQL 建测试表：

``` sql
CREATE TABLE accounts (
    id INT PRIMARY KEY,
    balance DECIMAL(12,2) NOT NULL
);
```

分别测试：

-   正常 SELECT。
-   EXPLAIN。
-   不存在的表。
-   SQL syntax error。
-   connection timeout。
-   statement timeout。
-   唯一键冲突。
-   数值越界。

特别验证执行：

``` sql
UPDATE accounts SET balance = balance + 999999999999 WHERE id = 1;
```

能够保留数据库实际错误码，而不是被 Tool 层吞掉。

## 如何验收

无需 Agent，仅调用 Python Tool API 就可以：

1.  获取数据库 Schema。
2.  获取索引。
3.  获取行数。
4.  执行 EXPLAIN。
5.  捕获并结构化返回数据库错误。

------------------------------------------------------------------------

# M3：ExecutableAction 与 Executor

## 这一步是在做什么

现在开始定义"Agent 最终能让系统执行什么"。

这一设计里 Agent **不输出 Intent**，而是直接输出最终
SQL、事务脚本、BenchBase 参数或 ChaosBlade 命令。因此必须先把 Executor
协议固定下来，之后 Agent 只需要生成符合这个协议的对象。

## 需要开发什么

创建：

``` text
models/action.py
execution/
├── dispatcher.py
├── sql_executor.py
├── transaction_executor.py
├── benchbase_executor.py
├── chaos_executor.py
└── command_executor.py
```

至少支持：

-   `SQLAction`
-   `TransactionAction`
-   `BenchBaseAction`
-   `ChaosBladeAction`

`TransactionAction` 要能表达锁冲突中的 holder / waiter：

``` text
holder:
BEGIN
SELECT ... FOR UPDATE
sleep

waiter:
BEGIN
UPDATE ...
COMMIT
```

所有 Executor 返回统一的 `ActionResult`。

## 如何测试

这一阶段全部使用**人工写死的 Action fixture**，不使用 LLM。

### SQLAction

执行：

``` sql
SELECT SLEEP(2);
```

验证 duration。

### TransactionAction

构造 holder/waiter，验证 waiter 出现 lock wait。

### BenchBaseAction

启动 TPC-C 30 秒：

-   检查 PID。
-   检查 workload 是否持续运行。
-   检查退出码。

### ChaosBladeAction

执行短时间 CPU pressure，结束后验证 cleanup。

## 如何验收

四种 Action 均能：

``` text
validate input
→ start
→ execute
→ timeout
→ stop
→ cleanup
→ structured result
```

并且 Executor 不包含任何 LLM 逻辑。

------------------------------------------------------------------------

# M4：Action Validation 与 Safety

## 这一步是在做什么

Agent 后面会直接生成 SQL，因此必须把"LLM
生成了什么"和"系统是否允许执行"分开。

Validator 检查协议和数据库对象是否合理；Safety
检查是否危险。它们只能拒绝 Agent 的 Action，不能偷偷把错误 SQL
改成另一个 SQL。

## 需要开发什么

``` text
runtime/
├── action_validator.py
└── safety.py
```

Validation：

-   database 是否是目标库。
-   table/column 是否存在。
-   SQL 是否为空。
-   transaction actor 是否完整。
-   duration 是否超预算。
-   concurrency 是否合理。
-   BenchBase benchmark/config 是否存在。
-   target_node 是否存在。

Safety：

-   禁止 DROP DATABASE。
-   禁止修改系统库。
-   禁止无限时长。
-   限制 command whitelist。
-   限制文件系统路径。
-   限制 ChaosBlade 资源强度。

返回：

``` text
PASS
REJECT
REGENERATE
```

## 如何测试

准备恶意/错误 Action：

-   `DROP DATABASE mysql`
-   修改 `information_schema`
-   不存在的 column
-   duration=36000
-   raw command=`rm -rf /`
-   正常 UPDATE

验证前五个被拒绝，最后一个通过。

## 如何验收

代码搜索确认不存在类似：

``` python
if sql_invalid:
    sql = fix_sql(sql)
```

Agent SQL 不允许 Runtime 静默修复。

------------------------------------------------------------------------

# M5：Metrics Timeline 与实验时间轴

## 这一步是在做什么

复现数据库异常不能只看 Action
有没有执行成功，而要知道注入前、注入期间、恢复阶段数据库发生了什么。

传播图更依赖时间关系，因此必须先建立统一 Experiment Clock。

## 需要开发什么

``` text
metrics/
├── collector.py
├── sampler.py
├── mysql_metrics.py
├── prometheus.py
├── slow_log.py
└── timeline.py
```

时间轴：

``` text
workload start
    ↓
warmup
    ↓
baseline
    ↓
injection + continuous sampling
    ↓
recovery
    ↓
workload stop
```

至少采集：

-   QPS
-   TPS
-   Threads_running
-   Threads_connected
-   lock waits
-   slow query log delta
-   CPU
-   memory
-   IO

每条样本必须有：

``` text
timestamp
phase
metric_name
value
```

## 如何测试

BenchBase 持续 60 秒，每秒采样。

人工在第 20 秒启动一个注入，在第 35 秒停止。

画出或导出 timeline，检查：

-   baseline 窗口正确。
-   injection marker 正确。
-   Metrics 没有因为 Action 执行阻塞而停止采样。
-   slow log 使用 marker 后新增记录，而不是读取历史累计值。

## 如何验收

能够从 `metrics.jsonl` 还原：

``` text
什么时候开始 baseline
什么时候开始 injection
什么时候异常指标第一次变化
什么时候恢复
```

------------------------------------------------------------------------

# M6：Evidence Rule Registry 与确定性 Evidence Engine

## 这一步是在做什么

这一步解决"什么叫异常命中"。

虽然系统不维护大型全局异常传播图，但不应该让 Agent 每次自己决定
`qps_drop` 的阈值，否则 Agent
同时负责生成异常和定义成功标准，会降低实验可信度。

## 需要开发什么

``` text
config/evidence_rules.yaml
graph/evidence_registry.py
evaluation/evidence_engine.py
models/evidence.py
```

支持：

-   absolute threshold
-   baseline ratio
-   delta
-   count
-   p95
-   consecutive samples

例如：

``` yaml
qps_drop:
  metric: qps
  aggregation: ratio
  reference: baseline
  operator: lt
  threshold: 0.7

slow_query:
  metric: new_slow_log_entries
  aggregation: count
  operator: gte
  threshold: 1
```

## 如何测试

不要先用真实数据库，先构造 Synthetic Timeline：

``` text
baseline QPS = 100
injection QPS = 69
```

应 HIT。

``` text
baseline QPS = 100
injection QPS = 70
```

按照严格 `< 0.7` 应 MISS。

再测试：

-   slow log +1。
-   lock waits delta \> 0。
-   metric missing。
-   NaN。
-   baseline=0。

## 如何验收

相同 Timeline 永远得到相同结果；Evidence Engine 不调用 LLM。

------------------------------------------------------------------------

# M7：Direct Reproduction 最小闭环------先不用 Agent

## 这一步是在做什么

现在第一次真正复现数据库异常，但仍然不让 LLM 参与。

目的不是追求智能，而是证明：

``` text
Action → Execute → Metrics → Oracle → Result → Cleanup
```

这条最基本的实验闭环是可靠的。

## 需要开发什么

``` text
reproduction/direct_runner.py
evaluation/incident_evaluator.py
```

先人工构造几类 Action：

1.  slow query。
2.  lock contention。
3.  CPU pressure。
4.  backup workload。
5.  high concurrency。

Direct Runner 管理：

``` text
prepare
→ baseline
→ execute
→ observe
→ recover
→ evaluate
→ cleanup
```

## 如何测试

每类异常至少运行 3 次。

例如 slow query：

-   人工 SQL `SELECT SLEEP(12)`。
-   目标：slow log marker 后新增记录。
-   验证 `IncidentEvaluator` HIT。

锁冲突：

-   holder 锁行。
-   waiter UPDATE。
-   验证 lock wait 指标。

## 如何验收

至少选 3 类代表性异常能够稳定复现。

特别检查：

-   SQL 执行失败属于 `SYSTEM_ERROR`。
-   SQL 成功但没有出现目标异常属于 `EXPERIMENT_MISS`。
-   两者不能混在一起。

------------------------------------------------------------------------

# M8：AnomalyGraph 模型与 Graph Evaluator------先只用合成数据

## 这一步是在做什么

开始实现异常传播图，但暂时既不让 Agent 构图，也不真正注入传播链。

先证明程序能够正确理解：

``` text
A → B → C
```

以及判断哪个节点、哪条边失败。

## 需要开发什么

``` text
models/anomaly_graph.py
graph/validator.py
graph/root_selector.py
evaluation/node_evaluator.py
evaluation/edge_evaluator.py
evaluation/graph_evaluator.py
```

Edge 至少包含：

``` text
source
target
mechanism
provenance
confidence
min_lag_sec
max_lag_sec
mechanism_metrics
```

## 如何测试

Synthetic Timeline 1：

``` text
A hit @ 10s
B hit @ 13s
C hit @ 18s
```

全部成功。

Timeline 2：

``` text
A hit
B miss
C miss
```

应输出 first failed edge `A→B`。

Timeline 3：

``` text
B @ 5s
A @ 10s
```

如果 Edge 要求 A 在 B 前，则 temporal invalid。

Timeline 4：节点都 HIT，但 mechanism metric 不满足，Edge 应失败。

## 如何验收

GraphEvaluator 能准确输出：

-   node hit。
-   edge satisfied。
-   lag。
-   failed node。
-   failed edge。
-   longest successful prefix。

------------------------------------------------------------------------

# M9：人工指定异常传播图的真实复现

## 这一步是在做什么

这是整个项目非常关键的一步。

此时**不输入 DBA Post，不让 Agent
自动构图**。人直接给系统一个确定的传播链，例如：

``` text
traffic_surge
    ↓
connections_up
    ↓
lock_contention
    ↓
slow_query
    ↓
qps_drop
```

然后验证系统是否真的能在数据库里复现这条传播链。

这样可以把"传播链本身难不难复现"和"Agent
能不能从帖子里理解传播关系"两个问题分开。

## 需要开发什么

``` text
reproduction/propagation_runner.py
```

人工提供：

``` json
{
  "nodes": [...],
  "edges": [...],
  "root_nodes": ["traffic_surge"]
}
```

人工提供 root injection Action。

Runner 只主动注入 root/minimal required nodes，下游节点只观察。

## 如何测试

运行：

``` text
BenchBase workload
→ baseline
→ traffic surge
→ continuous metrics
→ node evaluation
→ edge evaluation
```

至少重复 5 次。

记录每轮：

-   哪些节点 HIT。
-   哪条边第一次失败。
-   每个节点 first_trigger_time。
-   full graph 是否成功。

## 如何验收

即使当前还不能稳定全链命中，也必须做到：

-   root action 确实执行。
-   Metrics 全程连续。
-   Node/Edge Evaluation 正确。
-   能准确定位 `first_failed_edge`。

如果这一阶段都做不到，不进入 DBA Post 自动化。

------------------------------------------------------------------------

# M10：让 Agent 直接生成最终 SQL / Action

## 这一步是在做什么

前面 Executor、Safety、Metrics 都已经验证，现在才接 LLM。

Agent 的任务不是输出抽象
Intent，而是根据目标异常、Schema、Runtime、历史反馈，直接生成 Executor
可以消费的最终 Action。

## 需要开发什么

``` text
agent/
├── llm_client.py
├── context_builder.py
└── action_generator.py
```

输入 Context：

-   target anomaly/node。
-   Schema。
-   indexes。
-   data scale。
-   runtime snapshot。
-   workload status。
-   duration budget。
-   safety constraints。
-   previous reflection。

输出严格 Structured Output：

``` text
ActionPlan
└── SQLAction / TransactionAction / BenchBaseAction / ChaosBladeAction
```

## 如何测试

建立固定测试集：

### hot_update

给 Agent warehouse schema，看能否生成可执行 UPDATE。

特别检查过去出现过的：

``` sql
SET w_ytd = w_ytd + 1
```

在 `w_ytd` 接近上限时是否仍可能失败。

### lock contention

检查能否生成 holder/waiter。

### slow SQL

检查生成 SQL 是否存在、能 EXPLAIN、能执行。

### traffic surge

检查 BenchBase 参数合法。

统计：

``` text
Structured Output Success Rate
Validation Pass Rate
Execution Success Rate
Anomaly Hit Rate
```

## 如何验收

不能只看"JSON 能解析"。

至少要求能够分别知道：

``` text
100 个 Action
→ 多少格式正确
→ 多少 Validation 通过
→ 多少真正执行成功
→ 多少最终命中异常
```

这样才能定位 LLM SQL 质量问题。

------------------------------------------------------------------------

# M11：Reflection 与多轮动作再生成

## 这一步是在做什么

单轮 Agent 很难一次就精确命中异常传播链。因此需要把失败结果重新反馈给
Agent。

Reflection 不应该只告诉 Agent "失败了"，而要明确：

-   哪个节点没命中。
-   哪条边没建立。
-   当前参数是什么。
-   Metrics 实际变化是什么。
-   哪些动作已经有效，不要无意义重做。

## 需要开发什么

``` text
agent/reflector.py
models/reflection.py
```

`ReflectionContext`：

``` text
target
previous_actions
action_results
metrics_summary
node_results
edge_results
failed_node
failed_edge
environment
remaining_budget
```

输出：

``` text
diagnosis
recommended_changes
keep_successful_actions
```

下一轮由 `ActionGenerator` **重新生成完整 ActionPlan**。

## 如何测试

人为制造：

1.  terminals 太低。
2.  lock holder 时间太短。
3.  SQL 只运行 2 秒，达不到 slow log。
4.  workload 太轻。
5.  duration 超出 injection window。

验证 Reflection 是否针对失败原因调整，而不是无脑把所有参数翻倍。

## 如何验收

Artifact 中可以清楚看到：

``` text
Attempt 1
→ failed edge
→ reflection
→ Attempt 2 changed parameters/actions
→ new result
```

并且 Attempt 2 仍重新经过 Validation/Safety。

------------------------------------------------------------------------

# M12：Propagation-only Gate

## 这一步是在做什么

这是进入 DBA Post 之前的"硬门槛"。

目标是证明：即使没有帖子理解、没有自动构图，只给系统一个指定传播链，Agent
已经能够通过多轮 SQL/Action 调整复现传播关系。

如果这一点没有成立，那么后面加入 DBA Post 只会让问题更复杂。

## 需要开发什么

建立 `tests/propagation/cases/`：

``` text
traffic_to_lock_to_slow/
resource_to_slow/
connections_to_lock/
...
```

每个 case 固定：

-   target graph。
-   environment。
-   evidence rules。
-   workload。
-   max attempts。

## 如何测试

每个 case 重复多次，统计：

-   Node Hit Rate。
-   Edge Hit Rate。
-   Full Graph Success Rate。
-   Success@1。
-   Success@2。
-   Success@3。
-   Average Attempts。
-   first failed edge 分布。

## 如何验收

预先制定最低标准，例如：

-   root injection success \> 95%。
-   Metrics completeness \> 99%。
-   Graph evaluation deterministic = 100%。
-   至少若干代表性传播链能够在限定 attempts 内稳定成功。

具体成功率阈值应根据你的实验结果确定，而不是现在写死。

------------------------------------------------------------------------

# M13：DBA Post Incident Analyzer

## 这一步是在做什么

传播复现内核稳定以后，才开始真正读取 DBA Post。

Agent 首先只负责回答：

> 这个帖子描述了什么？

此阶段不要让它同时决定 Direct/Propagation，也不要让它同时构图。

## 需要开发什么

``` text
incident/
├── parser.py
└── provenance.py

agent/incident_analyzer.py
prompts/incident_analysis.txt
```

抽取：

-   DBMS/version。
-   schema。
-   data scale/distribution。
-   indexes。
-   SQL。
-   config。
-   workload/concurrency。
-   symptoms。
-   root-cause statements。
-   missing information。

要求严格区分：

``` text
Post explicit fact
Post author's hypothesis
Answer/reply hypothesis（如果输入包含回答）
Agent inference
Missing
```

## 如何测试

选 10～20 个 DBA Post，人工先做 Gold Annotation。

逐项比较：

-   SQL 是否完整抽取。
-   表名/列名。
-   行数。
-   索引。
-   并发。
-   症状。
-   root cause。
-   缺失信息。

## 如何验收

输出给人看的 `incident_summary` 应该可以独立阅读，例如：

> 帖子描述 MySQL 上一条带 leading wildcard 和复杂权限过滤的查询执行约 15
> 秒；issues 约 33 万行；帖子给出了
> EXPLAIN，但没有完整给出数据分布和所有相关表规模。

同时 JSON 中保留详细结构和 provenance。

------------------------------------------------------------------------

# M14：Information Sufficiency + Information HITL

## 这一步是在做什么

DBA Post
经常缺少复现所需状态，例如数据规模、索引、配置、并发数。系统不能让 Agent
无限制脑补。

因此需要判断：

> 缺的信息是否真的阻止复现？

如果只是可以通过 Runtime Probe
获得的信息，就不用问人；如果是帖子语义中无法可靠恢复的关键状态，再进入
HITL。

## 需要开发什么

``` text
incident/sufficiency.py
hitl/
├── manager.py
├── information_gate.py
├── response_parser.py
└── incident_merger.py
```

实现：

``` text
SUFFICIENT
NEED_HUMAN_INPUT
BLOCKED
```

HITL 问题必须说明：

-   缺什么。
-   为什么需要。
-   不补会影响什么。
-   可选答案是什么。

## 如何测试

Case 1：

帖子缺数据规模。

``` text
→ HITL
→ Human: 2,500,000 rows
→ merge
→ check again
→ sufficient
```

Case 2：

Human 回答 `unknown`，系统判断能否使用 Agent inference 或默认实验假设。

Case 3：

进程在 HITL 后退出，再运行：

``` bash
python main.py resume EXP001
```

继续执行。

Case 4：

Human abort。

## 如何验收

-   HITL 时没有后台 workload。
-   没有持有 DB transaction/lock。
-   没有残留 ChaosBlade。
-   回答后不会重复问同一个问题。
-   Human 信息标记为 `HUMAN_INPUT`。
-   HITL 可跨进程恢复。

------------------------------------------------------------------------

# M15：Agent 判断 Direct 还是 Propagation

## 这一步是在做什么

Incident 信息足够以后，Agent 再做第二个语义任务：

> 这个帖子应该直接复现，还是应该按照异常传播关系复现？

它不能只返回 `propagation` 一个词，而必须把自己的理解展示给人。

## 需要开发什么

``` text
agent/strategy_selector.py
models/strategy.py
prompts/strategy_selection.txt
```

输出：

``` text
incident_summary
anomalies
has_propagation
proposed_propagation
recommended_mode
rationale
confidence
```

例如：

``` text
帖子描述高并发更新导致热点记录竞争，
随后出现 lock wait 和查询延迟上升。

Anomalies:
- high concurrency
- lock contention
- slow query

Proposed:
high_concurrency → lock_contention → slow_query

Recommended mode:
PROPAGATION
```

## 如何测试

建立人工 Gold 数据集：

1.  明显 Direct。
2.  明显 Propagation。
3.  多异常但无明确传播。
4.  因果方向不明确。
5.  帖子只描述 symptom，没有 cause。

人工标注 `gold_mode`。

统计：

``` text
Mode Selection Accuracy
Propagation Precision
Propagation Recall
```

还要人工审查 Agent 有没有把普通数据库状态错误当成 anomaly node。

## 如何验收

Agent 必须能解释"为什么"。

如果 Agent 看到多个异常词就一律选择 propagation，这一步不通过。

------------------------------------------------------------------------

# M16：Strategy HITL

## 这一步是在做什么

Agent 的 Direct/Propagation
选择会直接决定后面是否构图，因此这是一个重要决策点，需要人审批。

Strategy HITL 不是让人从零分析帖子，而是把 Agent
已经整理好的结果展示出来，让人快速 approve/override/revise。

## 需要开发什么

``` text
hitl/strategy_gate.py
```

支持：

``` text
APPROVE
OVERRIDE_DIRECT
OVERRIDE_PROPAGATION
REVISE
ABORT
```

CLI 展示：

``` text
Incident Summary
Detected Anomalies
Possible Propagation
Agent Recommendation
Reason
Confidence

[1] Approve
[2] Override Direct
[3] Override Propagation
[4] Revise
[5] Abort
```

## 如何测试

### Override

Agent 推荐 propagation，人选择 direct。

检查：

``` json
decision_source = "human_override"
selected_mode = "direct"
```

### Revise

Human：

> 高并发只是背景，帖子核心是缺索引，不认为存在传播链。

系统重新调用 StrategySelector，生成新 Proposal。

### Abort

状态进入 ABORTED。

## 如何验收

最终 Artifact 中能够完整还原：

-   Agent 原本推荐什么。
-   人看到了什么。
-   人做了什么决定。
-   最终模式是什么。
-   为什么改变。

------------------------------------------------------------------------

# M17：命令行 Mode Control

## 这一步是在做什么

研究实验不能每次都依赖交互式 HITL。因此需要让实验人员直接控制复现模式。

## 需要开发什么

`cli.py`：

``` bash
python main.py reproduce post.txt --mode auto
python main.py reproduce post.txt --mode auto --yes
python main.py reproduce post.txt --mode direct
python main.py reproduce post.txt --mode propagation
```

语义：

-   `auto`：Agent 判断 + Strategy HITL。
-   `auto --yes`：Agent 判断，自动接受。
-   `direct`：人直接指定，不调用 StrategySelector。
-   `propagation`：人直接指定，不调用 StrategySelector。

但所有模式仍然允许 Information HITL。

## 如何测试

给同一个 Post 跑四次。

验证 phase history：

``` text
auto:
PROPOSE_STRATEGY → STRATEGY_HITL

auto --yes:
PROPOSE_STRATEGY → no strategy HITL

direct:
no strategy selector
no graph builder

propagation:
no strategy selector
→ graph builder
```

## 如何验收

CLI 指定模式优先级最高；Artifact 记录 `decision_source=user_cli`。

------------------------------------------------------------------------

# M18：Post-specific AnomalyGraph Builder

## 这一步是在做什么

只有最终模式为 propagation 时才进入这一阶段。

Agent 不查询一张预定义大型图，而是根据当前 IncidentSpec
构建**这个帖子自己的异常传播图**。

## 需要开发什么

``` text
agent/graph_builder.py
graph/validator.py
graph/evidence_registry.py
prompts/graph_building.txt
```

Agent 输出：

-   nodes。
-   edges。
-   edge mechanism。
-   provenance。
-   confidence。
-   root nodes。

节点的 Evidence Rule 优先从 Registry 绑定。

Edge 必须区分：

``` text
post_explicit
post_supported
agent_inferred
human_confirmed
```

## 如何测试

为 5～10 个传播型帖子人工标 Gold Graph。

比较：

-   Node precision/recall。
-   Edge precision/recall。
-   Edge direction accuracy。
-   root node accuracy。

故意输入：

-   cycle。
-   dangling edge。
-   unknown node。
-   无 Evidence Rule。
-   低 confidence edge。

验证 GraphValidator。

## 如何验收

生成的图必须能够回答：

> 这条边为什么存在？

如果只能回答"LLM 认为存在"，而没有帖子证据、机制解释或 inference
provenance，则不能作为高可信 Edge。

------------------------------------------------------------------------

# M19：DBA Post 环境重建

## 这一步是在做什么

帖子理解和复现策略确定后，需要把自然语言中的数据库状态真正变成实验环境。

这一步负责 Schema、Data、Index、Configuration、Workload
的准备，不负责生成异常注入 SQL。

## 需要开发什么

``` text
preparation/
├── schema.py
├── data.py
├── index.py
├── configuration.py
└── workload.py
```

每个 Preparation Step 输出：

``` text
expected state
actual state
commands/sql executed
verification result
```

例如：

``` text
expected issues rows ≈ 334823
actual issues rows = 334823
verified = true
```

## 如何测试

使用你已经复现过的 slow-query Post 作为 fixture：

-   建 issues。
-   建 projects。
-   造数据。
-   创建/删除指定索引。
-   配置 slow log。
-   运行目标 SQL。

每一步后立即 Probe 验证。

## 如何验收

不能因为 SQL `CREATE TABLE` 返回成功就认为环境正确。

必须做到：

``` text
prepare
→ probe
→ compare expected vs observed
```

环境错误必须在进入异常注入前被发现。

------------------------------------------------------------------------

# M20：DBA Post → Direct Reproduction E2E

## 这一步是在做什么

现在第一次把真实帖子和前面的 Direct Pipeline 接起来。

目标是验证一个没有异常传播关系的帖子能够：

``` text
Post
→ Incident
→ HITL
→ Direct
→ Environment
→ Agent SQL
→ Execute
→ Evaluate
→ Reflect
```

## 需要开发什么

实现 Direct E2E orchestration。

选择几类帖子：

-   improper SQL。
-   missing index。
-   slow query。
-   单一 lock contention。
-   单一 resource bottleneck。

## 如何测试

每个帖子至少运行：

``` text
attempt1
attempt2
attempt3
```

如果 attempt1 MISS：

-   保存 metrics。
-   Reflection。
-   Agent 生成新 SQL/参数。
-   再执行。

比较：

``` text
Success@1
Success@2
Success@3
```

## 如何验收

从最终 `final_report.json` 可以回答：

-   原帖异常是什么。
-   哪些信息来自原帖。
-   哪些信息由人补充。
-   为什么选择 Direct。
-   Agent 生成了什么 SQL。
-   SQL 是否可执行。
-   异常是否命中。
-   如果第一次没命中，第二次改了什么。

------------------------------------------------------------------------

# M21：DBA Post → Propagation Reproduction E2E

## 这一步是在做什么

这是系统最复杂的完整路径。

帖子不仅被理解，还会被 Agent 构造成 Post-specific AnomalyGraph，然后
Agent 通过根节点注入和连续 Metrics 尝试复现传播过程。

## 需要开发什么

完整串联：

``` text
Post
→ Incident Analyzer
→ Information HITL
→ Propagation Mode
→ Graph Builder
→ Graph Validator
→ Environment Reconstruction
→ Root Selection
→ Agent Action
→ Validation/Safety
→ Baseline
→ Injection
→ Continuous Metrics
→ Node Eval
→ Edge Eval
→ Graph Eval
→ Reflection
```

## 如何测试

选明确具有传播语义的 DBA Incident。

每个实验保存：

-   graph。
-   root actions。
-   metric timeline。
-   node first trigger time。
-   edge lag。
-   failed edge。
-   reflection。

至少重复多次，避免一次偶然成功。

## 如何验收

最终报告必须能够回答：

> 帖子描述的传播关系是什么？

> Agent 为什么构建这些节点和边？

> 系统主动注入了哪些根节点？

> 哪些下游异常是自然传播出现，而不是被主动注入？

> 每个节点什么时候出现？

> 哪条边没有成功建立？

> Reflection 下一轮具体调整了什么？

只有节点全部分别 HIT，但时间/机制边不成立，不能算传播图成功。

------------------------------------------------------------------------

# M22：Auto Mode、最终 Benchmark 与消融实验

## 这一步是在做什么

最后才验证完整系统的自主性：

> 给它一个 DBA Post，不提前告诉它用 Direct 还是
> Propagation，它能否正确理解帖子、让人在关键点审批，并最终复现异常？

这一阶段也是后续论文实验的基础。

## 需要开发什么

建立 DBA Post Benchmark。

每个 Post 人工标注：

-   Gold Incident Summary。
-   Gold Mode。
-   如果是 propagation：Gold/Reference Nodes 和 Edges。
-   关键缺失信息。
-   复现成功 Oracle。

记录系统指标。

## 如何测试

### 策略选择

``` text
Mode Selection Accuracy
Propagation Precision / Recall
Human Override Rate
```

### Incident Reconstruction

``` text
Fact Extraction Accuracy
Missing-information Recall
HITL Trigger Rate
Average HITL Rounds
```

### Graph Reconstruction

``` text
Node Precision / Recall
Edge Precision / Recall
Edge Direction Accuracy
```

### Action Quality

``` text
Structured Output Success
Validation Pass Rate
Execution Success Rate
```

### Reproduction

Direct：

``` text
Incident Reproduction Success@k
```

Propagation：

``` text
Node Hit Rate
Edge Hit Rate
Propagation Coverage
Full Graph Success@k
```

### Reflection

比较：

``` text
Success@1 vs Success@3
```

验证 Reflection 是否真正提高成功率。

## 消融实验

建议至少做：

``` text
Full System
w/o HITL
w/o Reflection
forced Direct
forced Propagation
w/o Evidence Registry
```

`forced Direct` 和 `forced Propagation` 很有价值，因为它可以回答：

> Agent 自动选择复现策略是否真的有意义？

`w/o HITL` 可以回答：

> 人工补全和策略审批是否提高了复现正确性？

`w/o Reflection` 可以回答：

> 多轮 Agent 调参是否提高复杂异常和传播链的复现率？

## 如何验收

最终系统至少需要支持：

``` bash
python main.py reproduce post.txt --mode auto
python main.py reproduce post.txt --mode direct
python main.py reproduce post.txt --mode propagation
python main.py resume EXPERIMENT_ID
```

并且任意实验都可以通过 Artifact 回答：

``` text
输入是什么
Agent 理解了什么
缺了什么
人补了什么
谁选择了复现模式
为什么选择
是否构图
图是什么
Agent 生成了什么
执行是否成功
Metrics 如何变化
异常/传播是否命中
失败在哪里
Reflection 如何修改
最终是否成功
```

------------------------------------------------------------------------

# 六、推荐实际开发阶段

虽然上面拆成 M0～M22，但实际执行时建议分四个大阶段。

## Phase A：确定性基础设施（M0～M6）

这一阶段完全不需要 LLM。

目标是把：

``` text
DB Tool
Executor
Validation
Safety
Metrics
Evidence
Artifact
```

做稳定。

**Phase A Gate：** 手工 Action 能被可靠执行和评价。

------------------------------------------------------------------------

## Phase B：异常复现内核（M7～M12）

先 Direct，再 Propagation；先人工 Action/Graph，再接 Agent。

目标是证明：

``` text
指定异常 → 能复现
指定传播图 → 能复现
Agent → 能直接生成 SQL/Action
Reflection → 能改善失败实验
```

**Phase B Gate：** 给定明确 target，系统本身能够稳定完成异常/传播复现。

这是最重要的工程门槛。

------------------------------------------------------------------------

## Phase C：DBA Incident Intelligence（M13～M18）

传播内核稳定后才增加：

``` text
Post Understanding
Information HITL
Strategy Selection
Strategy HITL
CLI Mode
Dynamic Graph Building
```

**Phase C Gate：**
给定帖子，系统能正确回答"帖子是什么异常、信息够不够、应该 Direct 还是
Propagation、如果 Propagation 图是什么"。

这一阶段甚至可以先不执行真实实验，只评价语义输出。

------------------------------------------------------------------------

## Phase D：完整 E2E 与论文实验（M19～M22）

最后耦合环境重建和真实复现。

目标是：

``` text
DBA Post
→ Incident Reconstruction
→ Human/Agent Decision
→ Direct or Propagation Reproduction
→ Iterative Reflection
→ Reproduction Dataset
```

**Phase D Gate：** 对一组 DBA Post 可以稳定批量运行，并产出完整可审计
Artifact 和论文实验指标。

------------------------------------------------------------------------

# 七、一个关键的 Debug 原则

最终系统失败时，不要只输出 `reproduction failed`，而要按层定位：

``` text
L1 Incident Failure
   帖子理解错误 / 事实抽取错误

L2 Information Failure
   关键状态缺失 / HITL 未解决

L3 Strategy Failure
   Direct/Propagation 选择错误

L4 Graph Failure
   节点/边/方向构建错误

L5 Action Generation Failure
   Agent SQL/Action 格式或语义错误

L6 Validation/Safety Failure
   Action 不允许执行

L7 Execution Failure
   DB/BenchBase/Chaos 实际执行错误

L8 Observation Failure
   Metrics 缺失或时间轴错误

L9 Reproduction Miss
   Action 执行成功，但目标异常没有出现

L10 Propagation Failure
   节点出现，但某条传播边没有建立
```

每层都必须有独立 Artifact 和错误类型。

这正是整个目录和开发顺序采用"一个职责一个 `.py` 文件"的原因：**Agent
负责推理，Runtime 负责生命周期，Executor 负责执行，Metrics
负责观测，Evaluator 负责判断，HITL
负责人机交互。任何一层都不应该替另一层偷偷完成工作。**

------------------------------------------------------------------------

# 八、最终开发路线一句话概括

最推荐的开发路线不是一开始就做：

``` text
DBA Post → Agent → AnomalyGraph → SQL → Experiment
```

而是逐层证明：

``` text
能执行
→ 能观测
→ 能判断异常
→ 能复现单异常
→ 能判断传播
→ 能复现指定传播链
→ Agent 能生成可执行 Action
→ Agent 能通过 Reflection 改进传播
→ 能理解 DBA Post
→ 能通过 HITL 补全帖子
→ 能选择 Direct / Propagation
→ 能从帖子动态构图
→ 最后才把所有能力耦合成完整 DBA Incident Reproduction Agent
```

这样每增加一个模块，都有上一阶段已经通过验收的能力作为可靠底座，最终系统出现问题时也能快速定位到具体阶段，而不是只能从一个庞大的
Agent Loop 中猜测失败原因。
