#!/usr/bin/env python3
"""学术论文 AIGC 规则检测器。

输入为第一层产生的 ``*.cleaned.json``，输出：

1. ``*.rules.jsonl``：逐段、逐句的机器可读结果；
2. ``*.rules.summary.json``：文档级规则命中统计；
3. ``*.rules.review.html``：浏览器可直接打开的红色标注全文报告。

Word 报告仅在显式传入 ``--docx`` 时生成；默认使用 HTML，避免跨软件的
中文字体替换差异。

本层只输出证据，不计算 AI 率，也不设置规则权重。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import html
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Callable, Iterable, Sequence


RULE_VERSION = "1.0"
SCHEMA_VERSION = "1.0"
FORMULA_MARKER = "[公式]"
LEVEL_RANK = {None: 0, "L1": 1, "L2": 2, "L3": 3}
LEVEL_LABEL = {
    "L1": "辅助证据",
    "L2": "强可疑证据",
    "L3": "直接证据",
}
SECTION_LABEL = {
    "abstract": "摘要",
    "body": "正文",
    "references": "参考文献",
}

FORMULA_RE = re.compile(re.escape(FORMULA_MARKER))

R01_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ai_identity",
        re.compile(
            r"(?:作为|身为)(?:一个|一名)?\s*(?:AI|人工智能|语言模型|大语言模型)(?:助手|模型)?",
            re.IGNORECASE,
        ),
    ),
    (
        "generation_task_residue",
        re.compile(
            r"(?:下面|以下)(?:为|是)(?:我为(?:您|你))?"
            r"(?:生成|撰写|整理|修改|改写|润色|优化)(?:后的|的)?"
            r"(?:内容|正文|段落|摘要|文本)?",
            re.IGNORECASE,
        ),
    ),
    (
        "polishing_residue",
        re.compile(
            r"以下是(?:根据[^\n。]{0,24})?(?:润色|修改|改写|优化)(?:后的)?"
            r"(?:内容|正文|段落|摘要|文本)",
            re.IGNORECASE,
        ),
    ),
    (
        "user_request_residue",
        re.compile(r"根据(?:您|你|用户)的(?:要求|需求|指示)", re.IGNORECASE),
    ),
    (
        "chat_role_residue",
        re.compile(r"(?<![A-Za-z])(?:User|Assistant|System)\s*[:：]", re.IGNORECASE),
    ),
    (
        "assistant_refusal_residue",
        re.compile(r"我无法(?:提供|完成|帮助|满足)[^\n。！？]{0,36}", re.IGNORECASE),
    ),
]

R02_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "markdown_front_matter",
        re.compile(
            r"(?ms)^\s*---\s*\n(?:[A-Za-z][^:\n]{0,80}:\s*[^\n]*\n){1,20}---\s*(?:\n|$)"
        ),
    ),
    ("markdown_heading", re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")),
    (
        "markdown_setext_heading",
        re.compile(r"(?m)^\s*[^\n]{1,160}\n\s{0,3}(?:={3,}|-{3,})\s*$"),
    ),
    (
        "markdown_bold_italic",
        re.compile(
            r"(?<!\*)\*\*\*(?=\S)[^*\n]{1,200}(?<=\S)\*\*\*(?!\*)|"
            r"(?<![A-Za-z0-9_])___(?=\S)[^_\n]{1,200}(?<=\S)___(?![A-Za-z0-9_])"
        ),
    ),
    (
        "markdown_bold",
        re.compile(
            r"(?<!\*)\*\*(?!\*)(?=\S)[^*\n]{1,200}(?<=\S)\*\*(?!\*)|"
            r"(?<![A-Za-z0-9_])__(?!_)(?=\S)[^_\n]{1,200}(?<=\S)__(?![A-Za-z0-9_])"
        ),
    ),
    (
        "markdown_italic",
        re.compile(
            r"(?<!\*)\*(?!\*)(?=\S)[^*\n]{1,200}(?<=\S)\*(?!\*)|"
            r"(?<![A-Za-z0-9_])_(?!_)(?=\S)[^_\n]{1,200}(?<=\S)_(?![A-Za-z0-9_])"
        ),
    ),
    (
        "markdown_strikethrough",
        re.compile(r"(?<!~)~~(?=\S)[^~\n]{1,200}(?<=\S)~~(?!~)"),
    ),
    ("markdown_blockquote", re.compile(r"(?m)^\s{0,3}>\s?(?:\[![A-Za-z]+\]\s*)?\S")),
    (
        "markdown_task_list",
        re.compile(r"(?m)^\s*[-+*]\s+\[(?: |x|X)\]\s+\S"),
    ),
    (
        "bullet_list",
        re.compile(r"(?m)^\s*[-+*•●▪]\s+(?!\[(?: |x|X)\]\s)\S"),
    ),
    (
        "numbered_list",
        re.compile(r"(?m)^\s*(?:\d{1,3}[.)、]|[一二三四五六七八九十]+[、.)）])\s*\S"),
    ),
    (
        "markdown_image",
        re.compile(r"!\[[^\]\n]{0,200}\]\(\s*(?:<[^>\n]+>|[^)\n]+)\s*\)"),
    ),
    (
        "markdown_link",
        re.compile(r"(?<!!)\[[^\]\n]{1,200}\]\(\s*(?:<[^>\n]+>|[^)\n]+)\s*\)"),
    ),
    (
        "markdown_reference_link",
        # 第二组标签至少包含字母、汉字、下划线或连字符，避免把论文中的
        # 相邻数字引文（如 [1][2]）误判为 Markdown 引用式链接。
        re.compile(
            r"(?<!!)\[[^\]\n]{1,200}\]"
            r"\[(?=[^\]\n]*[A-Za-z_\-\u4e00-\u9fff])[^\]\n]{1,100}\]"
        ),
    ),
    (
        "markdown_link_definition",
        re.compile(r"(?m)^\s{0,3}\[[^\]\n]{1,100}\]:\s*(?:https?://|<https?://)\S+"),
    ),
    (
        "markdown_autolink",
        re.compile(r"<https?://[^>\s]+>", re.IGNORECASE),
    ),
    (
        "markdown_fenced_code",
        re.compile(r"(?m)^\s{0,3}(?:`{3,}|~{3,})[A-Za-z0-9_+.-]*\s*$"),
    ),
    (
        "markdown_inline_code",
        re.compile(r"(?<!`)`(?!`)(?=\S)[^`\n]{1,400}(?<=\S)`(?!`)"),
    ),
    (
        "markdown_table",
        re.compile(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"),
    ),
    (
        "markdown_horizontal_rule",
        re.compile(r"(?m)^\s{0,3}(?:(?:\*\s*){3,}|(?:_\s*){3,}|(?:-\s*){3,})$"),
    ),
    (
        "markdown_footnote",
        re.compile(r"\[\^[^\]\n]{1,80}\](?::\s*\S)?"),
    ),
    (
        "markdown_raw_html",
        re.compile(
            r"<!--[\s\S]{0,1000}?-->|</?(?:div|span|details|summary|br|p|table|thead|tbody|tr|th|td|ul|ol|li|h[1-6]|pre|code|blockquote)\b[^>\n]*>",
            re.IGNORECASE,
        ),
    ),
    (
        "markdown_math_delimiter",
        re.compile(r"(?<!\\)\$\$[\s\S]{1,1000}?\$\$|(?<!\\)\$(?!\$)(?=\S)[^$\n]{1,300}(?<=\S)\$(?!\$)"),
    ),
    (
        "markdown_escape_sequence",
        re.compile(r"\\[*_#\[\]()>~`-]"),
    ),
    ("frequent_blank_lines", re.compile(r"(?:\n\s*){2,}")),
    (
        "answer_heading",
        re.compile(
            r"^\s*(?:以下是|下面是|回答如下|修改如下|优化如下|"
            r"润色后的|改写后的|根据您的要求)"
        ),
    ),
    (
        "closing_suggestion",
        re.compile(
            r"(?:如需|如果您需要|如果你需要|希望以上内容|"
            r"还可以继续|可以进一步为您)[^\n。！？]{0,80}[。！？!]?\s*$"
        ),
    ),
]

R02_EXPLANATIONS = {
    "markdown_front_matter": "检测到 Markdown/YAML front matter 分隔块。",
    "markdown_heading": "检测到未清理的 Markdown 标题符号。",
    "markdown_setext_heading": "检测到 Markdown Setext 标题下划线。",
    "markdown_bold_italic": "检测到未清理的 Markdown 粗斜体标记。",
    "markdown_bold": "检测到未清理的 Markdown 粗体标记。",
    "markdown_italic": "检测到未清理的 Markdown 斜体标记。",
    "markdown_strikethrough": "检测到未清理的 Markdown 删除线标记。",
    "markdown_blockquote": "检测到 Markdown 引用或提示块。",
    "markdown_task_list": "检测到 Markdown 任务列表。",
    "bullet_list": "检测到 AI 回答式项目列表。",
    "numbered_list": "检测到 AI 回答式编号列表。",
    "markdown_image": "检测到 Markdown 图片语法。",
    "markdown_link": "检测到 Markdown 行内链接语法。",
    "markdown_reference_link": "检测到 Markdown 引用式链接。",
    "markdown_link_definition": "检测到 Markdown 链接定义。",
    "markdown_autolink": "检测到 Markdown 自动链接。",
    "markdown_fenced_code": "检测到 Markdown 围栏代码块。",
    "markdown_inline_code": "检测到 Markdown 行内代码标记。",
    "markdown_table": "检测到 Markdown 表格分隔行。",
    "markdown_horizontal_rule": "检测到 Markdown 水平分隔线。",
    "markdown_footnote": "检测到 Markdown 脚注语法。",
    "markdown_raw_html": "检测到 Markdown 中的原始 HTML 标签或注释。",
    "markdown_math_delimiter": "检测到 Markdown/LaTeX 数学分隔符。",
    "markdown_escape_sequence": "检测到 Markdown 转义标记。",
    "frequent_blank_lines": "检测到连续空行造成的回答式分块。",
    "answer_heading": "检测到回答式开头或小标题。",
    "closing_suggestion": "检测到面向用户的结尾建议。",
}

EVALUATION_RE = re.compile(
    r"显著(?:提高|提升|改善|降低|优于)|"
    r"有效(?:提高|提升|改善|降低|增强|缓解)|"
    r"明显(?:提高|提升|改善|降低|优于)|"
    r"取得了?(?:良好|优异|较好|理想|显著)的?(?:效果|性能|成果)|"
    r"具有(?:重要意义|重要价值|广阔前景|较高价值)|"
    r"实验结果表明|结果表明|验证了[^\n。！？]{0,24}(?:有效性|优越性|可行性)"
)

EVIDENCE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|倍|分|秒|ms|s|MB|GB|TB|FLOPs?|Token|"
    r"个|条|组|项|次|年|月)?|"
    r"(?:AUROC|AUC|F1|MAE|RMSE|BLEU|ROUGE|Accuracy|Precision|Recall|Loss|"
    r"准确率|精确率|召回率|错误率|置信区间|标准差|显著性)|"
    r"(?:相比|较|高于|低于|增加了|降低了|提升了|减少了)|"
    r"\[[0-9,\-\s]+\]",
    re.IGNORECASE,
)

GENERIC_PREFIXES = {
    "本文提出",
    "本文采用",
    "实验结果",
    "结果表明",
    "研究结果",
    "本文研究",
    "本节介绍",
}

QUOTE_PATTERNS = [
    re.compile(r"“[^”\n]{1,800}”"),
    re.compile(r"「[^」\n]{1,800}」"),
    re.compile(r"『[^』\n]{1,800}』"),
]

CODE_PROTECTED_PATTERNS = [
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]{1,400}`"),
]

PROTECTED_PATTERNS = QUOTE_PATTERNS + CODE_PROTECTED_PATTERNS


class RuleDetectionError(RuntimeError):
    pass


@dataclass
class SentenceUnit:
    unit_id: str
    paragraph_id: str
    section_type: str
    char_start: int
    char_end: int
    text: str
    analysis_text: str
    skipped_spans: list[dict[str, Any]] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    raw_features: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParagraphUnit:
    unit_id: str
    section_type: str
    char_start: int
    char_end: int
    text: str
    analysis_text: str
    source_block_id: str
    skipped_spans: list[dict[str, Any]] = field(default_factory=list)
    sentences: list[SentenceUnit] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    raw_features: dict[str, Any] = field(default_factory=dict)


def _formula_spans(text: str, base_offset: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_formula",
            "char_start": base_offset + match.start(),
            "char_end": base_offset + match.end(),
        }
        for match in FORMULA_RE.finditer(text)
    ]


def mask_formula_markers(text: str) -> str:
    """用等长空格屏蔽公式占位符，保持字符偏移不变。"""
    return FORMULA_RE.sub(lambda match: " " * len(match.group(0)), text)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _period_is_sentence_end(text: str, index: int) -> bool:
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous.isdigit() and following.isdigit():
        return False
    token_start = index
    while token_start > 0 and not text[token_start - 1].isspace():
        token_start -= 1
    token = text[token_start : index + 1].lower()
    if "http://" in token or "https://" in token or token.startswith("www."):
        return False
    if following and not following.isspace() and following not in "”’」』》)]}":
        return False
    if re.search(r"(?:^|\s)(?:[A-Z]\.){1,5}[A-Z]?\.$", text[max(0, index - 16) : index + 1]):
        return False
    return True


def _sentence_spans(text: str, keep_whole: bool = False) -> list[tuple[int, int]]:
    if keep_whole:
        start, end = _trim_span(text, 0, len(text))
        return [(start, end)] if end > start else []

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    strong_endings = "。！？；!?;"
    closers = "”’」』》)]}"
    while index < len(text):
        char = text[index]
        is_ending = char in strong_endings or (
            char == "." and _period_is_sentence_end(text, index)
        )
        if not is_ending:
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end] in strong_endings + "." + closers:
            if text[end] == "." and not _period_is_sentence_end(text, end):
                break
            end += 1
        trimmed = _trim_span(text, start, end)
        if trimmed[1] > trimmed[0]:
            spans.append(trimmed)
        start = end
        index = end

    tail = _trim_span(text, start, len(text))
    if tail[1] > tail[0]:
        spans.append(tail)
    if not spans and text.strip():
        spans = [_trim_span(text, 0, len(text))]
    return spans


def split_sentences(paragraph: ParagraphUnit) -> list[SentenceUnit]:
    spans = _sentence_spans(
        paragraph.analysis_text,
        keep_whole=paragraph.section_type == "references",
    )

    sentences: list[SentenceUnit] = []
    for index, (local_start, local_end) in enumerate(spans, start=1):
        absolute_start = paragraph.char_start + local_start
        absolute_end = paragraph.char_start + local_end
        skipped = [
            span
            for span in paragraph.skipped_spans
            if span["char_start"] < absolute_end and span["char_end"] > absolute_start
        ]
        sentences.append(
            SentenceUnit(
                unit_id=f"{paragraph.unit_id}_s{index:03d}",
                paragraph_id=paragraph.unit_id,
                section_type=paragraph.section_type,
                char_start=absolute_start,
                char_end=absolute_end,
                text=paragraph.text[local_start:local_end],
                analysis_text=paragraph.analysis_text[local_start:local_end],
                skipped_spans=skipped,
            )
        )
    return sentences


def build_units(cleaned: dict[str, Any]) -> list[ParagraphUnit]:
    paragraphs: list[ParagraphUnit] = []
    included_blocks = [block for block in cleaned.get("blocks", []) if block.get("included")]
    for index, block in enumerate(included_blocks, start=1):
        text = str(block.get("text", ""))
        if not text.strip():
            continue
        start = block.get("clean_start")
        end = block.get("clean_end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise RuleDetectionError("清洗 JSON 缺少有效的 clean_start/clean_end。")
        paragraph = ParagraphUnit(
            unit_id=f"p{index:04d}",
            section_type=str(block.get("section_type", "body")),
            char_start=start,
            char_end=end,
            text=text,
            analysis_text=mask_formula_markers(text),
            source_block_id=str(block.get("block_id", "")),
            skipped_spans=_formula_spans(text, start),
        )
        paragraph.sentences = split_sentences(paragraph)
        paragraphs.append(paragraph)
    if not paragraphs:
        raise RuleDetectionError("清洗 JSON 中没有可检测文本块。")
    return paragraphs


def spans_for_patterns(
    text: str, patterns: Sequence[re.Pattern[str]]
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return sorted(spans)


def protected_spans(text: str) -> list[tuple[int, int]]:
    return spans_for_patterns(text, PROTECTED_PATTERNS)


def quoted_spans(text: str) -> list[tuple[int, int]]:
    return spans_for_patterns(text, QUOTE_PATTERNS)


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(left < end and right > start for left, right in spans)


def _new_hit(
    event_id: str,
    rule_id: str,
    level: str,
    signal: str,
    explanation: str,
    spans: Sequence[tuple[int, int]] = (),
    matched_text: str | None = None,
    features: dict[str, Any] | None = None,
    context_sentence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "rule_id": rule_id,
        "level": level,
        "signal": signal,
        "explanation": explanation,
        "matched_text": matched_text,
        "spans": [
            {"char_start": start, "char_end": end} for start, end in spans
        ],
        "context_sentence_ids": list(context_sentence_ids),
        "features": features or {},
    }


class EventFactory:
    def __init__(self) -> None:
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"e{self._counter:06d}"


def detect_r01(sentence: SentenceUnit, factory: EventFactory) -> None:
    protected = protected_spans(sentence.analysis_text)
    for signal, pattern in R01_PATTERNS:
        for match in pattern.finditer(sentence.analysis_text):
            if _overlaps(match.start(), match.end(), protected):
                continue
            absolute = (
                sentence.char_start + match.start(),
                sentence.char_start + match.end(),
            )
            sentence.hits.append(
                _new_hit(
                    factory.next_id(),
                    "R01",
                    "L3",
                    signal,
                    "检测到模型身份、生成任务或对话角色残留。",
                    [absolute],
                    match.group(0),
                )
            )


def detect_r02(paragraph: ParagraphUnit, factory: EventFactory) -> None:
    # 引号中的 Markdown 语法可能只是论文举例；代码标记本身则是本规则的检测对象。
    protected = quoted_spans(paragraph.analysis_text)
    for signal, pattern in R02_PATTERNS:
        if paragraph.section_type == "references" and signal == "numbered_list":
            continue
        for match in pattern.finditer(paragraph.analysis_text):
            if _overlaps(match.start(), match.end(), protected):
                continue
            absolute = (
                paragraph.char_start + match.start(),
                paragraph.char_start + match.end(),
            )
            paragraph.hits.append(
                _new_hit(
                    factory.next_id(),
                    "R02",
                    "L3",
                    signal,
                    R02_EXPLANATIONS[signal],
                    [absolute],
                    match.group(0),
                )
            )


def _meaningful_length(text: str) -> int:
    return sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _coefficient_of_variation(values: Sequence[int]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    if mean == 0:
        return None
    return statistics.pstdev(values) / mean


def _punctuation_skeleton(text: str) -> str:
    return "".join(char for char in text if char in "，,；;：:。！？!?" )


def detect_r03(paragraph: ParagraphUnit, factory: EventFactory) -> None:
    sentences = [item for item in paragraph.sentences if _meaningful_length(item.analysis_text) >= 8]
    lengths = [_meaningful_length(item.analysis_text) for item in sentences]
    sentence_cv = _coefficient_of_variation(lengths)

    skeletons = [_punctuation_skeleton(item.analysis_text) for item in sentences]
    skeleton_counts = Counter(skeleton for skeleton in skeletons if skeleton)
    skeleton_repeat_ratio = (
        max(skeleton_counts.values()) / len(sentences)
        if sentences and skeleton_counts
        else 0.0
    )

    clause_lengths: list[int] = []
    for sentence in sentences:
        clauses = [
            _meaningful_length(part)
            for part in re.split(r"[，,；;：:]", sentence.analysis_text)
            if _meaningful_length(part) >= 3
        ]
        if len(clauses) >= 3:
            clause_lengths.extend(clauses)
    clause_cv = _coefficient_of_variation(clause_lengths) if len(clause_lengths) >= 6 else None

    semicolon_groups: list[list[int]] = []
    for sentence in sentences:
        parts = [
            _meaningful_length(part)
            for part in re.split(r"[；;]", sentence.analysis_text)
            if _meaningful_length(part) >= 4
        ]
        if len(parts) >= 3:
            semicolon_groups.append(parts)
    parallel_cv_values = [
        value
        for group in semicolon_groups
        if (value := _coefficient_of_variation(group)) is not None
    ]
    parallel_cv = min(parallel_cv_values) if parallel_cv_values else None

    signals: list[str] = []
    if len(lengths) >= 4 and sentence_cv is not None and sentence_cv <= 0.12:
        signals.append("uniform_sentence_length")
    if len(sentences) >= 4 and skeleton_repeat_ratio >= 0.75:
        signals.append("repeated_punctuation_sequence")
    if clause_cv is not None and clause_cv <= 0.18:
        signals.append("uniform_clause_length")
    if parallel_cv is not None and parallel_cv <= 0.15:
        signals.append("symmetric_parallel_items")

    strong_single = (
        len(lengths) >= 5 and sentence_cv is not None and sentence_cv <= 0.08
    ) or (parallel_cv is not None and parallel_cv <= 0.08)
    if len(signals) < 2 and not strong_single:
        paragraph.raw_features["R03"] = {
            "sentence_count": len(sentences),
            "sentence_length_cv": sentence_cv,
            "clause_length_cv": clause_cv,
            "punctuation_repeat_ratio": skeleton_repeat_ratio,
            "parallel_item_cv": parallel_cv,
            "triggered": False,
        }
        return

    features = {
        "sentence_count": len(sentences),
        "sentence_lengths": lengths,
        "sentence_length_cv": sentence_cv,
        "clause_length_cv": clause_cv,
        "punctuation_repeat_ratio": skeleton_repeat_ratio,
        "parallel_item_cv": parallel_cv,
        "signals": signals,
        "triggered": True,
    }
    paragraph.raw_features["R03"] = features
    explanation_parts: list[str] = []
    if "uniform_sentence_length" in signals and sentence_cv is not None:
        explanation_parts.append(f"句长变异系数为 {sentence_cv:.3f}")
    if "repeated_punctuation_sequence" in signals:
        explanation_parts.append(f"标点序列重复率为 {skeleton_repeat_ratio:.3f}")
    if "uniform_clause_length" in signals and clause_cv is not None:
        explanation_parts.append(f"子句长度变异系数为 {clause_cv:.3f}")
    if "symmetric_parallel_items" in signals and parallel_cv is not None:
        explanation_parts.append(f"排比项长度变异系数为 {parallel_cv:.3f}")
    paragraph.hits.append(
        _new_hit(
            factory.next_id(),
            "R03",
            "L1",
            "+".join(signals) or "extreme_regularity",
            "句式节奏过度规整：" + "；".join(explanation_parts) + "。",
            [(paragraph.char_start, paragraph.char_end)],
            features=features,
            context_sentence_ids=[item.unit_id for item in sentences],
        )
    )


def _normalized_chars(text: str) -> str:
    characters = list(text)
    for start, end in protected_spans(text):
        for index in range(start, end):
            characters[index] = " "
    output = "".join(characters)
    # 英文模型名、缩写和数值属于 X02 保护内容，不作为中文字面重复证据。
    output = re.sub(r"[A-Za-z0-9]+", "", output)
    output = re.sub(r"[\s\W_]+", "", output, flags=re.UNICODE).lower()
    for phrase in GENERIC_PREFIXES:
        output = output.replace(phrase, "")
    return output


def _ngrams(text: str, sizes: Iterable[int] = (3, 4, 5)) -> set[str]:
    normalized = _normalized_chars(text)
    grams: set[str] = set()
    for size in sizes:
        grams.update(
            normalized[index : index + size]
            for index in range(max(0, len(normalized) - size + 1))
        )
    return grams


def _literal_pair_score(left: str, right: str) -> tuple[float, list[str]]:
    left_grams = _ngrams(left)
    right_grams = _ngrams(right)
    if not left_grams or not right_grams:
        return 0.0, []
    shared = left_grams & right_grams
    score = len(shared) / max(1, min(len(left_grams), len(right_grams)))
    examples = sorted(shared, key=lambda item: (-len(item), item))[:8]
    return score, examples


def _prefix(text: str, length: int = 4) -> str:
    normalized = _normalized_chars(text)
    return normalized[:length] if len(normalized) >= length else ""


def detect_r04(
    paragraph: ParagraphUnit,
    factory: EventFactory,
    semantic_scorer: Callable[[Sequence[str]], Sequence[float]] | None,
    semantic_threshold: float,
) -> None:
    sentences = [item for item in paragraph.sentences if _meaningful_length(item.analysis_text) >= 10]
    if len(sentences) < 2:
        paragraph.raw_features["R04"] = {
            "semantic_backend": "disabled" if semantic_scorer is None else "enabled",
            "triggered": False,
        }
        return

    semantic_scores = (
        list(semantic_scorer([item.analysis_text for item in sentences]))
        if semantic_scorer is not None
        else [None] * (len(sentences) - 1)
    )
    if len(semantic_scores) != len(sentences) - 1:
        raise RuleDetectionError("语义相似度计算器返回的数量与相邻句对不匹配。")

    prefixes = [_prefix(item.analysis_text) for item in sentences]
    prefix_counts = Counter(
        prefix
        for prefix in prefixes
        if prefix and not any(prefix.startswith(term) for term in GENERIC_PREFIXES)
    )
    repeated_prefixes = {prefix for prefix, count in prefix_counts.items() if count >= 3}

    pair_features: list[dict[str, Any]] = []
    for index in range(len(sentences) - 1):
        left = sentences[index]
        right = sentences[index + 1]
        literal_score, shared = _literal_pair_score(left.analysis_text, right.analysis_text)
        prefix_repeat = bool(
            prefixes[index]
            and prefixes[index] == prefixes[index + 1]
            and prefixes[index] in repeated_prefixes
        )
        literal_hit = (literal_score >= 0.32 and len(shared) >= 5) or prefix_repeat
        left_number_count = len(re.findall(r"\d+(?:\.\d+)?%?", left.analysis_text))
        right_number_count = len(re.findall(r"\d+(?:\.\d+)?%?", right.analysis_text))
        evidence_dense_pair = (
            left_number_count >= 2
            and right_number_count >= 2
            and bool(EVIDENCE_RE.search(left.analysis_text))
            and bool(EVIDENCE_RE.search(right.analysis_text))
        )
        if evidence_dense_pair:
            literal_hit = False
        semantic_score = semantic_scores[index]
        semantic_hit = semantic_score is not None and semantic_score >= semantic_threshold
        pair_features.append(
            {
                "left_sentence_id": left.unit_id,
                "right_sentence_id": right.unit_id,
                "literal_ngram_score": literal_score,
                "shared_ngrams": shared,
                "repeated_prefix": prefixes[index] if prefix_repeat else None,
                "semantic_similarity": semantic_score,
                "left_number_count": left_number_count,
                "right_number_count": right_number_count,
                "evidence_dense_pair": evidence_dense_pair,
                "literal_hit": literal_hit,
                "semantic_hit": semantic_hit,
            }
        )
        if not literal_hit and not semantic_hit:
            continue
        level = "L2" if literal_hit and semantic_hit else "L1"
        if literal_hit and semantic_hit:
            signal = "literal_and_semantic_redundancy"
            explanation = (
                f"相邻句同时出现字面重复和高语义相似（语义相似度 "
                f"{float(semantic_score):.3f}）。"
            )
        elif literal_hit:
            signal = "literal_redundancy"
            explanation = f"相邻句存在 3–5 gram 或句首重复（字面重复得分 {literal_score:.3f}）。"
        else:
            signal = "semantic_redundancy"
            explanation = f"相邻句语义高度相似（相似度 {float(semantic_score):.3f}）。"
        paragraph.hits.append(
            _new_hit(
                factory.next_id(),
                "R04",
                level,
                signal,
                explanation,
                [
                    (left.char_start, left.char_end),
                    (right.char_start, right.char_end),
                ],
                features=pair_features[-1],
                context_sentence_ids=[left.unit_id, right.unit_id],
            )
        )

    paragraph.raw_features["R04"] = {
        "semantic_backend": "disabled" if semantic_scorer is None else "enabled",
        "semantic_threshold": semantic_threshold,
        "pairs": pair_features,
        "triggered": any(
            item["literal_hit"] or item["semantic_hit"] for item in pair_features
        ),
    }


def detect_r05(paragraph: ParagraphUnit, factory: EventFactory) -> None:
    sentences = paragraph.sentences
    for index, sentence in enumerate(sentences):
        protected = protected_spans(sentence.analysis_text)
        context = " ".join(
            sentences[position].analysis_text
            for position in range(max(0, index - 1), min(len(sentences), index + 2))
        )
        evidence_matches = [match.group(0) for match in EVIDENCE_RE.finditer(context)]
        for match in EVALUATION_RE.finditer(sentence.analysis_text):
            if _overlaps(match.start(), match.end(), protected):
                continue
            if evidence_matches:
                continue
            absolute = (
                sentence.char_start + match.start(),
                sentence.char_start + match.end(),
            )
            sentence.hits.append(
                _new_hit(
                    factory.next_id(),
                    "R05",
                    "L1",
                    "evaluation_without_evidence",
                    f"评价性表述“{match.group(0)}”在同句及相邻句中未找到数值、指标或对比对象。",
                    [absolute],
                    match.group(0),
                    features={"context_evidence_count": 0},
                    context_sentence_ids=[
                        sentences[position].unit_id
                        for position in range(max(0, index - 1), min(len(sentences), index + 2))
                    ],
                )
            )


def _max_level(hits: Sequence[dict[str, Any]]) -> str | None:
    if not hits:
        return None
    return max((hit["level"] for hit in hits), key=lambda item: LEVEL_RANK[item])


def _record_for_sentence(sentence: SentenceUnit) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "unit_id": sentence.unit_id,
        "unit_type": "sentence",
        "parent_paragraph_id": sentence.paragraph_id,
        "section_type": sentence.section_type,
        "char_start": sentence.char_start,
        "char_end": sentence.char_end,
        "text": sentence.text,
        "skipped_spans": sentence.skipped_spans,
        "max_level": _max_level(sentence.hits),
        "hits": sentence.hits,
        "raw_features": sentence.raw_features,
        "status": "hit" if sentence.hits else "no_rule_hit",
    }


def _record_for_paragraph(paragraph: ParagraphUnit) -> dict[str, Any]:
    child_hits = [hit for sentence in paragraph.sentences for hit in sentence.hits]
    all_hits = [*paragraph.hits, *child_hits]
    return {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "unit_id": paragraph.unit_id,
        "unit_type": "paragraph",
        "source_block_id": paragraph.source_block_id,
        "section_type": paragraph.section_type,
        "char_start": paragraph.char_start,
        "char_end": paragraph.char_end,
        "text": paragraph.text,
        "skipped_spans": paragraph.skipped_spans,
        "sentence_ids": [sentence.unit_id for sentence in paragraph.sentences],
        "max_level": _max_level(all_hits),
        "hits": paragraph.hits,
        "child_hit_ids": [hit["event_id"] for hit in child_hits],
        "raw_features": paragraph.raw_features,
        "status": "hit" if all_hits else "no_rule_hit",
    }


def _collect_events(paragraphs: Sequence[ParagraphUnit]) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for paragraph in paragraphs:
        for hit in paragraph.hits:
            events[hit["event_id"]] = hit
        for sentence in paragraph.sentences:
            for hit in sentence.hits:
                events[hit["event_id"]] = hit
    return [events[key] for key in sorted(events)]


def detect_document(
    cleaned: dict[str, Any],
    semantic_scorer: Callable[[Sequence[str]], Sequence[float]] | None = None,
    semantic_threshold: float = 0.90,
) -> tuple[list[ParagraphUnit], list[dict[str, Any]], dict[str, Any]]:
    if not 0.0 <= semantic_threshold <= 1.0:
        raise RuleDetectionError("semantic_threshold 必须位于 0–1 之间。")
    paragraphs = build_units(cleaned)
    factory = EventFactory()
    for paragraph in paragraphs:
        for sentence in paragraph.sentences:
            detect_r01(sentence, factory)
        detect_r02(paragraph, factory)
        detect_r03(paragraph, factory)
        detect_r04(paragraph, factory, semantic_scorer, semantic_threshold)
        detect_r05(paragraph, factory)

    records: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        records.append(_record_for_paragraph(paragraph))
        records.extend(_record_for_sentence(sentence) for sentence in paragraph.sentences)

    events = _collect_events(paragraphs)
    level_counts = Counter(event["level"] for event in events)
    rule_counts = Counter(event["rule_id"] for event in events)
    signal_counts = Counter(event["signal"] for event in events)
    sentence_count = sum(len(paragraph.sentences) for paragraph in paragraphs)
    hit_sentence_count = sum(
        1 for paragraph in paragraphs for sentence in paragraph.sentences if sentence.hits
    )
    hit_paragraph_count = sum(
        1
        for paragraph in paragraphs
        if paragraph.hits or any(sentence.hits for sentence in paragraph.sentences)
    )
    source = cleaned.get("source", {})
    document_id = Path(str(source.get("path", "document"))).stem
    summary = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "document_id": document_id,
        "source_cleaned_document": source,
        "detection_scope": ["abstract", "body", "references"],
        "paragraph_count": len(paragraphs),
        "sentence_count": sentence_count,
        "hit_paragraph_count": hit_paragraph_count,
        "hit_sentence_count": hit_sentence_count,
        "event_count": len(events),
        "level_counts": {level: level_counts.get(level, 0) for level in ("L3", "L2", "L1")},
        "rule_counts": {rule: rule_counts.get(rule, 0) for rule in ("R01", "R02", "R03", "R04", "R05")},
        "signal_counts": dict(sorted(signal_counts.items())),
        "semantic_backend": "enabled" if semantic_scorer is not None else "disabled",
        "semantic_threshold": semantic_threshold if semantic_scorer is not None else None,
        "formula_placeholder_count": sum(
            len(paragraph.skipped_spans) for paragraph in paragraphs
        ),
        "ai_probability": None,
        "ai_rate": None,
        "interpretation": "规则命中只表示可疑证据，不等同于 AI 生成结论。",
        "warnings": (
            []
            if semantic_scorer is not None
            else ["R04 未启用句向量模型；当前只输出字面重复证据，不会产生 R04 L2 双证据结果。"]
        ),
    }
    return paragraphs, records, summary


def build_sentence_transformer_scorer(model_path: str) -> Callable[[Sequence[str]], Sequence[float]]:
    try:
        from sentence_transformers import SentenceTransformer
    except ModuleNotFoundError as exc:
        raise RuleDetectionError(
            "启用 --embedding-model 需要 sentence-transformers；"
            "请安装 optional-requirements.txt 中的可选依赖。"
        ) from exc

    model = SentenceTransformer(model_path)

    def scorer(sentences: Sequence[str]) -> Sequence[float]:
        if len(sentences) < 2:
            return []
        embeddings = model.encode(
            list(sentences),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [
            float((embeddings[index] * embeddings[index + 1]).sum())
            for index in range(len(embeddings) - 1)
        ]

    return scorer


def _set_run_font(run: Any, name: str, size_pt: float, color: str = "000000") -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run.font.size = Pt(size_pt)
    run.font.color.rgb = RGBColor.from_string(color)


BODY_FONT = "Source Han Sans CN"
HEADING_FONT = "Source Han Sans CN"


def _remove_paragraph_border(paragraph_or_style: Any) -> None:
    """Remove theme borders (notably Word's default Title underline)."""
    from docx.oxml.ns import qn

    element = getattr(paragraph_or_style, "_p", None)
    if element is None:
        element = paragraph_or_style._element
    properties = element.get_or_add_pPr()
    borders = properties.find(qn("w:pBdr"))
    if borders is not None:
        properties.remove(borders)


def _set_cell_shading(cell: Any, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), fill)


def _set_table_borders(table: Any, color: str = "D9D9D9") -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    borders = properties.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = borders.find(qn(f"w:{edge}"))
        if element is None:
            element = OxmlElement(f"w:{edge}")
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "6")
        element.set(qn("w:color"), color)


def _paragraph_events(paragraph: ParagraphUnit) -> list[dict[str, Any]]:
    events = list(paragraph.hits)
    events.extend(hit for sentence in paragraph.sentences for hit in sentence.hits)
    return sorted(events, key=lambda item: item["event_id"])


def _unique_note_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated note wording while retaining all highlighted evidence spans."""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        key = (event["rule_id"], event["level"], event["explanation"])
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique


def _severity_mask(paragraph: ParagraphUnit, events: Sequence[dict[str, Any]]) -> list[int]:
    mask = [0] * len(paragraph.text)
    for event in _unique_note_events(events):
        rank = LEVEL_RANK[event["level"]]
        spans = event.get("spans") or [
            {"char_start": paragraph.char_start, "char_end": paragraph.char_end}
        ]
        for span in spans:
            start = max(0, int(span["char_start"]) - paragraph.char_start)
            end = min(len(mask), int(span["char_end"]) - paragraph.char_start)
            for index in range(start, max(start, end)):
                mask[index] = max(mask[index], rank)
    return mask


def _visible_chunks(paragraph: ParagraphUnit) -> list[tuple[str, int]]:
    """Return visible text chunks grouped by severity, omitting formula markers."""
    events = _paragraph_events(paragraph)
    mask = _severity_mask(paragraph, events)
    formula_ranges = {
        (span["char_start"] - paragraph.char_start, span["char_end"] - paragraph.char_start)
        for span in paragraph.skipped_spans
    }
    skip_indices = {
        index
        for start, end in formula_ranges
        for index in range(max(0, start), min(len(paragraph.text), end))
    }

    chunks: list[tuple[str, int]] = []
    current_chars: list[str] = []
    current_rank: int | None = None
    for index, char in enumerate(paragraph.text):
        if index in skip_indices:
            continue
        rank = mask[index]
        if current_rank is None or rank == current_rank:
            current_chars.append(char)
            current_rank = rank
        else:
            chunks.append(("".join(current_chars), current_rank))
            current_chars = [char]
            current_rank = rank
    if current_chars:
        chunks.append(("".join(current_chars), current_rank or 0))
    return chunks


def _add_marked_paragraph(document: Any, paragraph: ParagraphUnit) -> None:
    from docx.shared import Pt

    events = _paragraph_events(paragraph)
    output = document.add_paragraph()
    output.paragraph_format.space_after = Pt(4)
    output.paragraph_format.line_spacing = 1.35
    if paragraph.section_type == "references":
        output.paragraph_format.left_indent = Pt(18)
        output.paragraph_format.first_line_indent = Pt(-18)

    for text, rank in _visible_chunks(paragraph):
        if not text:
            continue
        run = output.add_run(text)
        _set_run_font(run, BODY_FONT, 11.0, "C00000" if rank else "000000")
        if rank >= LEVEL_RANK["L3"]:
            run.bold = True

    for event in events:
        note = document.add_paragraph()
        note.paragraph_format.left_indent = Pt(18)
        note.paragraph_format.first_line_indent = Pt(-18)
        note.paragraph_format.space_after = Pt(2)
        note.paragraph_format.line_spacing = 1.15
        run = note.add_run(
            f"检测备注：[{event['rule_id']}｜{event['level']} "
            f"{LEVEL_LABEL[event['level']]}] {event['explanation']}"
        )
        _set_run_font(run, BODY_FONT, 9.5, "C00000")
        if event["level"] == "L3":
            run.bold = True


def write_review_docx(
    output_path: Path,
    paragraphs: Sequence[ParagraphUnit],
    summary: dict[str, Any],
) -> None:
    try:
        from docx import Document
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.shared import RGBColor
        from docx.shared import Inches, Pt
    except ModuleNotFoundError as exc:
        raise RuleDetectionError(
            "生成 Word 报告需要 python-docx；请安装 requirements.txt。"
        ) from exc

    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.85)
    section.bottom_margin = Inches(0.8)
    section.left_margin = Inches(0.9)
    section.right_margin = Inches(0.9)

    styles = document.styles
    normal = styles["Normal"]
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.3
    normal.font.size = Pt(11)
    normal.font.name = BODY_FONT
    normal._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), BODY_FONT)
    normal._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), BODY_FONT)
    for name in ("Title", "Heading 1", "Heading 2"):
        style = styles[name]
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.name = HEADING_FONT
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), HEADING_FONT)
        style._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), HEADING_FONT)
        style._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), HEADING_FONT)
        if name == "Title":
            _remove_paragraph_border(style)

    title = document.add_paragraph(style="Title")
    _remove_paragraph_border(title)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title.add_run("学术论文全文 AIGC 规则检测可视化报告")
    _set_run_font(title_run, HEADING_FONT, 20, "000000")
    title_run.bold = True

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle_run = subtitle.add_run(f"文档：{summary['document_id']}    规则版本：v{RULE_VERSION}")
    _set_run_font(subtitle_run, BODY_FONT, 10.5, "666666")

    intro = document.add_paragraph()
    intro.paragraph_format.space_before = Pt(10)
    intro_run = intro.add_run(
        "本报告展示摘要、正文和参考文献中的规则命中证据。"
        "红色文字表示需要人工复核的位置，段后红色备注说明命中规则和原因。"
        "规则命中不等同于 AI 生成结论，本阶段不计算 AI 率。"
    )
    _set_run_font(intro_run, BODY_FONT, 11, "000000")

    document.add_heading("检测概览", level=1)
    overview = document.add_table(rows=2, cols=5)
    overview.alignment = WD_TABLE_ALIGNMENT.CENTER
    overview.autofit = True
    headers = ["检测段落", "检测句子", "命中段落", "命中句子", "证据事件"]
    values = [
        summary["paragraph_count"],
        summary["sentence_count"],
        summary["hit_paragraph_count"],
        summary["hit_sentence_count"],
        summary["event_count"],
    ]
    for column, header in enumerate(headers):
        cell = overview.cell(0, column)
        cell.text = header
        _set_cell_shading(cell, "1F4E78")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for run in cell.paragraphs[0].runs:
            _set_run_font(run, HEADING_FONT, 9.5, "FFFFFF")
            run.bold = True
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        value_cell = overview.cell(1, column)
        value_cell.text = str(values[column])
        value_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        value_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in value_cell.paragraphs[0].runs:
            _set_run_font(run, BODY_FONT, 10, "000000")
    _set_table_borders(overview)

    document.add_paragraph()
    counts = document.add_table(rows=6, cols=4)
    counts.alignment = WD_TABLE_ALIGNMENT.CENTER
    count_headers = ["级别", "数量", "规则", "数量"]
    for column, header in enumerate(count_headers):
        cell = counts.cell(0, column)
        cell.text = header
        _set_cell_shading(cell, "D9EAF7")
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in cell.paragraphs[0].runs:
            _set_run_font(run, HEADING_FONT, 9.5, "000000")
            run.bold = True
    levels = ["L3", "L2", "L1", "", ""]
    rules_per_row = ["R01", "R02", "R03", "R04", "R05"]
    for row, level in enumerate(levels, start=1):
        row_values = [
            f"{level} {LEVEL_LABEL[level]}" if level else "",
            summary["level_counts"][level] if level else "",
            rules_per_row[row - 1],
            summary["rule_counts"][rules_per_row[row - 1]],
        ]
        for column, value in enumerate(row_values):
            cell = counts.cell(row, column)
            cell.text = str(value)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.paragraphs[0].alignment = (
                WD_ALIGN_PARAGRAPH.LEFT if column in {0, 2} else WD_ALIGN_PARAGRAPH.CENTER
            )
            for run in cell.paragraphs[0].runs:
                _set_run_font(run, BODY_FONT, 9.5, "000000")
    _set_table_borders(counts)

    legend = document.add_paragraph()
    legend.paragraph_format.space_before = Pt(8)
    black = legend.add_run("阅读说明：黑色为未命中文本；")
    _set_run_font(black, BODY_FONT, 10.5, "000000")
    red = legend.add_run("红色为规则命中文本；红色段后文字为命中原因。")
    _set_run_font(red, BODY_FONT, 10.5, "C00000")

    if summary.get("warnings"):
        warning = document.add_paragraph()
        warning_run = warning.add_run("运行提示：" + " ".join(summary["warnings"]))
        _set_run_font(warning_run, BODY_FONT, 9.5, "666666")

    document.add_page_break()
    current_section: str | None = None
    for paragraph in paragraphs:
        if paragraph.section_type != current_section:
            current_section = paragraph.section_type
            document.add_heading(SECTION_LABEL.get(current_section, current_section), level=1)
        _add_marked_paragraph(document, paragraph)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)


def write_review_html(
    output_path: Path,
    paragraphs: Sequence[ParagraphUnit],
    summary: dict[str, Any],
) -> None:
    """Write a self-contained browser report with inline evidence highlights."""
    level_counts = summary["level_counts"]
    rule_counts = summary["rule_counts"]
    warning_html = "".join(
        f'<li>{html.escape(str(warning))}</li>' for warning in summary.get("warnings", [])
    )
    if warning_html:
        warning_html = f'<aside class="warning"><strong>运行提示</strong><ul>{warning_html}</ul></aside>'

    body_parts: list[str] = []
    current_section: str | None = None
    for paragraph in paragraphs:
        if paragraph.section_type != current_section:
            current_section = paragraph.section_type
            body_parts.append(
                f'<h2 id="section-{html.escape(current_section)}">'
                f'{html.escape(SECTION_LABEL.get(current_section, current_section))}</h2>'
            )
        events = _paragraph_events(paragraph)
        classes = "paper-paragraph has-hit" if events else "paper-paragraph"
        body_parts.append(
            f'<article class="{classes}" id="{html.escape(paragraph.unit_id)}" '
            f'data-section="{html.escape(paragraph.section_type)}">'
        )
        body_parts.append('<p class="source-text">')
        for text, rank in _visible_chunks(paragraph):
            escaped = html.escape(text).replace("\n", "<br>")
            if rank:
                level = f"L{rank}"
                body_parts.append(
                    f'<mark class="hit {level.lower()}" title="{level} {LEVEL_LABEL[level]}">{escaped}</mark>'
                )
            else:
                body_parts.append(escaped)
        body_parts.append("</p>")
        for event in _unique_note_events(events):
            note = (
                f"检测备注：[{event['rule_id']}｜{event['level']} "
                f"{LEVEL_LABEL[event['level']]}] {event['explanation']}"
            )
            body_parts.append(
                f'<p class="review-note {event["level"].lower()}">'
                f'{html.escape(note)}</p>'
            )
        body_parts.append("</article>")

    document_id = html.escape(str(summary["document_id"]))
    report = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{document_id}｜AIGC 规则检测报告</title>
  <style>
    :root {{ color-scheme: light; --ink:#202124; --muted:#667085; --line:#d0d5dd;
      --panel:#f8fafc; --red:#b42318; --red-bg:#fff1f0; --blue:#175cd3; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:#eef2f6;
      font-family:"Source Han Sans CN","Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif;
      line-height:1.75; }}
    .shell {{ width:min(1120px, calc(100% - 32px)); margin:24px auto 56px; }}
    header, main {{ background:#fff; border:1px solid var(--line); border-radius:14px;
      box-shadow:0 6px 24px rgba(16,24,40,.06); }}
    header {{ padding:28px 32px 24px; margin-bottom:18px; }}
    h1 {{ margin:0 0 4px; font-size:28px; line-height:1.35; }}
    .subtitle, .disclaimer {{ color:var(--muted); margin:4px 0; }}
    .cards {{ display:grid; grid-template-columns:repeat(5,minmax(100px,1fr)); gap:10px; margin:20px 0 14px; }}
    .card {{ padding:12px 14px; background:var(--panel); border:1px solid #e4e7ec; border-radius:10px; }}
    .card b {{ display:block; font-size:23px; line-height:1.2; color:#101828; }}
    .card span {{ color:var(--muted); font-size:13px; }}
    .counts {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .pill {{ padding:4px 10px; background:#eff4ff; border-radius:999px; font-size:13px; }}
    .warning {{ margin-top:16px; padding:12px 16px; background:#fffaeb; border-left:4px solid #f79009; }}
    .warning ul {{ margin:4px 0 0; padding-left:20px; }}
    .toolbar {{ position:sticky; top:0; z-index:10; display:flex; gap:10px; align-items:center;
      padding:10px 24px; background:rgba(255,255,255,.96); border-bottom:1px solid var(--line); }}
    button {{ border:1px solid #98a2b3; border-radius:8px; background:#fff; padding:7px 12px; cursor:pointer; }}
    button[aria-pressed="true"] {{ color:#fff; background:var(--blue); border-color:var(--blue); }}
    #filter-status {{ color:var(--muted); font-size:13px; }}
    .content {{ padding:18px 32px 40px; }}
    h2 {{ margin:28px 0 12px; padding-bottom:7px; border-bottom:2px solid #344054; font-size:21px; }}
    .paper-paragraph {{ padding:8px 10px; border-radius:8px; }}
    .paper-paragraph:hover {{ background:#f9fafb; }}
    .paper-paragraph.is-hidden {{ display:none; }}
    .source-text {{ margin:0; text-align:justify; overflow-wrap:anywhere; }}
    mark.hit {{ color:var(--red); background:var(--red-bg); border-bottom:2px solid #f04438;
      border-radius:2px; padding:0 .08em; }}
    mark.l3 {{ font-weight:700; }}
    .review-note {{ margin:4px 0 0; color:var(--red); font-size:14px; font-weight:600; }}
    .review-note::before {{ content:"↳ "; }}
    @media (max-width:760px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }}
      header,.content {{ padding-left:18px; padding-right:18px; }} .toolbar {{ padding:9px 18px; }} }}
    @media print {{ body {{ background:#fff; }} .shell {{ width:auto; margin:0; }} header,main {{ border:0; box-shadow:none; }}
      .toolbar {{ display:none; }} .paper-paragraph {{ break-inside:avoid; }} }}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <h1>学术论文全文 AIGC 规则检测报告</h1>
    <p class="subtitle">文档：{document_id}　规则版本：v{html.escape(RULE_VERSION)}</p>
    <p class="disclaimer">红色文字为待人工复核的规则证据；规则命中不等同于 AI 生成结论，本阶段不计算 AI 率。</p>
    <section class="cards" aria-label="检测概览">
      <div class="card"><b>{summary['paragraph_count']}</b><span>检测段落</span></div>
      <div class="card"><b>{summary['sentence_count']}</b><span>检测句子</span></div>
      <div class="card"><b>{summary['hit_paragraph_count']}</b><span>命中段落</span></div>
      <div class="card"><b>{summary['hit_sentence_count']}</b><span>命中句子</span></div>
      <div class="card"><b>{summary['event_count']}</b><span>证据事件</span></div>
    </section>
    <div class="counts">
      <span class="pill">L3 直接证据：{level_counts['L3']}</span>
      <span class="pill">L2 强可疑证据：{level_counts['L2']}</span>
      <span class="pill">L1 辅助证据：{level_counts['L1']}</span>
      <span class="pill">R01：{rule_counts['R01']}</span><span class="pill">R02：{rule_counts['R02']}</span>
      <span class="pill">R03：{rule_counts['R03']}</span><span class="pill">R04：{rule_counts['R04']}</span>
      <span class="pill">R05：{rule_counts['R05']}</span>
    </div>
    {warning_html}
  </header>
  <main>
    <div class="toolbar">
      <button id="hit-toggle" type="button" aria-pressed="false">只看命中段落</button>
      <span id="filter-status">当前显示全文</span>
    </div>
    <div class="content">{''.join(body_parts)}</div>
  </main>
</div>
<script>
  const button = document.getElementById('hit-toggle');
  const status = document.getElementById('filter-status');
  const paragraphs = [...document.querySelectorAll('.paper-paragraph')];
  button.addEventListener('click', () => {{
    const active = button.getAttribute('aria-pressed') !== 'true';
    button.setAttribute('aria-pressed', String(active));
    button.textContent = active ? '显示完整全文' : '只看命中段落';
    paragraphs.forEach(p => p.classList.toggle('is-hidden', active && !p.classList.contains('has-hit')));
    status.textContent = active ? `当前显示 ${{paragraphs.filter(p => p.classList.contains('has-hit')).length}} 个命中段落` : '当前显示全文';
  }});
</script>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")


def output_paths(input_path: Path, output_dir: Path) -> tuple[Path, Path, Path, Path]:
    stem = input_path.stem
    if stem.endswith(".cleaned"):
        stem = stem[: -len(".cleaned")]
    return (
        output_dir / f"{stem}.rules.jsonl",
        output_dir / f"{stem}.rules.summary.json",
        output_dir / f"{stem}.rules.review.html",
        output_dir / f"{stem}.rules.review.docx",
    )


def write_outputs(
    records: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    paragraphs: Sequence[ParagraphUnit],
    jsonl_path: Path,
    summary_path: Path,
    docx_path: Path,
    html_path: Path | None = None,
    create_docx: bool = False,
    create_html: bool = True,
) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if create_html:
        if html_path is None:
            html_path = docx_path.with_suffix(".html")
        write_review_html(html_path, paragraphs, summary)
    if create_docx:
        write_review_docx(docx_path, paragraphs, summary)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对第一层清洗后的学术论文执行 R01–R05 规则检测。"
    )
    parser.add_argument("input", type=Path, help="输入 *.cleaned.json")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("检测流程/02_规则检测/results/local"),
        help="输出目录",
    )
    parser.add_argument(
        "--embedding-model",
        type=str,
        default=None,
        help="R04 可选句向量模型名称或本地路径，例如 BAAI/bge-base-zh-v1.5",
    )
    parser.add_argument(
        "--semantic-threshold",
        type=float,
        default=0.90,
        help="R04 相邻句语义相似度阈值（默认：0.90）",
    )
    parser.add_argument(
        "--docx",
        action="store_true",
        help="额外生成 Word 兼容报告（默认只生成 HTML）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.input.is_file():
            raise RuleDetectionError(f"输入文件不存在：{args.input}")
        cleaned = json.loads(args.input.read_text(encoding="utf-8"))
        semantic_scorer = (
            build_sentence_transformer_scorer(args.embedding_model)
            if args.embedding_model
            else None
        )
        paragraphs, records, summary = detect_document(
            cleaned,
            semantic_scorer=semantic_scorer,
            semantic_threshold=args.semantic_threshold,
        )
        jsonl_path, summary_path, html_path, docx_path = output_paths(args.input, args.output_dir)
        write_outputs(
            records,
            summary,
            paragraphs,
            jsonl_path,
            summary_path,
            docx_path,
            html_path=html_path,
            create_docx=args.docx,
        )
    except (RuleDetectionError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"规则检测完成：{args.input}")
    print(f"逐单元结果：{jsonl_path}")
    print(f"汇总结果：{summary_path}")
    print(f"HTML 可视化报告：{html_path}")
    if args.docx:
        print(f"Word 兼容报告：{docx_path}")
    print(
        f"检测 {summary['paragraph_count']} 段、{summary['sentence_count']} 句，"
        f"共 {summary['event_count']} 个规则证据事件。"
    )
    for warning in summary["warnings"]:
        print(f"警告：{warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
