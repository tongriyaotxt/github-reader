"""摘要质量抽查：从已缓存的 README 里批量抽样，人工看摘要是否准确。

不是产品的一部分，是开发期的校准工具。用法：
    python tools/inspect_taglines.py --limit 25

它复用 .cache/http 里的缓存，不消耗额外配额。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghdigest.analyze import (  # noqa: E402
    _extract_tagline,
    _snap_to_word,
    clean_markdown,
)
from ghdigest.console import setup_console  # noqa: E402
from ghdigest.github_client import GitHubClient  # noqa: E402


def find_repos_with_cached_readme() -> list[tuple[str, str]]:
    """占位：缓存文件名是 URL 的 hash，无法反查仓库名。

    所以仓库列表从最近一次日报里读，或用 --repos 显式指定。
    """
    return []


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="抽查摘要提取质量")
    parser.add_argument("--limit", type=int, default=20, help="抽查多少个")
    parser.add_argument(
        "--repos",
        default=None,
        help="逗号分隔的仓库名；不给则从最近一次日报里读",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent

    if args.repos:
        names = [r.strip() for r in args.repos.split(",") if r.strip()]
    else:
        names = []
        for digest in sorted((root / "digests").glob("2*.md"), reverse=True)[:1]:
            for line in digest.read_text(encoding="utf-8").split("\n"):
                if line.startswith(("- **[", "### ", "1. [", "2. [", "3. [", "4. [", "5. [")):
                    import re

                    m = re.search(r"\[([\w.-]+/[\w.-]+)\]", line)
                    if m:
                        names.append(m.group(1))
        names = list(dict.fromkeys(names))

    if not names:
        print("没有找到待抽查的仓库。用 --repos owner/name,owner/name 指定。")
        return 1

    client = GitHubClient()
    print(f"抽查 {min(args.limit, len(names))} 个仓库的摘要提取结果")
    print("（标题行标注摘要来源：desc = GitHub description，readme = 从 README 抽取）")
    print("=" * 100)

    suspicious = 0
    used_desc = 0
    checked = 0

    for name in names[: args.limit]:
        raw = client.get_readme(name)
        # description 是主来源，必须一起取，否则测的不是真实流程
        try:
            desc = (client.get_json(f"/repos/{name}") or {}).get("description") or ""
        except Exception:
            desc = ""

        checked += 1
        if not raw:
            print(f"\n### {name}\n  (无 README)")
            continue

        cleaned = clean_markdown(raw)
        tagline = _extract_tagline(cleaned, desc, name)

        # 判定摘要来源：把 description 也做同样的截断处理再比较
        desc_norm = _snap_to_word(desc.strip(), 320) if desc.strip() else ""
        source = "desc" if desc_norm and tagline == desc_norm else "readme"
        if source == "desc":
            used_desc += 1

        # 只标记「真正有问题」的情况。
        # 「短」不是问题——"Chrome DevTools for coding agents" 就是一句好摘要。
        # 以冒号结尾、含标题记号、为空才是需要修的错误。
        flags = []
        if tagline.endswith((":", "：")):
            flags.append("以冒号结尾（多半是操作引导语）")
        if not tagline:
            flags.append("为空")
        if "#" in tagline:
            flags.append("含标题记号")
        if "\n" in tagline:
            flags.append("含换行")

        mark = "  ⚠ " + "、".join(flags) if flags else ""
        if flags:
            suspicious += 1

        print(f"\n### {name}  [{source}]{mark}")
        print(f"  {tagline}")

    print()
    print("=" * 100)
    print(f"需要修的错误 {suspicious}/{checked}　摘要来自 description 的：{used_desc}/{checked}")
    if used_desc < checked:
        print(
            f"  提示：{checked - used_desc} 个仓库的摘要来自 README 抽取，"
            "这些是准确率较低的部分，值得重点看"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
