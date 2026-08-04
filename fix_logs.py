#!/usr/bin/env python3
"""修复日志文件：给缺少 _account_email 的记录补上当前账户，去重后重新写入。"""
import argparse
import json
import hashlib
import os
import sys

def record_key(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fix_log_file(filepath: str, email: str) -> tuple[int, int, int]:
    """返回 (总行数, 补账户数, 去重删除数)"""
    records: list[dict] = []
    seen: set[str] = set()
    total = 0
    filled = 0
    duplicates = 0

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue

            if "_account_email" not in rec:
                rec["_account_email"] = email
                filled += 1

            key = record_key(rec)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            records.append(rec)

    records.sort(key=lambda r: r.get("requestTime", ""), reverse=True)

    with open(filepath, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    return total, filled, duplicates


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="修复 RawChat Codex 日志")
    parser.add_argument("--log-dir", default="logs", help="日志目录")
    parser.add_argument("--email", required=True, help="补写到日志中的账户邮箱")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    log_dir = args.log_dir
    if not os.path.isdir(log_dir):
        print(f"日志目录不存在: {log_dir}")
        sys.exit(1)

    files = sorted(
        f for f in os.listdir(log_dir) if f.startswith("rawchat_codex_") and f.endswith(".jsonl")
    )
    if not files:
        print("没有找到日志文件")
        return

    for filename in files:
        filepath = os.path.join(log_dir, filename)
        total, filled, duplicates = fix_log_file(filepath, args.email)
        print(f"{filename}: {total}行 → 补账户 {filled}条, 去重删 {duplicates}条 → 保留 {total - duplicates}条")


if __name__ == "__main__":
    main()
