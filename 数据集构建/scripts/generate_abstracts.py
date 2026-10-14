#!/usr/bin/env python3
"""调用多家模型生成纯 AI 摘要或轻度润色摘要。

密钥只从环境变量读取。输出采用 JSONL 追加写入并支持按 sample_id 断点续跑。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


TASKS = {
    "ai_full": ("ai_full_v1.json", 0.7),
    "ai_polish_light": ("ai_polish_light_v1.json", 0.2),
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_dotenv(path: Path) -> None:
    """加载简单 KEY=VALUE 格式；已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def join_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:1000]}") from exc


def call_openai_responses(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout: int,
    request_overrides: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "reasoning": {"effort": "none"},
        "temperature": temperature,
        "max_output_tokens": 1500,
    }
    payload.update(request_overrides)
    response = post_json(
        join_url(base_url, "responses"),
        payload,
        {"Authorization": f"Bearer {api_key}"},
        timeout,
    )
    if isinstance(response.get("output_text"), str):
        return response["output_text"], response
    texts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                texts.append(content["text"])
    if not texts:
        raise ValueError(f"OpenAI response 中未找到文本：{response}")
    return "".join(texts), response


def call_openai_chat(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout: int,
    request_overrides: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": 1500,
        "stream": False,
    }
    payload.update(request_overrides)
    response = post_json(
        join_url(base_url, "chat/completions"),
        payload,
        {"Authorization": f"Bearer {api_key}"},
        timeout,
    )
    try:
        return response["choices"][0]["message"]["content"], response
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Chat Completions response 中未找到文本：{response}") from exc


def call_anthropic_messages(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout: int,
    request_overrides: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "temperature": temperature,
        "max_tokens": 1500,
    }
    payload.update(request_overrides)
    response = post_json(
        join_url(base_url, "v1/messages"),
        payload,
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout,
    )
    texts = [part["text"] for part in response.get("content", []) if part.get("type") == "text"]
    if not texts:
        raise ValueError(f"Anthropic response 中未找到文本：{response}")
    return "".join(texts), response


CALLERS = {
    "openai_responses": call_openai_responses,
    "openai_chat": call_openai_chat,
    "anthropic_messages": call_anthropic_messages,
}


def length_bucket(char_count: int) -> tuple[int, int]:
    if char_count < 200:
        return 150, 200
    if char_count < 300:
        return 200, 300
    if char_count < 400:
        return 300, 400
    return 400, 600


def clean_output(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def completed_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    ids: set[str] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "success":
                    ids.add(record["sample_id"])
    return ids


def render_prompt(prompt: dict[str, Any], task: str, row: dict[str, str]) -> str:
    if task == "ai_full":
        minimum, maximum = length_bucket(int(row["char_count"]))
        return prompt["user_template"].format(
            title=row["title"],
            discipline=row["discipline"],
            keywords=row["keywords"],
            min_chars=minimum,
            max_chars=maximum,
        )
    return prompt["user_template"].format(human_abstract=row["abstract"])


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=["openai", "deepseek", "anthropic"], required=True)
    parser.add_argument("--task", choices=["ai_full", "ai_polish_light", "all"], default="all")
    parser.add_argument("--config", type=Path, default=Path("数据集构建/config/providers.json"))
    parser.add_argument("--prompts-dir", type=Path, default=Path("数据集构建/prompts"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    providers = load_json(args.config)
    provider = providers[args.provider]
    base_url = os.getenv(provider["base_url_env"], "")
    api_key = os.getenv(provider["api_key_env"], "")
    if not args.dry_run and (not base_url or not api_key):
        raise ValueError(
            f"缺少 {provider['base_url_env']} 或 {provider['api_key_env']}；"
            "请配置 .env 或环境变量"
        )

    tasks = list(TASKS) if args.task == "all" else [args.task]
    with args.input.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if args.limit is not None:
        rows = rows[: args.limit]

    done = completed_ids(args.output)
    caller = CALLERS[provider["api_style"]]
    for row in rows:
        for task in tasks:
            sample_id = f"{row['group_id']}-{args.provider}-{task}"
            if sample_id in done:
                print(f"SKIP {sample_id}")
                continue

            prompt_filename, temperature = TASKS[task]
            prompt = load_json(args.prompts_dir / prompt_filename)
            user_prompt = render_prompt(prompt, task, row)
            if args.dry_run:
                print(f"\n===== {sample_id} =====")
                print(user_prompt)
                continue

            started_at = dt.datetime.now(dt.timezone.utc)
            error_message = None
            for attempt in range(1, args.retries + 2):
                try:
                    raw_text, raw_response = caller(
                        base_url,
                        api_key,
                        provider["model"],
                        prompt["system"],
                        user_prompt,
                        temperature,
                        args.timeout,
                        provider.get("request_overrides", {}),
                    )
                    finished_at = dt.datetime.now(dt.timezone.utc)
                    record = {
                        "sample_id": sample_id,
                        "group_id": row["group_id"],
                        "source_sample_id": row["sample_id"],
                        "label": task,
                        "provider": args.provider,
                        "requested_model": provider["model"],
                        "returned_model": raw_response.get("model"),
                        "system_fingerprint": raw_response.get("system_fingerprint"),
                        "prompt_version": prompt["prompt_version"],
                        "temperature": temperature,
                        "reasoning_mode": "disabled",
                        "started_at": started_at.isoformat(),
                        "finished_at": finished_at.isoformat(),
                        "attempt": attempt,
                        "status": "success",
                        "raw_output": raw_text,
                        "clean_output": clean_output(raw_text),
                        "raw_response": raw_response,
                    }
                    append_record(args.output, record)
                    print(f"OK   {sample_id}")
                    break
                except Exception as exc:  # noqa: BLE001 - 将失败写入实验记录
                    error_message = str(exc)
                    if attempt <= args.retries:
                        time.sleep(args.retry_delay * attempt)
            else:
                append_record(
                    args.output,
                    {
                        "sample_id": sample_id,
                        "group_id": row["group_id"],
                        "source_sample_id": row["sample_id"],
                        "label": task,
                        "provider": args.provider,
                        "requested_model": provider["model"],
                        "prompt_version": prompt["prompt_version"],
                        "temperature": temperature,
                        "reasoning_mode": "disabled",
                        "started_at": started_at.isoformat(),
                        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "status": "failed",
                        "error": error_message,
                    },
                )
                print(f"FAIL {sample_id}: {error_message}")


if __name__ == "__main__":
    main()
