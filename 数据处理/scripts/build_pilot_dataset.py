#!/usr/bin/env python3
"""把人类摘要与成功生成的 JSONL 合并为可直接实验的 CSV。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


FIELDS = [
    "sample_id",
    "group_id",
    "text",
    "label",
    "title",
    "keywords",
    "discipline",
    "section_type",
    "source_id",
    "source_dataset",
    "generator_model",
    "prompt_version",
    "temperature",
    "source_text_provided",
    "generation_date",
    "human_edit_level",
    "char_count",
    "split",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human", type=Path, required=True)
    parser.add_argument("--generated", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with args.human.open(encoding="utf-8", newline="") as handle:
        human_rows = list(csv.DictReader(handle))
    human_by_group = {row["group_id"]: row for row in human_rows}

    rows: list[dict[str, str | int | float]] = []
    for row in human_rows:
        rows.append(
            {
                "sample_id": row["sample_id"],
                "group_id": row["group_id"],
                "text": row["abstract"],
                "label": "human",
                "title": row["title"],
                "keywords": row["keywords"],
                "discipline": row["discipline"],
                "section_type": "abstract",
                "source_id": row["sample_id"],
                "source_dataset": row["source_dataset"],
                "generator_model": "",
                "prompt_version": "",
                "temperature": "",
                "source_text_provided": "",
                "generation_date": "",
                "human_edit_level": "none",
                "char_count": len(row["abstract"]),
                "split": "pilot",
            }
        )

    for path in args.generated:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") != "success":
                continue
            source = human_by_group[record["group_id"]]
            text = record["clean_output"]
            rows.append(
                {
                    "sample_id": record["sample_id"],
                    "group_id": record["group_id"],
                    "text": text,
                    "label": record["label"],
                    "title": source["title"],
                    "keywords": source["keywords"],
                    "discipline": source["discipline"],
                    "section_type": "abstract",
                    "source_id": source["sample_id"],
                    "source_dataset": source["source_dataset"],
                    "generator_model": record.get("returned_model")
                    or record["requested_model"],
                    "prompt_version": record["prompt_version"],
                    "temperature": record["temperature"],
                    "source_text_provided": str(
                        record["label"] == "ai_polish_light"
                    ).lower(),
                    "generation_date": record["finished_at"][:10],
                    "human_edit_level": "none",
                    "char_count": len(text),
                    "split": "pilot",
                }
            )

    rows.sort(key=lambda row: (str(row["group_id"]), str(row["sample_id"])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
