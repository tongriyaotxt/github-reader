"""ghdigest 主入口。

用法：
    python -m ghdigest              # 生成今天的日报
    python -m ghdigest --top 8      # 精选 8 条
    python -m ghdigest --offline    # 只用缓存（不消耗配额）
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .analyze import analyze
from .config import DIGEST_DIR, Settings, ensure_dirs
from .console import bold, cyan, dim, green, setup_console, yellow
from .discover import Repo, discover
from .github_client import GitHubClient, GitHubError
from .render import DigestEntry, render_digest, write_digest
from .state import State
from .translate import parse_digest, translate_file
from .llm import resolve_config


def _enrich(client: GitHubClient, repos: list[Repo], limit: int) -> None:
    """给排名靠前的仓库补充 languages/releases 信号。

    只对前 limit 个做，因为每个仓库要多花 1-2 次请求。
    对候选全量抓取会浪费配额而没有对应的信息增益。
    """
    for repo in repos[:limit]:
        extra: dict[str, Any] = {}

        try:
            langs = client.get_json(f"/repos/{repo.full_name}/languages")
            if isinstance(langs, dict) and langs:
                total = sum(v for v in langs.values() if isinstance(v, (int, float)))
                if total > 0:
                    top = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:3]
                    extra["languages"] = [
                        f"{name} {value * 100 // total}%" for name, value in top
                    ]
        except GitHubError:
            pass

        try:
            releases = client.get_json(f"/repos/{repo.full_name}/releases?per_page=20")
            if isinstance(releases, list):
                cutoff = time.time() - 30 * 86400
                recent = 0
                for rel in releases:
                    published = rel.get("published_at") or ""
                    try:
                        ts = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        recent += 1
                extra["releases_30d"] = recent
        except GitHubError:
            pass

        repo.analysis["extra"] = extra


def run(args: argparse.Namespace) -> int:
    setup_console()

    def log(msg: str) -> None:
        print(msg, flush=True)

    started = time.time()
    ensure_dirs()

    settings = Settings.load(Path(args.config) if args.config else None)
    if args.top:
        settings.top_picks = args.top
    if args.feed:
        settings.feed_picks = args.feed
    if args.domains:
        wanted = {d.strip().lower() for d in args.domains.split(",") if d.strip()}
        filtered = {
            k: v
            for k, v in settings.domains.items()
            if any(w in k.lower() for w in wanted)
        }
        if filtered:
            settings.domains = filtered
        else:
            log(yellow(f"[warn] --domains 没匹配到任何领域，保留全部：{list(settings.domains)}"))
    settings.offline = args.offline
    settings.verbose = args.verbose

    log(bold(f"\nghdigest v{__version__}") + dim(f"   {datetime.now():%Y-%m-%d %H:%M}"))
    log("")

    # ---- 客户端 ----
    client = GitHubClient(
        cache_ttl_minutes=settings.cache_ttl_minutes,
        offline=settings.offline,
        verbose=settings.verbose,
    )
    if not client.authenticated:
        log(yellow("  ⚠ 未检测到 GitHub 凭据，将使用未认证请求（60 次/小时）"))
        log(yellow("    建议运行： gh auth login"))
    else:
        rate = client.probe_rate_limit()
        if rate.limit:
            log(dim(f"  已认证 · 配额 {rate.remaining}/{rate.limit} 次"))
        else:
            log(dim("  已认证"))

    # ---- 发现 ----
    log(cyan("\n[1/4] 发现候选…"))
    try:
        repos, stats = discover(client, settings, log=log)
    except GitHubError as exc:
        log(yellow(f"\n发现阶段失败：{exc}"))
        return 2

    if not repos:
        log(yellow("\n没有找到候选仓库。可能原因："))
        log(yellow("  · 配额耗尽或网络不可达 —— 试 python -m ghdigest --offline"))
        log(yellow("  · config.json 的星数门槛设得太高"))
        return 1

    # 历史趋势（必须在 observe 之前读，才能对比出增量）
    state = State()
    today = datetime.now().strftime("%Y-%m-%d")
    prior = {
        name: (h.seen_count, h.stars_first, h.consecutive_days)
        for name, h in state.history.items()
    }

    # ---- 抓 README ----
    log(cyan(f"\n[2/4] 抓取 README（{len(repos)} 个，并发 8）…"))
    done = 0

    def on_done(name: str) -> None:
        nonlocal done
        done += 1
        if done % 20 == 0 or done == len(repos):
            print(dim(f"      {done}/{len(repos)}"), flush=True)

    try:
        readmes = client.prefetch_readmes([r.full_name for r in repos], on_done=on_done)
    except Exception as exc:
        log(yellow(f"  README 抓取异常，将降级为仅用元数据分析：{exc}"))
        readmes = {}

    got = sum(1 for v in readmes.values() if v)
    log(f"  成功获取 {got}/{len(repos)} 篇 README")

    # ---- 补充信号 ----
    log(cyan(f"\n[3/4] 补充技术栈与发版信号（前 {settings.top_picks * 2} 个）…"))
    _enrich(client, repos, settings.top_picks * 2)

    # ---- 分析 ----
    items: list[DigestEntry] = []
    domain_counts: dict[str, int] = {}

    for repo in repos:
        analysis = analyze(repo, readmes.get(repo.full_name), fetch_extra=repo.analysis.get("extra"))
        repo.analysis["result"] = analysis

        history, is_new_day = state.observe(repo.full_name, repo.stars, today)

        # 趋势只在「跨天的新观察」时才有意义。
        # 同一天重跑时星数波动是噪音，不该包装成「增长」。
        was_seen = prior.get(repo.full_name)
        observed_delta: int | None = None

        if was_seen is None:
            trend = "★ 首次出现"
        elif not is_new_day:
            # 同一天重跑时，星数波动是噪音，不该包装成「增长」。
            # 但如果星数确实变了（说明距上次运行有真实增长），仍值得说一句。
            delta = repo.stars - was_seen[1]
            trend = f"今日已见，+{delta} ★" if delta > 0 else ""
        else:
            delta = repo.stars - was_seen[1]
            if delta > 0:
                observed_delta = delta
                trend = f"第 {history.seen_count} 次出现，累计 +{delta} ★"
                if history.consecutive_days >= 2:
                    trend += f"，连续 {history.consecutive_days} 天在榜"
            elif history.consecutive_days >= 2:
                trend = f"连续 {history.consecutive_days} 天在榜，星数持平"
            else:
                trend = f"第 {history.seen_count} 次出现"

        items.append(
            DigestEntry(
                repo=repo, analysis=analysis, trend=trend, observed_delta=observed_delta
            )
        )

        for d in repo.domains:
            domain_counts[d] = domain_counts.get(d, 0) + 1

    # ---- 渲染 ----
    log(cyan("\n[4/4] 生成日报…"))
    picks = items[: settings.top_picks]
    feed = items[settings.top_picks : settings.top_picks + settings.feed_picks]

    content = render_digest(
        picks,
        feed,
        stats=stats,
        authenticated=client.authenticated,
        domain_counts=domain_counts,
    )

    digest_dir = Path(args.out) if args.out else DIGEST_DIR
    path = write_digest(content, digest_dir)

    state.prune()
    state.save(today)

    # ---- 可选：中文摘要 ----
    translated = 0
    if getattr(args, "zh", False):
        translated = _run_translation(path, args, log)

    elapsed = time.time() - started
    log("")
    log(green(f"  ✓ 日报已生成：{path}"))
    log(dim(f"    精选 {len(picks)} 条 + 动态 {len(feed)} 条 · 耗时 {elapsed:.1f}s"))
    if client.rate.limit:
        log(dim(f"    API 配额剩余 {client.rate.remaining}/{client.rate.limit}"))
    log("")
    log(f"  {bold(str(path))}")
    log("")
    return 0


def _run_translation(path: Path, args: argparse.Namespace, log) -> int:
    """给生成的日报补中文摘要。返回翻译条数。

    失败不致命：翻译是可选增强，抓取结果已经落盘了。
    """
    log(cyan("\n[5/5] 生成中文摘要…"))

    cfg = resolve_config(model=getattr(args, "zh_model", None))
    if not cfg.available:
        log(yellow("  ⚠ 没有可用的 API key，跳过中文摘要"))
        log(yellow("    可设 DEEPSEEK_API_KEY 环境变量，或配好 ~/.dsh 凭据"))
        return 0
    log(dim(f"    模型 {cfg.model} · 凭据来源：{cfg.source}"))

    parsed = parse_digest(path.read_text(encoding="utf-8"))
    if not parsed.units:
        log(dim("    没有待翻译的字段（可能已全部翻过）"))
        return 0
    log(f"    待翻译 {len(parsed.units)} 条")

    def progress(bi: int, total: int, size: int) -> None:
        print(dim(f"      批次 {bi}/{total}（{size} 条）"), flush=True)

    try:
        count, errors = translate_file(
            path, cfg, batch_size=args.zh_batch, progress=progress
        )
    except Exception as exc:  # 翻译失败不该让整个命令失败
        log(yellow(f"  ⚠ 翻译阶段异常，日报本身已保存：{exc}"))
        return 0

    if count:
        log(green(f"    ✓ 已补 {count} 条中文摘要"))
    for err in errors[:5]:
        log(yellow(f"    ⚠ {err}"))
    if len(errors) > 5:
        log(yellow(f"    ⚠ 另有 {len(errors) - 5} 条错误"))
    if errors:
        log(dim("    重跑同一条命令即可补齐缺失的译文（已翻的不会重复翻）"))
    return count


def translate_only(args: argparse.Namespace) -> int:
    """只翻译已有日报，不重新抓取（省配额、省时间）。"""
    setup_console()

    digest_dir = Path(args.out) if args.out else DIGEST_DIR
    path = digest_dir / f"{datetime.now():%Y-%m-%d}.md"
    if not path.exists():
        path = digest_dir / "latest.md"
    if not path.exists():
        print(yellow(f"找不到日报：{digest_dir}"))
        return 1

    print(bold(f"\n仅翻译模式：{path.name}\n"))
    cfg = resolve_config(model=getattr(args, "zh_model", None))
    if not cfg.available:
        print(yellow("没有可用的 API key。可设 DEEPSEEK_API_KEY，或配好 ~/.dsh 凭据"))
        return 1

    parsed = parse_digest(path.read_text(encoding="utf-8"))
    print(f"待翻译 {len(parsed.units)} 条（已翻过的会跳过）")
    if not parsed.units:
        return 0

    def progress(bi: int, total: int, size: int) -> None:
        print(dim(f"  批次 {bi}/{total}（{size} 条）"), flush=True)

    count, errors = translate_file(
        path, cfg, batch_size=args.zh_batch, progress=progress
    )
    print(green(f"\n  ✓ 已补 {count} 条中文摘要 → {path}"))
    for err in errors[:5]:
        print(yellow(f"  ⚠ {err}"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ghdigest",
        description="把 GitHub 技术动态压缩成一份能读完的日报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m ghdigest                    生成今天的日报\n"
            "  python -m ghdigest --zh               生成并补中文摘要\n"
            "  python -m ghdigest --zh-only          只翻译已有日报，不重新抓取\n"
            "  python -m ghdigest --top 8 --feed 30  精选 8 条 + 动态 30 条\n"
            "  python -m ghdigest --domains ai       只看 AI/LLM/Agent\n"
            "  python -m ghdigest --offline          只用缓存，不消耗配额\n"
            "  python -m ghdigest --verbose          显示被过滤掉的仓库及原因\n"
        ),
    )
    p.add_argument("--top", type=int, default=None, help="精选条数（默认 5）")
    p.add_argument("--feed", type=int, default=None, help="动态速览条数（默认 20）")
    p.add_argument("--domains", default=None, help="按领域名过滤，逗号分隔（如 ai,tool）")
    p.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    p.add_argument("--out", default=None, help="输出目录（默认 ./digests）")
    p.add_argument("--offline", action="store_true", help="只用缓存，不发网络请求")
    p.add_argument("--verbose", "-v", action="store_true", help="输出详细过程")

    zh = p.add_argument_group("中文摘要")
    zh.add_argument(
        "--zh", action="store_true", help="生成后补中文摘要（需要 API key）"
    )
    zh.add_argument(
        "--zh-only",
        action="store_true",
        help="不抓取，只给已有日报补中文摘要",
    )
    zh.add_argument(
        "--zh-batch", type=int, default=12, help="每批翻译条数（默认 12）"
    )
    zh.add_argument("--zh-model", default=None, help="覆盖模型名（默认 deepseek-chat）")

    p.add_argument("--version", action="version", version=f"ghdigest {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.zh_only:
            return translate_only(args)
        return run(args)
    except KeyboardInterrupt:
        print(yellow("\n已中断。"), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
