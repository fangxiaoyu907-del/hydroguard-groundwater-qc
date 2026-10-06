# HydroGuard | 地下水监测数据智能质控

面向地下水及环境监测时序数据的 Python **离线批量质控工具**。结合时序特征、Isolation Forest、稳健统计与业务规则，完成异常标记、有限自动修复和可追溯报告输出。

这是一个可复现的个人作品展示项目，不是已部署的生产系统，也不属于大语言模型 Agent 或地下水位预测模型。示例均为程序生成的合成数据。

## 功能

- 接入 CSV、XLSX、XLS、JSON 或 JSON Lines，检查必需字段。
- 从监测数值构造原值、一阶差分、滚动中位数残差和局部偏离等特征；评估标签不参与特征构造。
- 支持 Isolation Forest 和 Robust Temporal 两种后端；融合模型分数、全局 MAD 和变化率分数。
- 标记缺失值、缺失/非法时间戳、重复时间戳、采样中断及传感器冻结迹象。
- 仅修复两侧有观测、时间连续的内部短缺失段，以及邻点正常的孤立高分尖峰；保留源行号、修改前后值与理由。
- 长缺失、边界缺失、跨采样中断缺失、缺失/非法/重复时间戳及长采样间隔进入人工复核。

冻结迹象和其他模型告警会输出到事件表，仍需业务人员解读；程序的 `approved` 表示未命中当前配置的结构性复核条件，不代表确认所有观测真实可靠。

## 快速开始

建议 Python 3.12。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run_demo.py
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python run_demo.py
```

演示生成两组各 720 条小时级观测，包含已知尖峰、短缺失和冻结片段；第二组额外包含重复时间戳，用于验证转人工流程。程序在 `outputs/normal` 和 `outputs/structural_risk` 写入结果。

处理自己的数据：

```powershell
.\.venv\Scripts\python.exe hydroguard.py --input your_data.xlsx --output outputs/your_run --timestamp-column timestamp --target-column water_level_m --backend isolation_forest
```

`--backend` 支持 `auto`、`isolation_forest`、`robust`。`auto` 在模型不可用时切换为稳健统计后端并记录原因；需要确认机器学习路径时，请显式选择 `isolation_forest`。

## 输入约定

| 字段 | 用途 |
| --- | --- |
| `timestamp` | 必需，能被 pandas 解析的时间戳 |
| `water_level_m` | 必需，地下水位数值；字段可通过命令行改名 |
| `temperature_c`、`pumping_rate_m3h` 等 | 可选数值特征 |
| `well_id` | 示例元数据；不参与模型，不自动按井分组 |
| `injected_anomaly` | 可选评估标签，布尔值或 0/1；不会进入模型 |

一次运行应包含同一井点、相同采样频率的数据。多井数据请先拆分处理。默认期望采样间隔为 60 分钟，超过 1.6 倍会触发复核；其他频率可在 `HydroGuardConfig` 调整 `expected_interval_minutes`。自定义标签列可在配置中设置 `label_column`。

XLSX 由 openpyxl 读取；旧版 XLS 需另安装 xlrd：`python -m pip install xlrd`。评估标签不完整或格式不合法时跳过指标计算。

## 输出与审计

| 文件 | 内容 |
| --- | --- |
| `scored_data.csv` | 模型/融合分数、是否异常及主要异常类型 |
| `repaired_data.csv` | 有限自动修复后的数据 |
| `anomaly_events.csv` | 可同时包含同一观测的多种异常、原始文件行号和原因 |
| `repair_audit.csv` | 修改前值、后值、动作与理由 |
| `quality_report.md` | 质控摘要和复核原因 |
| `demo_metrics.json` | 有标签时的合成数据诊断指标 |
| `run_state.json` | 步骤、问题、修复及输出路径 |

CSV 的 `source_row` 是带一行表头时的文件行号；其他格式可将其理解为从 2 开始的原始记录编号。运行输出可能包含原始数据或本机路径，因此默认不会提交到 GitHub。

## 验证和限制

测试覆盖输出生成、结构风险分流、必要字段、无效时间戳及源行号、边界与跨中断缺失保护、评估标签防泄漏和显式 Isolation Forest 后端。验证摘要见 [docs/VALIDATION.md](docs/VALIDATION.md)。

- 居中滚动窗口会使用同一批内的后续观测，模型也在同一批数据拟合并评分；这不是因果在线检测或独立测试集预测。
- 合成样例的 Precision / Recall / F1 只用于检查流程，不代表真实现场性能，不作为业务收益证明。
- 不自动确认地质成因，不替代设备检查、专业判断或人工复核。异常分数和尖峰阈值是可调启发式，不是已校准的风险概率。
- 默认线性插值按行序进行；它仅用于时间连续的内部短缺失，不适合任意不等间隔数据。
- 真实使用前需要按井点、设备和采样频率标定阈值，并使用人工标注历史数据进行时序回测。

## 目录

```text
hydroguard.py                 核心质控流程与命令行入口
run_demo.py                   固定随机种子的合成数据与演示
requirements.txt             运行依赖
tests/test_hydroguard.py      自动测试
examples/                    公开合成样例
docs/VALIDATION.md           验证摘要
```

## 展示与复用

本仓库用于公开展示代码与方法，未选择开放源代码许可证。若需复用或再分发，请先联系仓库作者。项目开发日期及履历应以实际记录为准，示例数据时间戳不代表开发时间。
