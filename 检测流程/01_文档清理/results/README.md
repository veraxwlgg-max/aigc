# 第一层测试结果

本目录保存第一层数据清理的测试结果。

- `test_summary.json`：可提交 GitHub 的合成单元测试汇总，不包含真实论文及其派生统计；
- `local/`：本地生成的 `*.cleaned.txt` 与 `*.cleaned.json`，可能含论文正文，已由 `.gitignore` 排除。

重新运行：

```bash
python3 -m unittest discover -s 检测流程/01_文档清理/tests -v
python3 检测流程/01_文档清理/scripts/clean_academic_document.py /path/to/paper.docx
```
