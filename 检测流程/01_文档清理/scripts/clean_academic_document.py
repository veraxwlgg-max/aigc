#!/usr/bin/env python3
"""从 DOCX 中抽取 AIGC 检测需要的第一轮清洗文本。

保留：摘要正文、正文连续自然语言段落、参考文献。
排除：题名与作者元数据、章节标题、图表及图注、独立公式、代码、
      分点列表、附录、致谢、页眉页脚及出版模板。

脚本不改写正文措辞或标点。输出 JSON 保存每个文本块的来源位置、
保留状态和排除原因；TXT 只包含可送入后续检测器的文本。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Sequence


SUPPORTED_SUFFIXES = {".docx"}

ABSTRACT_HEADING_RE = re.compile(
    r"^(?:摘\s*要|内容摘要|abstract)\s*[:：]?\s*$", re.IGNORECASE
)
ABSTRACT_INLINE_RE = re.compile(
    r"^(?:摘\s*要|内容摘要|abstract)\s*[:：]\s*(.+)$", re.IGNORECASE
)
REFERENCE_HEADING_RE = re.compile(
    r"^(?:参考文献|主要参考文献|引用文献|references|bibliography)\s*[:：]?\s*$",
    re.IGNORECASE,
)
APPENDIX_HEADING_RE = re.compile(
    r"^(?:附录(?:\s*[A-ZＡ-Ｚ一二三四五六七八九十0-9]+)?|appendix(?:\s+[A-Z0-9]+)?)\s*[:：]?\s*$",
    re.IGNORECASE,
)
APPENDIX_LETTER_HEADING_RE = re.compile(
    r"^[A-Z](?:\.\d+)*\s+[A-Z][A-Z0-9][A-Z0-9\s,:()/'&+\-]{3,120}$"
)
ACK_HEADING_RE = re.compile(
    r"^(?:致谢|鸣谢|acknowledg(?:e)?ments?)\s*[:：]?\s*$", re.IGNORECASE
)
KEYWORD_RE = re.compile(r"^(?:关键词|关键字|key\s*words?)\s*[:：]", re.IGNORECASE)

NUMBER_PREFIX = (
    r"(?:第[一二三四五六七八九十百]+[章节篇]|"
    r"[一二三四五六七八九十]+[、.]|"
    r"\d+(?:\.\d+){0,4}[、.．\s])"
)
BODY_HEADING_TERM = (
    r"引言|绪论|研究背景|相关工作|文献综述|理论基础|研究方法|材料与方法|"
    r"方法|实验设计|实验|结果与分析|结果|分析与讨论|讨论|结论与展望|"
    r"结论|总结|研究结论|局限性|未来工作|研究展望|introduction|"
    r"related work|literature review|background|methods?|materials and methods|"
    r"experiments?|results?|discussion|conclusions?|limitations?|future work"
)
BODY_HEADING_RE = re.compile(
    rf"^(?:{NUMBER_PREFIX}\s*)?(?:{BODY_HEADING_TERM})\s*$", re.IGNORECASE
)
NUMBERED_HEADING_RE = re.compile(
    rf"^{NUMBER_PREFIX}\s*(?=[A-Za-z\u4e00-\u9fff])\S.{{0,58}}$"
)
REFERENCE_START_RE = re.compile(
    r"^(?:\[\s*\d+\s*\]|［\s*\d+\s*］|\(\s*\d+\s*\)|"
    r"（\s*\d+\s*）|\d+[.、]\s*)"
)
LIST_RE = re.compile(
    r"^\s*(?:[-*•●▪◦]|[（(]?[一二三四五六七八九十0-9]+[)）、])\s+"
)
CAPTION_RE = re.compile(
    r"^\s*(?:图|表|figure|fig\.?|table)\s*[A-Z一二三四五六七八九十0-9]+(?:[.．-]\d+)*\s*",
    re.IGNORECASE,
)
METADATA_RE = re.compile(
    r"^(?:中图分类号|文献标识码|文章编号|收稿日期|修回日期|录用日期|基金项目|"
    r"基金资助|作者简介|通讯作者|利益冲突|数据可用性|伦理审批|开放获取|"
    r"copyright|doi\s*[:：]|received\s*[:：]|accepted\s*[:：]|"
    r"conflicts? of interest|data availability|funding\s*[:：])",
    re.IGNORECASE,
)
FOOTNOTE_RE = re.compile(
    r"^(?:[*†‡]\s*)?(?:corresponding author|通讯作者)|"
    r"^\d+\s*(?:the\s+)?(?:code|data|project|implementation)\b|"
    r"^\d*\s*(?:https?://|www\.)",
    re.IGNORECASE,
)
CODE_RE = re.compile(
    r"^(?:```|~~~|(?:def|class|function|public|private|import|from)\s+\w+|"
    r"(?:for|while|if)\s*\(.+\)\s*[{:]|SELECT\s+.+\s+FROM\s+)",
    re.IGNORECASE,
)
FORMULA_TOKEN_RE = re.compile(
    r"(?:\\(?:frac|sum|int|begin|end|alpha|beta|theta)|"
    r"[∑∫√≈≠≤≥∞⊙⊗]|[_^]\{[^}]+\}|\$[^$]+\$)"
)
TERMINAL_PUNCTUATION = set("。！？；.!?;")


class CleaningError(RuntimeError):
    """输入文件无法可靠解析。"""


@dataclass
class SourceBlock:
    """解析器输出的原始文本块。"""

    source_index: int
    text: str
    page: int | None = None
    kind_hint: str = "paragraph"
    style: str = ""
    bbox: list[float] | None = None
    is_list: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CleanBlock:
    """结构清洗后的文本块及其审计信息。"""

    block_id: str
    source_indices: list[int]
    page: int | None
    bbox: list[float] | None
    section_type: str
    block_type: str
    text: str
    included: bool
    exclude_reason: str | None
    clean_start: int | None = None
    clean_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    """只处理 Unicode、软连字符和空白，不改变标点或词语。"""
    text = unicodedata.normalize("NFC", str(text))
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_package(module_name: str, install_name: str) -> Any:
    try:
        return __import__(module_name)
    except ModuleNotFoundError as exc:
        raise CleaningError(
            f"缺少依赖 {install_name}。请先运行："
            "python3 -m pip install -r 检测流程/01_文档清理/requirements.txt"
        ) from exc


def _paragraph_has_numbering(paragraph: Any) -> bool:
    properties = paragraph._p.pPr
    return bool(properties is not None and properties.numPr is not None)


def _paragraph_has_drawing(paragraph: Any) -> bool:
    xml = paragraph._p.xml
    return any(token in xml for token in ("<w:drawing", "<w:pict", "<w:object"))


def _extract_docx_paragraph_text(paragraph: Any) -> tuple[str, int]:
    """按 DOCX 元素顺序读取段落，不解析公式内容。

    python-docx 的 Paragraph.text 会静默丢掉 Word 行内公式。这里把每个
    OMML 公式统一替换为 [公式]，供后续步骤识别并跳过公式本体。
    """
    from docx.oxml.ns import qn

    parts: list[str] = []
    formula_count = 0
    math_tags = {qn("m:oMath"), qn("m:oMathPara")}
    text_tag = qn("w:t")
    tab_tag = qn("w:tab")
    break_tags = {qn("w:br"), qn("w:cr")}

    for child in paragraph._p.iterchildren():
        if child.tag in math_tags:
            parts.append("[公式]")
            formula_count += 1
            continue

        for node in child.iter():
            if node.tag in math_tags:
                parts.append("[公式]")
                formula_count += 1
                continue
            if any(parent.tag in math_tags for parent in node.iterancestors()):
                continue
            if node.tag == text_tag and node.text:
                parts.append(node.text)
            elif node.tag == tab_tag:
                parts.append("\t")
            elif node.tag in break_tags:
                parts.append("\n")

    return normalize_text("".join(parts)), formula_count


def extract_docx(path: Path) -> tuple[list[SourceBlock], list[str]]:
    """按文档顺序读取 DOCX 段落与表格，表格只进入排除审计。"""
    require_package("docx", "python-docx")
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    document = Document(path)
    blocks: list[SourceBlock] = []
    source_index = 0

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text, formula_count = _extract_docx_paragraph_text(paragraph)
            has_drawing = _paragraph_has_drawing(paragraph)
            if not text and not has_drawing:
                continue
            style = paragraph.style.name if paragraph.style is not None else ""
            style_lower = style.lower()
            is_list = (
                _paragraph_has_numbering(paragraph)
                or "list" in style_lower
                or "列表" in style
            )
            kind_hint = "paragraph"
            if has_drawing and not text:
                kind_hint = "figure"
                text = "[嵌入图片]"
            elif "caption" in style_lower or "题注" in style:
                kind_hint = "caption"
            elif style_lower.startswith("heading") or style.startswith("标题"):
                kind_hint = "heading"
            prose_without_formulas = normalize_text(text.replace("[公式]", ""))
            if formula_count and not looks_like_prose(prose_without_formulas, 6):
                kind_hint = "equation_or_code"
            blocks.append(
                SourceBlock(
                    source_index=source_index,
                    text=text,
                    kind_hint=kind_hint,
                    style=style,
                    is_list=is_list,
                    metadata={
                        "contains_inline_formula": formula_count > 0,
                        "inline_formula_count": formula_count,
                    },
                )
            )
            source_index += 1
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows: list[str] = []
            for row in table.rows:
                cells = [normalize_text(cell.text) for cell in row.cells]
                rows.append(" | ".join(cell for cell in cells if cell))
            blocks.append(
                SourceBlock(
                    source_index=source_index,
                    text="\n".join(row for row in rows if row) or "[空表格]",
                    kind_hint="table",
                )
            )
            source_index += 1

    warnings: list[str] = []
    if not blocks:
        warnings.append("DOCX 中没有提取到可见文本。")
    return blocks, warnings


def detect_special_heading(text: str) -> str | None:
    normalized = normalize_text(text)
    if ABSTRACT_HEADING_RE.match(normalized):
        return "abstract"
    if REFERENCE_HEADING_RE.match(normalized):
        return "references"
    if APPENDIX_HEADING_RE.match(normalized):
        return "appendix"
    if ACK_HEADING_RE.match(normalized):
        return "acknowledgments"
    return None


def looks_like_caption(text: str) -> bool:
    """区分题注与以“图/\u8868 X”开头的正文解释句。"""
    normalized = normalize_text(text)
    match = CAPTION_RE.match(normalized)
    if not match:
        return False
    remainder = normalized[match.end() :].lstrip(" 　:：,，")
    prose_opening = re.compile(
        r"^(?:中|所示|显示|展示|给出|给出了|表明|说明|反映|"
        r"比较|汇总|列出|可见|shows?\b|illustrates?\b|presents?\b|"
        r"reports?\b|summari[sz]es?\b|indicates?\b|demonstrates?\b)",
        re.IGNORECASE,
    )
    return not prose_opening.match(remainder)


def is_heading(block: SourceBlock) -> bool:
    text = normalize_text(block.text)
    if block.kind_hint == "heading":
        return True
    if detect_special_heading(text) is not None:
        return True
    if BODY_HEADING_RE.match(text) or NUMBERED_HEADING_RE.match(text):
        return True
    return False


def looks_like_equation_or_code(text: str) -> bool:
    normalized = normalize_text(text)
    if CODE_RE.match(normalized):
        return True
    formula_matches = list(FORMULA_TOKEN_RE.finditer(normalized))
    if formula_matches:
        # 含有行内 Markdown/LaTeX 公式的自然语言正文仍需送入第二层，
        # 以便检测 ``$...$`` 等源格式残留；只把几乎没有正文的公式块排除。
        text_without_formulas = FORMULA_TOKEN_RE.sub("", normalized)
        prose_chars = sum(
            1
            for char in text_without_formulas
            if char.isalnum() or "\u4e00" <= char <= "\u9fff"
        )
        if prose_chars < 8:
            return True
    if len(normalized) < 4:
        return False
    symbol_count = sum(
        1
        for char in normalized
        if unicodedata.category(char).startswith(("S", "M"))
        or char in "=+-*/<>|{}[]_^"
    )
    language_count = sum(1 for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    return symbol_count >= 4 and symbol_count > language_count * 0.75


def looks_like_prose(text: str, min_prose_chars: int) -> bool:
    normalized = normalize_text(text)
    meaningful = sum(
        1 for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )
    if meaningful >= min_prose_chars:
        return True
    return meaningful >= 6 and normalized[-1:] in TERMINAL_PUNCTUATION


def _new_clean_block(
    block: SourceBlock,
    section_type: str,
    block_type: str,
    included: bool,
    reason: str | None,
    text: str | None = None,
) -> CleanBlock:
    return CleanBlock(
        block_id=f"b{block.source_index:06d}",
        source_indices=[block.source_index],
        page=block.page,
        bbox=block.bbox,
        section_type=section_type,
        block_type=block_type,
        text=normalize_text(block.text if text is None else text),
        included=included,
        exclude_reason=reason,
        metadata=dict(block.metadata),
    )


def _reference_entry_spans(text: str) -> list[tuple[int, int]]:
    """返回参考文献流中的条目范围，支持编号式和常见无编号式著录。"""
    numbered = list(
        re.finditer(
            r"(?m)(?<!\S)(?:\[\s*\d+\s*\]|［\s*\d+\s*］|"
            r"\(\s*\d+\s*\)|（\s*\d+\s*）)\s*",
            text,
        )
    )
    if len(numbered) >= 2 and numbered[0].start() < 20:
        starts = [match.start() for match in numbered]
        return [
            (start, starts[index + 1] if index + 1 < len(starts) else len(text))
            for index, start in enumerate(starts)
        ]

    year_end = re.compile(
        r"(?:19|20)\d{2}[a-z]?\."
        r"(?=(?:\s|\n)*(?!(?:URL|https?://|doi\b|arXiv\b))[A-ZÀ-ÖØ-Þ\u4e00-\u9fff])"
    )
    boundaries = [match.end() for match in year_end.finditer(text)]
    if not boundaries:
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in boundaries:
        if boundary - start >= 20:
            spans.append((start, boundary))
            start = boundary
    if len(text) - start >= 20:
        spans.append((start, len(text)))
    elif spans and start < len(text):
        old_start, _ = spans[-1]
        spans[-1] = (old_start, len(text))
    return spans or [(0, len(text))]


def _consolidate_reference_blocks(blocks: list[CleanBlock]) -> list[CleanBlock]:
    """将连续参考文献流重组为尽可能接近单条文献的文本块。"""
    reference_positions = [
        index
        for index, block in enumerate(blocks)
        if block.included and block.section_type == "references"
    ]
    if not reference_positions:
        return blocks

    reference_blocks = [blocks[index] for index in reference_positions]
    stream_parts: list[str] = []
    mappings: list[tuple[int, int, CleanBlock]] = []
    cursor = 0
    for index, block in enumerate(reference_blocks):
        if index:
            stream_parts.append("\n")
            cursor += 1
        start = cursor
        stream_parts.append(block.text)
        cursor += len(block.text)
        mappings.append((start, cursor, block))
    stream = "".join(stream_parts)
    spans = _reference_entry_spans(stream)
    if len(spans) <= 1:
        return blocks

    entries: list[CleanBlock] = []
    for entry_index, (start, end) in enumerate(spans, start=1):
        entry_text = normalize_text(stream[start:end].replace("\n", " "))
        if not entry_text:
            continue
        owners = [
            block
            for block_start, block_end, block in mappings
            if block_start < end and block_end > start
        ]
        if not owners:
            continue
        pages = {block.page for block in owners}
        page = next(iter(pages)) if len(pages) == 1 else None
        bboxes = [block.bbox for block in owners if block.bbox is not None]
        bbox: list[float] | None = None
        if page is not None and bboxes:
            bbox = [
                min(item[0] for item in bboxes),
                min(item[1] for item in bboxes),
                max(item[2] for item in bboxes),
                max(item[3] for item in bboxes),
            ]
        entries.append(
            CleanBlock(
                block_id=f"ref{entry_index:06d}",
                source_indices=sorted(
                    {source for block in owners for source in block.source_indices}
                ),
                page=page,
                bbox=bbox,
                section_type="references",
                block_type="reference",
                text=entry_text,
                included=True,
                exclude_reason=None,
            )
        )

    reference_ids = {id(block) for block in reference_blocks}
    first_position = reference_positions[0]
    output = [block for block in blocks if id(block) not in reference_ids]
    insertion_position = sum(
        1 for block in blocks[:first_position] if id(block) not in reference_ids
    )
    output[insertion_position:insertion_position] = entries
    return output


def clean_blocks(
    source_blocks: Sequence[SourceBlock], min_prose_chars: int = 10
) -> tuple[list[CleanBlock], list[str]]:
    """按论文结构筛选文本块，并保留逐块排除原因。"""
    if min_prose_chars < 1:
        raise ValueError("min_prose_chars 必须大于 0")

    cleaned: list[CleanBlock] = []
    warnings: list[str] = []
    state = "front_matter"
    saw_abstract = False
    saw_body = False
    saw_references = False
    pending_caption_tail: tuple[int | None, float] | None = None

    for block in source_blocks:
        text = normalize_text(block.text)
        if not text:
            continue

        if pending_caption_tail is not None:
            caption_page, caption_bottom = pending_caption_tail
            same_page = block.page == caption_page
            close_below = bool(
                block.bbox
                and 0 <= float(block.bbox[1]) - caption_bottom <= 18
            )
            if (
                same_page
                and close_below
                and block.kind_hint not in {"heading", "table", "header_footer"}
                and detect_special_heading(text) is None
                and not BODY_HEADING_RE.match(text)
                and not NUMBERED_HEADING_RE.match(text)
            ):
                cleaned.append(
                    _new_clean_block(
                        block,
                        state,
                        "caption_continuation",
                        False,
                        "figure_or_caption",
                    )
                )
                pending_caption_tail = None
                continue
            pending_caption_tail = None

        if block.kind_hint == "header_footer":
            cleaned.append(_new_clean_block(block, state, "layout_noise", False, "header_footer"))
            continue
        if block.kind_hint in {"table", "figure", "caption"} or looks_like_caption(text):
            reason = "table" if block.kind_hint == "table" else "figure_or_caption"
            cleaned.append(_new_clean_block(block, state, block.kind_hint, False, reason))
            if (
                reason == "figure_or_caption"
                and block.bbox
                and text[-1:] not in TERMINAL_PUNCTUATION
            ):
                pending_caption_tail = (block.page, float(block.bbox[3]))
            continue
        if block.kind_hint == "footnote" or FOOTNOTE_RE.match(text):
            cleaned.append(_new_clean_block(block, state, "footnote", False, "footnote"))
            continue
        if block.kind_hint == "equation_or_code" or looks_like_equation_or_code(text):
            cleaned.append(_new_clean_block(block, state, "equation_or_code", False, "equation_or_code"))
            continue

        inline_abstract = ABSTRACT_INLINE_RE.match(text)
        if inline_abstract:
            state = "abstract"
            saw_abstract = True
            abstract_text = normalize_text(inline_abstract.group(1))
            if looks_like_prose(abstract_text, min_prose_chars):
                cleaned.append(
                    _new_clean_block(
                        block, state, "paragraph", True, None, text=abstract_text
                    )
                )
            else:
                cleaned.append(
                    _new_clean_block(
                        block, state, "paragraph", False, "too_short", text=abstract_text
                    )
                )
            continue

        special_heading = detect_special_heading(text)
        if state == "references" and APPENDIX_LETTER_HEADING_RE.match(text):
            special_heading = "appendix"
        if special_heading is not None:
            state = special_heading
            saw_abstract = saw_abstract or state == "abstract"
            saw_references = saw_references or state == "references"
            cleaned.append(_new_clean_block(block, state, "section_heading", False, "section_heading"))
            continue

        # 参考文献条目中常出现“1. Author...”或“140. IEEE...”等形式，
        # 必须先按文献条目处理，不能被通用章节编号规则抢先识别。
        if state == "references":
            cleaned.append(_new_clean_block(block, state, "reference", True, None))
            continue

        # 进入附录或致谢后保持排除状态；其中的数字小节不能重新开启正文。
        # 若后面出现“参考文献”标题，已由上面的特殊标题分支恢复 references。
        if state in {"appendix", "acknowledgments"}:
            cleaned.append(_new_clean_block(block, state, "paragraph", False, state))
            continue

        if BODY_HEADING_RE.match(text) or NUMBERED_HEADING_RE.match(text):
            state = "body"
            saw_body = True
            cleaned.append(_new_clean_block(block, state, "section_heading", False, "section_heading"))
            continue

        if block.kind_hint == "heading":
            # 前置区域中的 Heading 样式可能只是论文题名，不能据此进入正文。
            if state in {"abstract", "body"}:
                state = "body"
                saw_body = True
            cleaned.append(_new_clean_block(block, state, "section_heading", False, "section_heading"))
            continue

        if is_heading(block):
            cleaned.append(_new_clean_block(block, state, "section_heading", False, "section_heading"))
            continue

        if state == "front_matter":
            reason = "metadata" if METADATA_RE.match(text) else "front_matter"
            cleaned.append(_new_clean_block(block, state, "metadata", False, reason))
            continue

        if KEYWORD_RE.match(text):
            cleaned.append(_new_clean_block(block, state, "keywords", False, "keywords"))
            continue
        if METADATA_RE.match(text):
            cleaned.append(_new_clean_block(block, state, "metadata", False, "publication_metadata"))
            continue
        if block.is_list or block.kind_hint == "list" or LIST_RE.match(text):
            cleaned.append(_new_clean_block(block, state, "list", False, "non_prose_list"))
            continue
        if not looks_like_prose(text, min_prose_chars):
            cleaned.append(_new_clean_block(block, state, "short_text", False, "too_short"))
            continue

        section_type = "abstract" if state == "abstract" else "body"
        cleaned.append(_new_clean_block(block, section_type, "paragraph", True, None))

    if not saw_abstract:
        warnings.append("未识别到摘要标题；标题之前的内容按前置元数据排除。")
    if not saw_body:
        warnings.append("未识别到正文章节标题；请抽查正文是否被正确保留。")
    if not saw_references:
        warnings.append("未识别到参考文献标题。")

    cleaned = _consolidate_reference_blocks(cleaned)
    _assign_clean_offsets(cleaned)
    return cleaned, warnings


def _assign_clean_offsets(blocks: Sequence[CleanBlock]) -> str:
    parts: list[str] = []
    cursor = 0
    for block in blocks:
        block.clean_start = None
        block.clean_end = None
        if not block.included:
            continue
        if parts:
            parts.append("\n\n")
            cursor += 2
        block.clean_start = cursor
        parts.append(block.text)
        cursor += len(block.text)
        block.clean_end = cursor
    return "".join(parts)


def build_result(
    source_path: Path,
    source_format: str,
    source_blocks: Sequence[SourceBlock],
    clean_blocks_result: Sequence[CleanBlock],
    warnings: Sequence[str],
) -> dict[str, Any]:
    clean_text = _assign_clean_offsets(clean_blocks_result)
    included = [block for block in clean_blocks_result if block.included]
    excluded = [block for block in clean_blocks_result if not block.included]
    return {
        "schema_version": "1.0",
        "source": {
            "path": str(source_path.resolve()),
            "format": source_format,
            "sha256": sha256_file(source_path),
        },
        "scope": {
            "included": ["abstract", "body_prose", "references"],
            "excluded": [
                "front_matter",
                "section_headings",
                "figures_and_tables",
                "captions",
                "equations_and_code",
                "non_prose_lists",
                "appendices",
                "acknowledgments",
                "layout_noise",
            ],
        },
        "clean_text": clean_text,
        "blocks": [asdict(block) for block in clean_blocks_result],
        "stats": {
            "source_block_count": len(source_blocks),
            "included_block_count": len(included),
            "excluded_block_count": len(excluded),
            "included_char_count": len(clean_text),
            "included_by_section": dict(Counter(block.section_type for block in included)),
            "excluded_by_reason": dict(Counter(block.exclude_reason for block in excluded)),
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def clean_document(path: Path, min_prose_chars: int = 10) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise CleaningError(f"输入文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise CleaningError("只支持原生 .docx 文件；PDF 拆解由上游系统负责")

    source_blocks, extraction_warnings = extract_docx(path)
    source_format = "docx"

    cleaned, cleaning_warnings = clean_blocks(source_blocks, min_prose_chars)
    return build_result(
        source_path=path,
        source_format=source_format,
        source_blocks=source_blocks,
        clean_blocks_result=cleaned,
        warnings=[*extraction_warnings, *cleaning_warnings],
    )


def output_paths(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    stem = input_path.stem
    return (
        output_dir / f"{stem}.cleaned.txt",
        output_dir / f"{stem}.cleaned.json",
    )


def write_result(result: dict[str, Any], text_path: Path, json_path: Path) -> None:
    text_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(str(result["clean_text"]) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清洗学术 DOCX，保留摘要、正文连续段落和参考文献。"
    )
    parser.add_argument("input", type=Path, help="输入原生 .docx 文件")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("检测流程/01_文档清理/results/local"),
        help="输出目录（默认：检测流程/01_文档清理/results/local）",
    )
    parser.add_argument(
        "--min-prose-chars",
        type=int,
        default=10,
        help="连续正文块的最少有效字符数（默认：10）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = clean_document(args.input, min_prose_chars=args.min_prose_chars)
        text_path, json_path = output_paths(args.input, args.output_dir)
        write_result(result, text_path, json_path)
    except (CleaningError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    stats = result["stats"]
    print(f"清洗完成：{args.input}")
    print(f"保留文本：{text_path}")
    print(f"审计记录：{json_path}")
    print(
        "文本块："
        f"保留 {stats['included_block_count']}，"
        f"排除 {stats['excluded_block_count']}，"
        f"共 {stats['included_char_count']} 字符"
    )
    for warning in result["warnings"]:
        print(f"警告：{warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
