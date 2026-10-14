#!/usr/bin/env python3
"""核对 R02 注入清单与规则 JSONL，并输出不含论文正文的验收报告。"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Sequence


class ValidationError(RuntimeError):
    pass


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSONL 第 {line_number} 行无法解析：{exc}") from exc
        if not isinstance(item, dict):
            raise ValidationError(f"JSONL 第 {line_number} 行不是对象。")
        records.append(item)
    return records


def validate(
    manifest: dict[str, Any], records: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    expected_list = manifest.get("expected_signals")
    if not isinstance(expected_list, list) or not all(
        isinstance(item, str) and item for item in expected_list
    ):
        raise ValidationError("注入清单缺少有效的 expected_signals。")
    if len(expected_list) != len(set(expected_list)):
        raise ValidationError("注入清单中的 expected_signals 存在重复。")

    counts: Counter[str] = Counter()
    for record in records:
        for hit in record.get("hits", []):
            if hit.get("rule_id") == "R02" and isinstance(hit.get("signal"), str):
                counts[hit["signal"]] += 1

    expected = set(expected_list)
    detected = {signal for signal, count in counts.items() if count > 0}
    missing = sorted(expected - detected)
    unexpected = sorted(detected - expected)
    return {
        "expected_signal_type_count": len(expected),
        "detected_expected_signal_type_count": len(expected & detected),
        "r02_event_count": sum(counts.values()),
        "missing_signals": missing,
        "unexpected_signals": unexpected,
        "passed": not missing and not unexpected,
        "signal_counts": {
            signal: counts.get(signal, 0) for signal in sorted(expected)
        },
        "contains_source_text": False,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="*.injections.json 注入清单")
    parser.add_argument("rules_jsonl", type=Path, help="第二层 *.rules.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="脱敏验收报告路径")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        report = validate(manifest, load_jsonl(args.rules_jsonl))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"R02 注入验收：{'通过' if report['passed'] else '失败'}")
    print(
        f"预期 {report['expected_signal_type_count']} 类，"
        f"检出 {report['detected_expected_signal_type_count']} 类，"
        f"共 {report['r02_event_count']} 条 R02 事件。"
    )
    print(f"报告：{args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
