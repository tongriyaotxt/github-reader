"""候选发现引擎。

核心难点不是「找到仓库」——GitHub 搜索就能做到。难点是**从噪音里挑出信号**。
实测踩到的三个坑，都在这里编码成规则：

坑 1：按 stars 排序返回的是 freeCodeCamp(45万星)、awesome-python 这类
      常青巨型库。它们每天都有推送，但永远不是「新闻」。
      → 对策：常青阈值降权 + 动量评分取代绝对星数。

坑 2：大量高星仓库是「资源集合」而非「可用工具」（awesome-xxx、面试题、
      教程）。对一个想跟进技术发展的开发者，这些是噪音。
      → 对策：topic/名称/描述三层 blocklist。

坑 3：新项目星少但可能极重要（比如刚发布两周的协议实现）。
      拿它和老项目比绝对星数是拿苹果比橘子。
      → 对策：星数按「年龄」归一化成 velocity（星/天）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .github_client import GitHubClient, GitHubError


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since(dt: datetime | None, now: datetime) -> float:
    if dt is None:
        return 9999.0
    return max((now - dt).total_seconds() / 86400.0, 0.05)  # 防止除零


@dataclass
class Repo:
    """一个候选仓库，以及我们为它算出的所有信号。"""

    full_name: str
    url: str
    description: str
    stars: int
    forks: int
    open_issues: int
    language: str | None
    license: str | None
    created_at: datetime | None
    pushed_at: datetime | None
    topics: list[str]
    archived: bool
    is_fork: bool
    homepage: str | None
    size_kb: int

    # 发现来源
    domains: list[str] = field(default_factory=list)
    discovered_by: list[str] = field(default_factory=list)

    # 计算出的信号
    age_days: float = 9999.0
    idle_days: float = 9999.0
    velocity: float = 0.0  # 星/天，按年龄归一
    momentum: float = 0.0  # 综合动量分
    relevance: float = 0.0  # 与关注领域的相关度

    # README 分析结果（后填充）
    readme: str | None = None
    analysis: dict[str, Any] = field(default_factory=dict)

    # 记录信号是否已计算，避免依赖调用顺序
    _signals_ready: bool = False

    def ensure_signals(self, now: datetime | None = None) -> None:
        """确保 age_days / idle_days / velocity 已计算。

        实测踩坑：这些字段原先只在 discover() 里算，直接调用 analyze() 时
        它们还是默认值 9999，导致「刚推送的项目」被判成「两周没动静」。
        依赖调用顺序是脆弱的，所以改成按需自算。
        """
        if self._signals_ready:
            return
        now = now or datetime.now(timezone.utc)
        self.age_days = _days_since(self.created_at, now)
        self.idle_days = _days_since(self.pushed_at, now)
        self.velocity = self.stars / (self.age_days + 30.0)
        self._signals_ready = True


# --------------------------------------------------------------- 噪音过滤

# 明显是「资源集合 / 教程」而非「工具」的名字模式
_NOISE_NAME_RE = re.compile(
    r"(^|[-_/])(awesome|book|books|course|courses|tutorial|tutorials|"
    r"interview|roadmap|cheatsheet|cheat-sheet|guide|examples?|samples?|"
    r"free-programming|public-apis|developer-roadmap|best-?practices|"
    r"learning|study|notes|blog)([-_/]|$)",
    re.IGNORECASE,
)


def _is_noise(repo: Repo, settings: Settings) -> tuple[bool, str]:
    """判断是否为「不值得跟进」的仓库。返回 (是否噪音, 原因)。"""
    if settings.exclude_archived and repo.archived:
        return True, "已归档"
    if settings.exclude_forks and repo.is_fork:
        return True, "fork"

    topics_lower = {t.lower() for t in repo.topics}
    if settings.exclude_awesome_lists:
        blocked = topics_lower & set(settings.blocklist_topics)
        if blocked:
            return True, f"话题命中过滤: {','.join(sorted(blocked))}"

    if settings.exclude_awesome_lists and _NOISE_NAME_RE.search(repo.full_name):
        return True, "名称像资源集合/教程"

    # 没有描述且没有话题 —— 几乎无法判断价值，跳过
    if not repo.description and not repo.topics:
        return True, "无描述无话题"

    # 主页指向 github.io 常见于纯文档站
    if repo.homepage and "github.io" in repo.homepage and repo.size_kb < 400:
        return True, "疑似纯文档站"

    return False, ""


# --------------------------------------------------------------- 动量评分


def _compute_signals(repo: Repo, now: datetime, settings: Settings) -> None:
    """算出动量分。

    年龄/活跃度/星速由 repo.ensure_signals() 保证，这里只做评分，
    避免「时间信号」这类基础事实在两个地方各算一遍导致不一致。
    """
    repo.ensure_signals(now)

    # 新鲜度：越近推送越活跃。7 天内满分。
    freshness = math.exp(-max(repo.idle_days, 0.0) / 10.0)

    # 动量 = 星速（对数压缩）× 新鲜度 × 常青惩罚
    velocity_score = math.log10(repo.velocity + 1.0) * 2.4
    evergreen_penalty = 1.0
    if repo.stars > settings.evergreen_star_threshold:
        # 星数越高惩罚越重，但不归零——openclaw 这种既巨大又活跃的仍值得看
        over = repo.stars / settings.evergreen_star_threshold
        evergreen_penalty = max(0.28, 1.0 / (1.0 + 0.35 * math.log10(over * 10)))

    # 新项目额外加成：刚出现的东西更可能是「新闻」
    fresh_bonus = 1.0
    if repo.age_days <= settings.fresh_days:
        fresh_bonus = 1.35

    # 维护健康度：issue 完全没人管是坏信号
    health = 1.0
    if repo.open_issues > 0 and repo.stars > 500:
        ratio = repo.open_issues / max(repo.stars, 1)
        if ratio > 0.5:
            health = 0.8

    repo.momentum = velocity_score * freshness * evergreen_penalty * fresh_bonus * health


def _compute_relevance(repo: Repo, settings: Settings) -> None:
    """相关度：命中的领域关键词越多越高。"""
    text = " ".join(
        [
            repo.full_name.lower(),
            (repo.description or "").lower(),
            " ".join(repo.topics).lower(),
        ]
    )
    hits = 0
    total = 0
    for keywords in settings.domains.values():
        for kw in keywords:
            total += 1
            if kw.lower() in text:
                hits += 1
    repo.relevance = hits / max(total, 1)


# --------------------------------------------------------------- 搜索查询构造


def _build_queries(settings: Settings, now: datetime) -> list[tuple[str, str]]:
    """构造 (领域, 查询语句) 列表。

    每个领域跑两路：
      fresh —— 最近创建 + 有星，捕捉「新东西」
      active —— 老项目但最近在推进，捕捉「有动静」
    """
    fresh_since = (now - timedelta(days=settings.fresh_days)).strftime("%Y-%m-%d")
    active_since = (now - timedelta(days=settings.active_days)).strftime("%Y-%m-%d")

    queries: list[tuple[str, str]] = []
    for domain, keywords in settings.domains.items():
        for kw in keywords:
            queries.append(
                (
                    domain,
                    f'"{kw}" stars:>={settings.min_stars_fresh} created:>{fresh_since}',
                )
            )
            queries.append(
                (
                    domain,
                    f'"{kw}" stars:>={settings.min_stars_established} pushed:>{active_since}',
                )
            )
    return queries


def _repo_from_api(item: dict, now: datetime, settings: Settings) -> Repo:
    license_info = item.get("license") or {}
    return Repo(
        full_name=item.get("full_name", ""),
        url=item.get("html_url", ""),
        description=(item.get("description") or "").strip(),
        stars=item.get("stargazers_count", 0) or 0,
        forks=item.get("forks_count", 0) or 0,
        open_issues=item.get("open_issues_count", 0) or 0,
        language=item.get("language"),
        license=license_info.get("spdx_id") if isinstance(license_info, dict) else None,
        created_at=_parse_dt(item.get("created_at")),
        pushed_at=_parse_dt(item.get("pushed_at")),
        topics=item.get("topics") or [],
        archived=bool(item.get("archived")),
        is_fork=bool(item.get("fork")),
        homepage=item.get("homepage") or None,
        size_kb=item.get("size", 0) or 0,
    )


def discover(
    client: GitHubClient,
    settings: Settings,
    *,
    now: datetime | None = None,
    log=print,
) -> tuple[list[Repo], dict[str, int]]:
    """跑完整发现流程，返回 (候选列表, 统计信息)。"""
    now = now or datetime.now(timezone.utc)
    queries = _build_queries(settings, now)

    log(f"  构造 {len(queries)} 条搜索查询，覆盖 {len(settings.domains)} 个领域")

    seen: dict[str, Repo] = {}
    stats = {
        "queries": 0,
        "raw_results": 0,
        "unique": 0,
        "filtered_noise": 0,
        "filtered_evergreen": 0,
        "final": 0,
    }

    for domain, query in queries:
        try:
            items = client.search_repos(query, per_page=settings.per_domain_limit)
        except GitHubError as exc:
            log(f"  [warn] 查询失败，跳过：{query[:60]}… ({exc})")
            continue

        stats["queries"] += 1
        stats["raw_results"] += len(items)

        for item in items:
            full_name = item.get("full_name")
            if not full_name:
                continue
            repo = seen.get(full_name)
            if repo is None:
                repo = _repo_from_api(item, now, settings)
                seen[full_name] = repo

            if domain not in repo.domains:
                repo.domains.append(domain)
            tag = "fresh" if repo.age_days <= settings.fresh_days else "active"
            if tag not in repo.discovered_by:
                repo.discovered_by.append(tag)

    stats["unique"] = len(seen)
    log(f"  去重后 {stats['unique']} 个候选仓库")

    # 过滤 + 打分
    kept: list[Repo] = []
    for repo in seen.values():
        _compute_signals(repo, now, settings)
        _compute_relevance(repo, settings)

        noise, reason = _is_noise(repo, settings)
        if noise:
            stats["filtered_noise"] += 1
            if settings.verbose:
                log(f"    [skip] {repo.full_name} — {reason}")
            continue

        # 常青巨库：只有在「确实很活跃 + 与领域相关」时才保留
        if repo.stars > settings.evergreen_star_threshold:
            if repo.idle_days > settings.active_days or repo.relevance == 0:
                stats["filtered_evergreen"] += 1
                if settings.verbose:
                    log(f"    [skip] {repo.full_name} — 常青巨库且非领域相关")
                continue

        # 星速过低的直接丢（30 天平滑后仍 < 0.5 星/天说明增长停滞）
        if repo.velocity < 0.5 and repo.stars < settings.min_stars_established:
            continue

        kept.append(repo)

    kept.sort(key=lambda r: r.momentum, reverse=True)
    stats["final"] = len(kept)
    log(f"  过滤后保留 {stats['final']} 个（剔除噪音 {stats['filtered_noise']}，常青 {stats['filtered_evergreen']}）")

    return kept, stats
