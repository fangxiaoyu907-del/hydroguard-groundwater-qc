# 本地验证记录

验证日期：2026-10-07。此日期是发布准备核验日期，不代表历史项目开发时间。

## 已完成

在独立 Windows / Python 3.12.14 环境安装完整依赖后执行：

```text
python -m unittest discover -s tests -v
Ran 8 tests
OK
```

8 项全部通过，无跳过：演示输出、重复时间戳转人工、必需字段检查、无效/缺失时间戳及源行号、首尾缺失保护、跨采样中断缺失保护、整数评估标签防泄漏、显式 Isolation Forest 后端。

验证环境：numpy 2.5.3、pandas 2.3.3、scikit-learn 1.9.1、openpyxl 3.1.5、scipy 1.18.1。演示实际使用 IsolationForest，没有切换至统计后端。

两组合成数据运行结果：

| 样例 | 记录数 | 异常标记数 | 自动修复数 | 处置 |
| --- | --- | --- | --- | --- |
| normal | 720 | 11 | 5 | approved_with_repairs |
| structural_risk | 720 | 14 | 5 | needs_human_review |

以上是固定合成样例与当前配置的运行结果，不代表现场性能。

## 待验证

仓库附有自动测试工作流，安装完整依赖后会强制检查 scikit-learn、执行全部测试与合成演示。在首次远程运行成功前，不宣称 GitHub Actions 已通过。

工作流使用 GitHub 官方 [checkout](https://github.com/actions/checkout) 和 [setup-python](https://github.com/actions/setup-python)。

## 指标解释

示例标签是合成异常注入标记；检测在同批数据拟合与评分，不是独立时间测试集。生成的 Precision、Recall、F1 仅用于诊断演示流程，不能用于宣称真实现场泛化性能或业务收益。
