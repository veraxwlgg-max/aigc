from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import rule_detector as detector
import inject_r02_format_issues as injector
import validate_r02_injections as validator


def cleaned_document(paragraphs: list[tuple[str, str]]) -> dict:
    blocks = []
    clean_parts: list[str] = []
    cursor = 0
    for index, (section, text) in enumerate(paragraphs):
        if clean_parts:
            clean_parts.append("\n\n")
            cursor += 2
        start = cursor
        clean_parts.append(text)
        cursor += len(text)
        blocks.append(
            {
                "block_id": f"b{index:06d}",
                "section_type": section,
                "block_type": "paragraph" if section != "references" else "reference",
                "text": text,
                "included": True,
                "exclude_reason": None,
                "clean_start": start,
                "clean_end": cursor,
            }
        )
    return {
        "source": {"path": "/tmp/paper.docx", "format": "docx", "sha256": "test"},
        "clean_text": "".join(clean_parts),
        "blocks": blocks,
        "warnings": [],
    }


class RuleTests(unittest.TestCase):
    def test_injection_targets_are_selected_without_text_anchors(self) -> None:
        class Style:
            name = "Normal"

        class Paragraph:
            def __init__(self, text: str) -> None:
                self.text = text
                self.style = Style()

        class Document:
            paragraphs = [
                Paragraph(f"第{index}段" + "这是用于合成测试的正文内容。" * 10)
                for index in range(40)
            ]

        targets = injector.select_target_paragraphs(Document(), 27)

        self.assertEqual(len(targets), 27)
        self.assertEqual(len({index for index, _ in targets}), 27)

    def test_injection_targets_stop_before_references(self) -> None:
        class Style:
            name = "Normal"

        class Paragraph:
            def __init__(self, text: str) -> None:
                self.text = text
                self.style = Style()

        body = [Paragraph("这是用于合成测试的正文内容。" * 10) for _ in range(27)]
        references = [Paragraph("参考文献"), Paragraph("文献条目内容。" * 20)]

        class Document:
            paragraphs = body + references

        targets = injector.select_target_paragraphs(Document(), 27)

        self.assertTrue(all(index < 27 for index, _ in targets))

    def test_formula_marker_is_masked_and_offsets_are_preserved(self) -> None:
        data = cleaned_document(
            [("body", "参数[公式]越大时，模型显著提高了预测性能。")]
        )
        paragraphs, records, summary = detector.detect_document(data)
        paragraph = paragraphs[0]

        self.assertEqual(len(paragraph.analysis_text), len(paragraph.text))
        self.assertNotIn("[公式]", paragraph.analysis_text)
        self.assertEqual(summary["formula_placeholder_count"], 1)
        sentence_record = next(item for item in records if item["unit_type"] == "sentence")
        self.assertEqual(sentence_record["skipped_spans"][0]["char_start"], 2)
        self.assertFalse(any("formula" in str(hit).lower() for hit in sentence_record["hits"]))

    def test_r01_direct_residue_is_l3_but_quoted_example_is_protected(self) -> None:
        data = cleaned_document(
            [
                ("body", "作为一个AI语言模型，我将根据您的要求生成内容。"),
                ("body", "文中引用了“作为一个AI语言模型”这一典型表达作为分析样例。"),
            ]
        )
        _, records, _ = detector.detect_document(data)
        sentences = [item for item in records if item["unit_type"] == "sentence"]

        self.assertEqual(sentences[0]["max_level"], "L3")
        self.assertTrue(any(hit["rule_id"] == "R01" for hit in sentences[0]["hits"]))
        self.assertFalse(any(hit["rule_id"] == "R01" for hit in sentences[1]["hits"]))

    def test_r02_single_markdown_signal_is_l3(self) -> None:
        data = cleaned_document([("body", "**优化后的正文** 这是一段说明文字。")])
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")

        self.assertEqual(paragraph["max_level"], "L3")
        self.assertTrue(any(hit["rule_id"] == "R02" for hit in paragraph["hits"]))
        self.assertTrue(
            any("Markdown 粗体标记" in hit["explanation"] for hit in paragraph["hits"])
        )

    def test_r02_detects_extended_markdown_residues(self) -> None:
        cases = {
            "markdown_front_matter": "---\ntitle: 格式测试\n---\n正文内容。",
            "markdown_heading": "### 格式测试标题",
            "markdown_setext_heading": "格式测试标题\n---\n正文内容。",
            "markdown_bold_italic": "这是***粗斜体残留***测试。",
            "markdown_bold": "这是**粗体残留**测试。",
            "markdown_italic": "这是*斜体残留*测试。",
            "markdown_strikethrough": "这是~~删除线残留~~测试。",
            "markdown_blockquote": "> [!NOTE] 这是提示块。",
            "markdown_task_list": "- [x] 已完成格式测试。",
            "bullet_list": "- 这是项目列表。",
            "numbered_list": "1. 这是编号列表。",
            "markdown_image": "图示为![模型结构](figure.png)。",
            "markdown_link": "参见[项目主页](https://example.org)。",
            "markdown_reference_link": "参见[项目主页][project]。",
            "markdown_link_definition": "[project]: https://example.org/project",
            "markdown_autolink": "地址为<https://example.org>。",
            "markdown_fenced_code": "```python\nprint(1)\n```",
            "markdown_inline_code": "调用`detect()`完成检测。",
            "markdown_table": "| 字段 | 数值 |\n| --- | --- |\n| A | 1 |",
            "markdown_horizontal_rule": "正文说明。\n***\n继续说明。",
            "markdown_footnote": "该结论已有说明[^note]。",
            "markdown_raw_html": "<details><summary>说明</summary>内容</details>",
            "markdown_math_delimiter": "目标函数为$L=N/D$。",
            "markdown_escape_sequence": r"这是\*转义星号\*残留。",
            "frequent_blank_lines": "第一段。\n\n第二段。",
            "answer_heading": "以下是优化后的正文。",
            "closing_suggestion": "正文到此结束。如需进一步说明，可以继续补充。",
        }
        for expected_signal, text in cases.items():
            with self.subTest(signal=expected_signal):
                data = cleaned_document([("body", text)])
                _, records, _ = detector.detect_document(data)
                paragraph = next(
                    item for item in records if item["unit_type"] == "paragraph"
                )
                signals = {hit["signal"] for hit in paragraph["hits"]}
                self.assertIn(expected_signal, signals)

    def test_r02_does_not_confuse_academic_citations_or_identifiers(self) -> None:
        data = cleaned_document(
            [
                (
                    "body",
                    "现有研究[1][2]采用 fluency_en、ad_en 和 model_name 作为字段名。",
                )
            ]
        )
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        signals = {hit["signal"] for hit in paragraph["hits"] if hit["rule_id"] == "R02"}
        self.assertNotIn("markdown_reference_link", signals)
        self.assertNotIn("markdown_italic", signals)

    def test_r02_ignores_markdown_example_inside_chinese_quotes(self) -> None:
        data = cleaned_document(
            [("body", "本文把“**粗体**”作为 Markdown 语法示例，而不是正文格式。")]
        )
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        self.assertFalse(any(hit["rule_id"] == "R02" for hit in paragraph["hits"]))

    def test_reference_number_is_not_r02_list(self) -> None:
        data = cleaned_document(
            [("references", "1. Author. Paper title. Journal, 2020.")]
        )
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        self.assertFalse(any(hit["rule_id"] == "R02" for hit in paragraph["hits"]))

    def test_decimal_url_and_reference_periods_do_not_over_split(self) -> None:
        data = cleaned_document(
            [
                ("body", "模型的 AUROC 为 0.9554，结果保持稳定。详情见 https://example.org/a.html。"),
                ("references", "A. Author. Paper title. Journal, 2020. https://example.org/paper."),
            ]
        )
        paragraphs, _, _ = detector.detect_document(data)
        self.assertEqual(len(paragraphs[0].sentences), 2)
        self.assertEqual(len(paragraphs[1].sentences), 1)

    def test_r03_detects_multiple_regularity_signals(self) -> None:
        text = (
            "模型用于处理数据，实验用于验证效果。"
            "方法用于提取特征，结果用于评估性能。"
            "系统用于管理任务，模块用于生成输出。"
            "算法用于计算得分，指标用于衡量差异。"
            "流程用于组织步骤，规则用于约束行为。"
        )
        data = cleaned_document([("body", text)])
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        r03 = [hit for hit in paragraph["hits"] if hit["rule_id"] == "R03"]

        self.assertEqual(len(r03), 1)
        self.assertEqual(r03[0]["level"], "L1")

    def test_r04_combined_evidence_is_l2_with_semantic_scorer(self) -> None:
        text = (
            "该方法能够稳定提升模型的整体预测性能。"
            "该方法能够稳定提升模型的综合预测表现。"
        )
        data = cleaned_document([("body", text)])
        scorer = lambda sentences: [0.96]
        _, records, _ = detector.detect_document(data, semantic_scorer=scorer)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        r04 = [hit for hit in paragraph["hits"] if hit["rule_id"] == "R04"]

        self.assertEqual(len(r04), 1)
        self.assertEqual(r04[0]["level"], "L2")

    def test_r04_protects_parallel_numeric_reporting(self) -> None:
        text = (
            "A1 的平均冲突分别为 0.1153 和 0.1202，高冲突占比分别为 10.85% 和 12.48%；"
            "A2 的平均冲突分别为 0.1326 和 0.1330，高冲突占比分别为 10.44% 和 10.82%。"
        )
        data = cleaned_document([("body", text)])
        _, records, _ = detector.detect_document(data)
        paragraph = next(item for item in records if item["unit_type"] == "paragraph")
        self.assertFalse(any(hit["rule_id"] == "R04" for hit in paragraph["hits"]))

    def test_r05_uses_neighboring_evidence(self) -> None:
        data = cleaned_document(
            [
                ("body", "该方法显著提高了系统性能。"),
                ("body", "该方法显著提高了准确率。测试准确率为95%。"),
            ]
        )
        _, records, _ = detector.detect_document(data)
        sentences = [item for item in records if item["unit_type"] == "sentence"]

        self.assertTrue(any(hit["rule_id"] == "R05" for hit in sentences[0]["hits"]))
        self.assertFalse(any(hit["rule_id"] == "R05" for hit in sentences[1]["hits"]))


class OutputTests(unittest.TestCase):
    def test_default_outputs_include_jsonl_summary_and_red_html(self) -> None:
        data = cleaned_document([("body", "作为一个AI语言模型，以下是根据您的要求生成的正文。")])
        paragraphs, records, summary = detector.detect_document(data)
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            jsonl = directory / "paper.rules.jsonl"
            summary_path = directory / "paper.rules.summary.json"
            docx = directory / "paper.rules.review.docx"
            detector.write_outputs(
                records,
                summary,
                paragraphs,
                jsonl,
                summary_path,
                docx,
                create_docx=False,
            )
            html_path = directory / "paper.rules.review.html"

            loaded = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(item["max_level"] == "L3" for item in loaded))
            loaded_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded_summary["ai_rate"], None)
            self.assertEqual(loaded_summary["signal_counts"]["ai_identity"], 1)
            self.assertTrue(html_path.is_file())
            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("作为一个AI", html_text)
            self.assertIn('class="hit l3"', html_text)
            self.assertIn("检测备注", html_text)
            self.assertEqual(html_text.count("检测备注："), 1)
            self.assertNotIn("[公式]", html_text)
            self.assertFalse(docx.exists())

    def test_r02_injection_validator_reports_missing_signal(self) -> None:
        manifest = {"expected_signals": ["markdown_bold", "bullet_list"]}
        records = [
            {
                "hits": [
                    {
                        "rule_id": "R02",
                        "signal": "markdown_bold",
                    }
                ]
            }
        ]
        report = validator.validate(manifest, records)

        self.assertFalse(report["passed"])
        self.assertEqual(report["missing_signals"], ["bullet_list"])
        self.assertEqual(report["r02_event_count"], 1)
        self.assertNotIn("text", report)


if __name__ == "__main__":
    unittest.main()
