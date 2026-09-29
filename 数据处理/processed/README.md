# 已处理数据

## `csl_cs_20_human_abstracts.csv`

用于跑通数据流程的20篇中文计算机科学与技术论文摘要。

- 来源：[CSL: A Large-scale Chinese Scientific Literature Dataset](https://github.com/ydli-ai/CSL)
- 原始切分：CSL benchmark `train`
- 学科：计算机科学与技术
- 长度范围：150—600 个中文字符
- 抽样数量：20
- 随机种子：`20260929`
- 许可：Apache-2.0（以 CSL 官方仓库当前许可文件为准）
- 抽样脚本：`../scripts/sample_csl_abstracts.py`

这20篇摘要仅用于验证数据字段、提示词和配对生成流程，不能用于评价检测模型性能。

## `csl_cs_20_pilot_2models.csv`

由上述20篇人类摘要与两个模型的配对生成结果合并而成，共100条：

- `human`：20条；
- `ai_full`：40条（每个模型20条）；
- `ai_polish_light`：40条（每个模型20条）；
- 生成模型：`gpt-5.6-luna`、`claude-sonnet-5`；
- `split=pilot` 表示仅用于流程验证，尚未划分训练集、验证集和测试集。

原始 API 响应保存在被 Git 忽略的 `../generated/*.jsonl` 中。合并脚本为
`../scripts/build_pilot_dataset.py`。国产模型线路暂不可用，恢复后应按同一提示词补齐，
不要把当前100条误称为原计划的三模型140条数据集。
