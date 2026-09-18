# DBA Incident Reproduction Agent

当前仓库处于 M0：只提供工程骨架、类型化配置、统一 ArtifactStore、结构化日志和最小诊断命令。此阶段不调用 LLM，也不运行数据库异常实验。

## 环境

使用项目指定的 Conda 环境：

```bash
conda activate dbmags-hierarchy-agent
```

安装项目和测试依赖：

```bash
python -m pip install -e '.[test]'
```

如果只需要运行当前工作区已有依赖，也可以直接执行：

```bash
python -m pytest -q test/test_m0.py
```

## M0 验证

不连接数据库，仅检查配置和 Artifact 根目录：

```bash
python -m db_repro_agent.main doctor --skip-db
```

检查数据库 TCP 可达性：

```bash
python -m db_repro_agent.main doctor
```

M0 的测试和验收用例位于 [test/test_m0.py](test/test_m0.py)。实验产物默认保存到 `experiment_runs/`，该目录不应提交到 git。

配置支持 YAML/JSON 文件，环境变量可以覆盖文件值。数据库密码和 LLM API key 只从环境变量读取，不应写入版本库。
