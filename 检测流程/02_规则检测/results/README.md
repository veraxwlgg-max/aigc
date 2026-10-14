# 第二层测试结果

本目录保存第二层规则检测的测试结果。

- `test_summary.json`：可提交 GitHub 的合成单元测试汇总，不包含真实论文及其派生统计；
- `local/`：本地生成的 JSONL、汇总 JSON、HTML 和可选 Word 报告，可能含论文正文，已由 `.gitignore` 排除。

重新运行：

```bash
python3 -m unittest discover -s 检测流程/02_规则检测/tests -v
python3 检测流程/02_规则检测/scripts/rule_detector.py \
  检测流程/01_文档清理/results/local/paper.cleaned.json

python3 检测流程/02_规则检测/scripts/validate_r02_injections.py \
  检测流程/02_规则检测/test_documents/markdown_injected.injections.json \
  检测流程/02_规则检测/results/local/markdown_injected.rules.jsonl \
  --output 检测流程/02_规则检测/results/local/r02_injection_validation.json
```
