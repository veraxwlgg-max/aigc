# 第二步 规则检测

本模块读取第一层生成的 `*.cleaned.json`，对摘要、正文和参考文献执行 R01–R05 规则。它只输出可疑证据，不计算 AI 率，也不设置规则权重。

## 规则

| ID | 名称 | 级别 |
|---|---|---|
| R01 | 模型或生成任务残留 | L3 |
| R02 | AI 回答格式与 Markdown 残留（27 类子信号） | L3 |
| R03 | 句式节奏过度规整 | L1 |
| R04 | 字面与语义冗余 | 单证据 L1，字面和语义同时命中为 L2 |
| R05 | 空泛评价与证据缺失 | L1 |

`[公式]` 仅用于追溯。检测时替换为等长空格，不参与短语匹配、句长、重复度或语义相似度计算。

R02 当前覆盖 Markdown front matter、两类标题、粗斜体/粗体/斜体/删除线源标记、引用与提示块、任务/项目/编号列表、图片、五类链接、围栏/行内代码、表格、水平线、脚注、原始 HTML、数学分隔符、转义符、连续空行及回答式开头/结尾。这里检测的是文本中真实残留的 `**字体**`、`*字体*`、行首 `- 第一点` 等 Markdown 字符，不检测 Word 自身的加粗或斜体样式。每类信号单独命中即为 L3；中文引号中的语法示例不触发。

## 输出

每篇论文默认生成三份结果：

- `*.rules.jsonl`：每行一个段落或句子，包含字符范围、规则命中、原始特征和公式跳过范围；
- `*.rules.summary.json`：文档级命中统计，同时列出每个实际 `signal` 的命中次数；
- `*.rules.review.html`：人工复核入口，浏览器直接打开；支持完整全文与“只看命中段落”切换。

HTML 会删除用于追溯的 `[公式]` 标记，命中文字和段后备注使用红色。考虑到 Word、WPS 和 LibreOffice 的中文字体替换并不一致，HTML 是默认且推荐的可视化格式。

确有 Word 交付需求时，可以额外传入 `--docx`；它属于兼容输出，不作为默认验收格式。

`no_rule_hit` 只表示没有规则证据，不能据此判定文本为人工撰写。

## 安装

默认的 JSON/HTML 流程只使用 Python 标准库，无需安装第三方依赖。

R04 语义重复或 `--docx` 兼容输出需要可选依赖：

```bash
python3 -m pip install -r 数据处理/02_规则检测/optional-requirements.txt
```

## 使用

不加载句向量模型时，R04 只运行字面重复检测：

```bash
python3 数据处理/02_规则检测/scripts/rule_detector.py \
  数据处理/01_数据清理/results/local/paper.cleaned.json
```

启用中文句向量模型：

```bash
python3 数据处理/02_规则检测/scripts/rule_detector.py \
  数据处理/01_数据清理/results/local/paper.cleaned.json \
  --embedding-model /path/to/bge-base-zh-v1.5
```

额外生成 Word 兼容版本：

```bash
python3 数据处理/02_规则检测/scripts/rule_detector.py \
  数据处理/01_数据清理/results/local/paper.cleaned.json \
  --docx
```

默认结果写入 `results/local/`。全文 JSONL、HTML 和 Word 报告只保存在本地；`results/test_summary.json` 保存不含论文正文的回归测试结果，可安全提交 GitHub。

语义模型未启用时，汇总文件会明确记录警告，不会把字面相似伪装成语义证据。
