#!/usr/bin/env python3
"""从 CSL 官方 benchmark 中抽取计算机科学与技术摘要样本。"""

from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path


TARGET_DISCIPLINE = "计算机科学与技术"
SOURCE_DATASET = "CSL benchmark"
SOURCE_LICENSE = "Apache-2.0"


def read_tsv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle, delimiter="\t"))


def normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--min-chars", type=int, default=150)
    parser.add_argument("--max-chars", type=int, default=600)
    args = parser.parse_args()

    dcp_rows = read_tsv(args.source_dir / "cls_dcp_train.tsv")
    title_rows = read_tsv(args.source_dir / "ts_train.tsv")
    keyword_rows = read_tsv(args.source_dir / "kg_train.tsv")

    if not (len(dcp_rows) == len(title_rows) == len(keyword_rows)):
        raise ValueError(
            "CSL 三个任务文件行数不一致："
            f"discipline={len(dcp_rows)}, title={len(title_rows)}, "
            f"keywords={len(keyword_rows)}"
        )

    candidates: list[dict[str, str | int]] = []
    for row_index, (dcp, title, keyword) in enumerate(
        zip(dcp_rows, title_rows, keyword_rows, strict=True), start=1
    ):
        if len(dcp) != 3 or len(title) != 3 or len(keyword) != 3:
            continue

        _, dcp_abstract, discipline = dcp
        _, title_abstract, paper_title = title
        _, keyword_input, keywords = keyword

        abstract = normalize_text(dcp_abstract)
        if abstract != normalize_text(title_abstract):
            raise ValueError(f"第 {row_index} 行摘要在学科与标题任务中不一致")

        # 关键词任务的输入是“摘要 + 标题”。少数标题在不同任务文件中
        # 存在引号等轻微格式差异，因此使用摘要验证行对齐，不强制标题逐字相同。
        normalized_keyword_input = normalize_text(keyword_input)
        if not normalized_keyword_input.startswith(abstract):
            raise ValueError(f"第 {row_index} 行关键词任务无法与摘要对齐")

        char_count = len(abstract.replace(" ", ""))
        if discipline != TARGET_DISCIPLINE:
            continue
        if not args.min_chars <= char_count <= args.max_chars:
            continue

        candidates.append(
            {
                "source_row": row_index,
                "title": normalize_text(paper_title),
                "abstract": abstract,
                "keywords": normalize_text(keywords),
                "discipline": discipline,
                "char_count": char_count,
            }
        )

    if len(candidates) < args.count:
        raise ValueError(f"符合条件的样本只有 {len(candidates)} 条，少于所需 {args.count} 条")

    rng = random.Random(args.seed)
    selected = rng.sample(candidates, args.count)
    selected.sort(key=lambda item: int(item["source_row"]))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id",
        "group_id",
        "label",
        "title",
        "abstract",
        "keywords",
        "discipline",
        "char_count",
        "source_dataset",
        "source_split",
        "source_row",
        "source_license",
        "sampling_seed",
    ]
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample_number, item in enumerate(selected, start=1):
            sample_id = f"csl-cs-{sample_number:03d}-human"
            group_id = f"csl-cs-{sample_number:03d}"
            writer.writerow(
                {
                    "sample_id": sample_id,
                    "group_id": group_id,
                    "label": "human",
                    "title": item["title"],
                    "abstract": item["abstract"],
                    "keywords": item["keywords"],
                    "discipline": item["discipline"],
                    "char_count": item["char_count"],
                    "source_dataset": SOURCE_DATASET,
                    "source_split": "train",
                    "source_row": item["source_row"],
                    "source_license": SOURCE_LICENSE,
                    "sampling_seed": args.seed,
                }
            )

    print(f"候选样本：{len(candidates)}")
    print(f"已写出：{len(selected)} -> {args.output}")


if __name__ == "__main__":
    main()
