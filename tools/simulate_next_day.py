"""把状态文件里所有记录回退一天，用来模拟「第二天运行」。

为什么需要这个工具：跨天趋势（累计增量、连续天数）是这个项目最有价值的
功能之一，但天然没法在一天内验证。这个脚本让你立刻能测：
改完状态后跑一次 ghdigest，就能看到「连续 2 天在榜，累计 +N ★」是否正确。

用法：
    python tools/simulate_next_day.py          # 回退一天
    python tools/simulate_next_day.py --days 3 # 回退三天
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghdigest.console import setup_console  # noqa: E402


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="模拟第二天运行，用于验证跨天趋势")
    parser.add_argument("--days", type=int, default=1, help="回退多少天（默认 1）")
    parser.add_argument(
        "--state",
        default=None,
        help="状态文件路径（默认 .cache/state.json）",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    path = Path(args.state) if args.state else root / ".cache" / "state.json"

    if not path.exists():
        print(f"状态文件不存在：{path}")
        print("先跑一次： python -m ghdigest")
        return 1

    blob = json.loads(path.read_text(encoding="utf-8"))
    before = blob.get("last_run")

    # 记下修改前的值，用于回读校验（顺序无所谓，取第一条即可）
    first_name = next(iter(blob.get("history", {})), None)
    original_last_seen = (
        blob["history"][first_name].get("last_seen") if first_name else None
    )

    shifted = 0
    for item in blob.get("history", {}).values():
        last_seen = item.get("last_seen")
        if not last_seen:
            continue
        try:
            d = date.fromisoformat(last_seen)
        except ValueError:
            continue
        item["last_seen"] = (d - timedelta(days=args.days)).isoformat()
        # 回退后「连续天数」重置为 1，下一次运行才能算出正确的连续计数
        item["consecutive_days"] = 1
        shifted += 1

    if before:
        try:
            blob["last_run"] = (
                date.fromisoformat(before) - timedelta(days=args.days)
            ).isoformat()
        except ValueError:
            pass

    path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")

    # 立刻回读校验。踩过坑：曾用 PowerShell 改写状态文件，
    # 看起来成功但 ghdigest 读到的还是旧值，导致验证结论完全错误。
    # 所以改完必须回读，且比较的是「文件里的值」和「修改前的值」。
    check = json.loads(path.read_text(encoding="utf-8"))
    new_last_seen = (
        check["history"][first_name].get("last_seen") if first_name else None
    )
    ok = shifted == 0 or new_last_seen != original_last_seen

    print(f"回退了 {shifted} 条记录（-{args.days} 天）")
    print(f"last_run: {before} -> {check.get('last_run')}")
    if first_name:
        print(f"样例 {first_name}: {original_last_seen} -> {new_last_seen}")
    print(f"回读校验: {'OK' if ok else 'FAILED —— 状态文件没有被正确改写'}")
    print()
    print("现在运行 python -m ghdigest，应能看到「连续 N 天在榜」的趋势行")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
