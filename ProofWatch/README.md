# ProofWatch 核心模型代码库

根据论文 *ProofWatch: Compiling Persistent Verification Obligations for Agent Commitments on the Evolving Web* 的公开方法实现的独立 Python 工程。此代码库保留核心模块与 YAML 配置，不包含测试集、演示数据或示例配置。本项目不属于论文作者官方源码，亦不承诺复现论文报告的实验指标。

## 环境和安装

需要 Python 3.10 或更高版本。

```bash
python -m pip install -e .
proofwatch --help
```

也可以使用仓库内 `dist/` 下的 wheel 安装，无需编辑 Python 源码。

## 代码结构

- `src/proofwatch/engine.py`：承诺与验证义务的编译、行动循环和局部修订
- `src/proofwatch/logic.py`：三值逻辑、替代支持路径、最小反例集合
- `src/proofwatch/probability.py`：贝叶斯风险、EVSI 估计、变化与访问概率
- `src/proofwatch/sources.py`：HTTP 与离线回放来源接口、内容解析与 URL 解析
- `src/proofwatch/memory.py`：SQLite 持久状态、容量限制与恢复
- `src/proofwatch/metrics.py`：承诺暴露损失与决策覆盖指标
- `src/proofwatch/config.py`：YAML 读取、覆盖合并和验证
- `src/proofwatch/cli.py`：命令行接口
- `configs/defaults.yaml`：可编辑的默认参数配置
- `src/proofwatch/defaults.yaml`：Python 包内的默认参数副本

## YAML 配置

`configs/defaults.yaml` 包含动作预算、内存容量、证据有效期、Gamma–Poisson 变化风险、Beta 访问概率、调度权重和 HTTP 请求参数。核心逻辑无须硬编码具体阈值、网址或目标。

运行时，需要**另外提供你自己的业务 YAML 文件**，其中包含非空的 `sources`、`propositions` 和 `goals`：

- `sources`：每个数据源的 `id`、`kind`（`http` 或 `fixture`）、`resource` 与 `category`。
- `propositions`：每条谓词的 `id`、`source`、`path`、`operator` 及可选/必需的 `threshold`。
- `goals`：每个目标的 `id`、`support_paths` 和可选 `weight`。
- `engine`：按需覆盖默认参数，缺失部分由包内 YAML 自动补齐。

支持的比较操作符：`eq`、`ne`、`ge`、`gt`、`le`、`lt`、`contains`、`exists`。`fixture` 类型需另外提供 `fixture_file`；实时 HTTP 数据源可配置 JSON 字段路径或文本正则抽取。配置文件中的源、谓词、目标必须保持相互引用一致。

`configs/defaults.yaml` 是参数默认值，不是完整业务任务；它不含具体来源、谓词与目标，因此**不能直接作为 `--config` 的输入**。删除示例后，需要使用自己业务的配置启动。

## 命令

```bash
proofwatch validate --config /path/to/task.yaml
proofwatch run --config /path/to/task.yaml --state /path/to/state.sqlite --at 2026-01-01T00:00:00Z
proofwatch show --state /path/to/state.sqlite
proofwatch simulate --config /path/to/task.yaml --state /path/to/state.sqlite --checkpoints /path/to/checkpoints.yaml
```

`validate` 校验 YAML。`run` 依据给定时间推进单轮验证，保留未完成的多步探测。`show` 读取已经保存在 SQLite 中的状态。`simulate` 通过用户提供的检查点文件执行离线回放。

## 设计和适用边界

实现包含带来源和时间戳的证据、范围受限的覆盖记录、强 Kleene 三值逻辑、合取/析取支持路径、反例集合、可恢复的 resolve/fetch 探测、决策感知的有限预算调度，以及依赖目标的局部修订。访问失败、网页迁移和缺失字段不会自动构成否定证据；页面解析成功也不代表断言永远成立。

EVSI 和来源变化风险采用可配置近似，不能视为经过外部校准的真实性概率。论文中的 Qwen 模型权重、私有提示词、240 个项目的历史测试语料及专有实验环境不包含在本仓库内。若应用场景需要 LLM 自然语言证据抽取或高质量网页结构化解析，需单独接入相应服务。
