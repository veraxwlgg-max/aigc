# 端到端测试输入

本目录在本地保存用于端到端回归的 Word 副本和注入清单。真实论文及其派生副本可能包含未公开内容，因此 `*.docx` 和 `*.injections.json` 不提交到公开仓库。

使用 `scripts/inject_r02_format_issues.py` 可从一份本地 DOCX 生成包含 27 类 R02 格式残留的测试副本：

```bash
python3 检测流程/02_规则检测/scripts/inject_r02_format_issues.py \
  /path/to/source.docx \
  检测流程/02_规则检测/test_documents/markdown_injected.docx
```
