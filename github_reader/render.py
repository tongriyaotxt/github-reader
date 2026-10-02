"""Markdown 日报渲染。

设计原则：**先给结论，再给细节**。
日报名为「日报」，但真实使用场景是：你只有 2 分钟，想知道今天有没有值得看的。
所以结构是：
  1. 顶部速览 —— 30 秒能扫完，决定要不要往下读
  2. 精选 5 条 —— 最值得花时间的，带完整上手评估
  3. 动态速览 —— 其余候选，一行一条

深度版内容多，所以每条条目有固定骨架（是什么 / 关键信息 / 上手难度），
读到第三条时你已经知道该往哪看，不需要重新适应格式。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .analyze import ReadmeAnalysis
from .discover import Repo


@dataclass
class DigestEntry:
    """日报里的一条。用具名结构而非元组——

    元组在增加字段时极易错位（for repo, analysis, trend in feed 这种写法
    一旦字段顺序变化就是静默错误），具名结构能让类型检查器和读者都看清。
    """

    repo: Repo
    analysis: ReadmeAnalysis
    trend: str = ""
    observed_delta: int | None = None


def _fmt_num(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}w"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _friction_label(score: int) -> str:
    if score <= 2:
        return "🟢 很轻"
    if score <= 5:
        return "🟡 中等"
    if score <= 7:
        return "🟠 偏重"
    return "🔴 很重"


def _age_label(days: float) -> str:
    if days < 1:
        return "今天"
    if days < 30:
        return f"{int(days)} 天"
    if days < 365:
        return f"{int(days / 30)} 个月"
    return f"{days / 365:.1f} 年"


def _rate_label(repo: Repo, observed_delta: int | None = None) -> str:
    """把「星速」这个数字说得诚实。

    实测踩坑：OpenSpec 有 7 万星、1.1 年历史，lifetime 均值算出来是
    +157.9 ★/天，直接显示会让人以为它现在每天涨 158 星——这是误导。
    GitHub 不提供任意仓库的真实近期增速，所以：
      · 老项目优先显示「距上次推送」，这才是可验证的活跃度
      · 新项目显示终身均值（此时它接近真实增速）
      · 有跟踪历史时显示我们自己测到的真实增量，这是最可靠的
    """
    if observed_delta is not None and observed_delta > 0:
        return f"跟踪期内 +{observed_delta} ★"

    if repo.age_days > 180:
        # 老项目的终身均值没有参考价值，改用可验证的活跃度
        if repo.idle_days < 1:
            return "今天有推送"
        if repo.idle_days < 30:
            return f"距上次推送 {int(repo.idle_days)} 天"
        return f"⚠ {int(repo.idle_days)} 天无推送"

    if repo.age_days <= 60:
        return f"均速 {repo.velocity:.1f} ★/天"
    # 60 天以上的项目，「终身均速」已经不能代表当前增速，不再展示，
    # 统一给可验证的活跃度。分界必须和 _pick_reason 保持一致——
    # 否则同一份日报里对同一个项目会出现两种口径。
    if repo.idle_days < 1:
        return "今天有推送"
    if repo.idle_days < 30:
        return f"距上次推送 {int(repo.idle_days)} 天"
    return f"⚠ {int(repo.idle_days)} 天无推送"


def render_repo_detail(
    repo: Repo,
    analysis: ReadmeAnalysis,
    *,
    index: int | None = None,
    trend: str = "",
    observed_delta: int | None = None,
) -> str:
    """渲染一条完整条目。"""
    out: list[str] = []
    prefix = f"### {index}. " if index is not None else "### "

    lang = repo.language or "未知语言"
    license_ = repo.license if repo.license not in (None, "NOASSERTION", "NONE") else "无协议"

    out.append(f"{prefix}[{repo.full_name}]({repo.url})")
    out.append("")
    out.append(
        f"`{_fmt_num(repo.stars)} ★` · `{_rate_label(repo, observed_delta)}` · "
        f"`{lang}` · `{license_}` · 创建于 {_age_label(repo.age_days)}前"
    )
    if trend:
        out.append("")
        out.append(f"**跟踪**：{trend}")

    if analysis.tagline:
        out.append("")
        out.append(f"**是什么**：{analysis.tagline}")
    elif repo.description:
        out.append("")
        out.append(f"**是什么**：{repo.description}")

    # 关键信息
    facts: list[str] = []
    if analysis.install_methods:
        kinds = "\u3001".join(dict.fromkeys(m.kind for m in analysis.install_methods))
        facts.append(f"安装方式：{kinds}")
    if analysis.has_lang_requirement:
        facts.append(f"环境要求：{', '.join(analysis.has_lang_requirement)}")
    if analysis.has_api_key_need:
        facts.append("需要 API Key / 环境变量")
    if analysis.has_gpu_need:
        facts.append("涉及 GPU / CUDA")
    if analysis.code_blocks:
        facts.append(f"{analysis.code_blocks} 段代码示例")
    if repo.homepage:
        facts.append(f"[官网]({repo.homepage})")
    if facts:
        out.append("")
        out.append(f"**关键信息**：{' · '.join(facts)}")

    # 质量证据
    if analysis.quality_signals:
        out.append("")
        out.append("**靠不靠谱**：")
        for sig in analysis.quality_signals:
            out.append(f"- {sig}")

    # 上手评估
    out.append("")
    out.append(
        f"**上手成本**：{_friction_label(analysis.friction_score)}"
        f"（{analysis.friction_score}/10，预估 {analysis.setup_minutes} 分钟跑通）"
    )
    if analysis.recommended_action:
        out.append("")
        out.append(f"**建议**：{analysis.recommended_action}")

    if analysis.best_command:
        out.append("")
        out.append("**最快启动命令**：")
        out.append("")
        out.append("```bash")
        out.append(analysis.best_command)
        out.append("```")
    elif analysis.install_methods:
        # 只找到方式没定位到具体命令时，诚实说明而不是编一条
        out.append("")
        out.append("**安装方式**：README 中未定位到可直接执行的命令，建议查看安装章节")

    languages = (repo.analysis.get("extra") or {}).get("languages")
    if languages:
        out.append("")
        out.append(f"**技术栈占比**：{' · '.join(languages)}")

    out.append("")
    return "\n".join(out)


def render_feed_line(repo: Repo, analysis: ReadmeAnalysis, trend: str = "") -> str:
    """渲染动态速览里的一行。"""
    tag = repo.language or "—"
    desc = analysis.tagline or repo.description or "（无描述）"
    desc = desc.replace("\n", " ").strip()
    # 在词边界截断，避免出现 "intel…" 这种断在单词中间的残句
    if len(desc) > 130:
        cut = desc[:130]
        space = cut.rfind(" ")
        if space > 80:
            cut = cut[:space]
        desc = cut.rstrip(" ,;:-") + "…"

    extras: list[str] = []
    if analysis.has_docker:
        extras.append("docker")
    if analysis.has_gpu_need:
        extras.append("需GPU")
    if analysis.has_api_key_need:
        extras.append("需Key")
    extra_txt = f" `[{'/'.join(extras)}]`" if extras else ""

    trend_txt = f" — {trend}" if trend else ""
    return (
        f"- **[{repo.full_name}]({repo.url})** "
        f"`{_fmt_num(repo.stars)}★ {_rate_label(repo)}` `{tag}`{extra_txt}  \n"
        f"  {desc}{trend_txt}"
    )


def render_digest(
    picks: list[DigestEntry],
    feed: list[DigestEntry],
    *,
    stats: dict,
    run_at: datetime | None = None,
    authenticated: bool = True,
    domain_counts: dict[str, int] | None = None,
) -> str:
    """渲染完整日报。"""
    run_at = run_at or datetime.now()
    today = run_at.strftime("%Y-%m-%d")

    out: list[str] = []
    out.append(f"# GitHub 技术动态日报 · {today}")
    out.append("")

    # ---------------- 顶部速览 ----------------
    out.append("## 30 秒速览")
    out.append("")
    total = len(picks) + len(feed)
    # 说清「筛出多少」和「展示多少」的区别：
    # 保留的候选可能远多于展示条数，笼统写「N 个值得看」会让人以为只有 N 个。
    kept = stats.get("final", total)
    if kept > total:
        out.append(
            f"扫描了 **{stats.get('unique', 0)}** 个候选，"
            f"剔除噪音 **{stats.get('filtered_noise', 0)}** 个，"
            f"筛出 **{kept}** 个值得看 —— 本日报展示前 **{total}** 个。"
        )
    else:
        out.append(
            f"扫描了 **{stats.get('unique', 0)}** 个候选，"
            f"剔除噪音 **{stats.get('filtered_noise', 0)}** 个，"
            f"最终 **{total}** 个值得看。"
        )
    out.append("")

    if picks:
        out.append("**今天最值得看的：**")
        out.append("")
        for i, entry in enumerate(picks, 1):
            reason = _pick_reason(entry.repo, entry.analysis)
            out.append(f"{i}. [{entry.repo.full_name}]({entry.repo.url}) — {reason}")
        out.append("")

    if not authenticated:
        out.append(
            "> ⚠ 本次为**未认证**请求，配额有限（60 次/小时），"
            "候选数量会明显偏少。运行 `gh auth login` 后可提升到 5000 次/小时。"
        )
        out.append("")

    if domain_counts:
        parts = "、".join(f"{k} {v} 个" for k, v in sorted(domain_counts.items()))
        out.append(f"> 领域分布：{parts}")
        out.append("")

    out.append("---")
    out.append("")

    # ---------------- 精选详情 ----------------
    if picks:
        out.append(f"## 精选 {len(picks)} 条（值得花时间）")
        out.append("")
        for i, entry in enumerate(picks, 1):
            out.append(
                render_repo_detail(
                    entry.repo,
                    entry.analysis,
                    index=i,
                    trend=entry.trend,
                    observed_delta=entry.observed_delta,
                )
            )
            out.append("---")
            out.append("")

    # ---------------- 动态速览 ----------------
    if feed:
        out.append(f"## 动态速览（{len(feed)} 条，快速扫一眼）")
        out.append("")
        for entry in feed:
            out.append(render_feed_line(entry.repo, entry.analysis, entry.trend))
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        f"*由 github-reader 生成于 {run_at.strftime('%Y-%m-%d %H:%M')}*　"
        f"*简介取自仓库 description（优先）或 README 抽取；"
        f"上手成本、安装方式为 README 结构启发式判断，非人工核实*"
    )
    out.append("")
    return "\n".join(out)


def _pick_reason(repo: Repo, analysis: ReadmeAnalysis) -> str:
    """给速览列表用的极短推荐理由。

    注意：这里也必须用 _rate_label，不能直接引用 repo.velocity。
    踩坑记录：详情区改成「距上次推送 N 天」后，速览区还在显示
    「星速 157.9/天」——同一份日报里两处口径不一致，等于没修。
    """
    rate = _rate_label(repo)

    if analysis.friction_score <= 2 and repo.velocity > 3:
        return f"上手极轻，约 {analysis.setup_minutes} 分钟跑通（{rate}）"
    if repo.age_days <= 21 and repo.velocity > 5:
        return f"新项目爆发中（{int(repo.age_days)} 天 {_fmt_num(repo.stars)}★）"
    if repo.idle_days <= 2 and repo.velocity > 2:
        if analysis.install_methods:
            return f"{rate}，{analysis.install_methods[0].kind} 方式安装"
        return f"{rate}，活跃推进中"
    if analysis.friction_score >= 7:
        return f"{_fmt_num(repo.stars)}★ 但上手重（{analysis.setup_minutes} 分钟），先收藏"
    return f"{_fmt_num(repo.stars)}★，{rate}"


def write_digest(content: str, digest_dir: Path, run_at: datetime | None = None) -> Path:
    """把日报写到 digests/YYYY-MM-DD.md，同时更新 latest.md。"""
    run_at = run_at or datetime.now()
    digest_dir.mkdir(parents=True, exist_ok=True)

    dated = digest_dir / f"{run_at.strftime('%Y-%m-%d')}.md"
    dated.write_text(content, encoding="utf-8")

    latest = digest_dir / "latest.md"
    latest.write_text(content, encoding="utf-8")

    return dated
