"""回归测试。

每个测试都对应一个**实际踩过的坑**，不是形式化的覆盖率凑数：
改动代码时如果不小心把坑踩回来，这里会红。

运行： python -m pytest tests/ -v       （或 python tests/test_ghdigest.py）
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghdigest.analyze import (
    analyze,
    clean_markdown,
    _extract_install_methods,
    _extract_tagline,
    _section_body,
    _snap_to_word,
    _INSTALL_SECTION_RE,
)
from ghdigest.config import Settings
from ghdigest.discover import Repo, _is_noise, _compute_signals
from ghdigest.render import DigestEntry, _rate_label, render_digest, render_feed_line
from ghdigest.state import State


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_repo(**over) -> Repo:
    base = dict(
        full_name="owner/name",
        url="https://github.com/owner/name",
        description="A tool that does something useful for developers",
        stars=1000,
        forks=100,
        open_issues=10,
        language="Python",
        license="MIT",
        created_at=NOW - timedelta(days=60),
        pushed_at=NOW - timedelta(days=1),
        topics=["cli"],
        archived=False,
        is_fork=False,
        homepage=None,
        size_kb=5000,
    )
    base.update(over)
    return Repo(**base)


class TestCleanMarkdown(unittest.TestCase):
    def test_strips_contributor_avatar_wall(self):
        """坑：openclaw 的 README 全是 1500 字符的贡献者头像 HTML 墙，
        不清洗会把摘要提取的注意力预算全吃掉（实测压缩率 9% → 81%）。"""
        wall = "\n".join(
            f'<a href="https://github.com/user{i}"><img src="https://avatars.githubusercontent.com/u/{i}?v=4&s=48" width="48"></a>'
            for i in range(40)
        )
        raw = f"# Title\n\nReal description text here.\n\n{wall}\n"
        cleaned = clean_markdown(raw)
        self.assertLess(len(cleaned), len(raw) * 0.5)
        self.assertIn("Real description text here.", cleaned)
        self.assertNotIn("avatars.githubusercontent.com", cleaned)

    def test_keeps_badge_link_pair(self):
        """坑：过宽的引用式链接正则会把徽章行 [![npm]](url) 整行删掉，
        而它其实是有用链接。"""
        raw = "# T\n\n[![npm]](https://www.npmjs.com/package/foo)\n\nDescription here.\n"
        cleaned = clean_markdown(raw)
        self.assertIn("npmjs.com", cleaned)

    def test_removes_real_link_definitions(self):
        raw = "# T\n\n[doc]: https://example.com/docs\n\nBody text.\n"
        cleaned = clean_markdown(raw)
        self.assertNotIn("example.com/docs", cleaned)

    def test_removes_base64_images(self):
        raw = "# T\n\n![x](data:image/png;base64," + "A" * 200 + ")\n\nBody.\n"
        cleaned = clean_markdown(raw)
        self.assertNotIn("base64", cleaned)


class TestInstallExtraction(unittest.TestCase):
    def test_ignores_dependency_cmd_in_later_section(self):
        """坑（严重）：ai-data-extractor 的 README 后段讲依赖，
        原先扫全文会把 `pip install detect-secrets` 当成它自己的安装命令。
        用户照着敲会装错东西。"""
        readme = """# ai-data-extractor

Extract chat history from AI coding assistants into JSONL.

## Install

```bash
pip install ai-data-extractor
```

## How it works

It uses a scanner. Install its dependency:

```bash
pip install detect-secrets --break-system-packages
```

## Usage

Run it.
"""
        methods, best = _extract_install_methods(clean_markdown(readme))
        self.assertIn("ai-data-extractor", best)
        self.assertNotIn("detect-secrets", best)

    def test_rejects_conversational_code_block(self):
        """坑：x64dbg-mcp-server 把对话示例当成「最快启动命令」输出。"""
        readme = """# MCP Server

Control the debugger from an AI assistant.

## Usage

```text
You: Load calc.exe and break at the entry point
Assistant: Breakpoint set at 0x401000
```
"""
        methods, best = _extract_install_methods(clean_markdown(readme))
        self.assertEqual(best, "")
        self.assertEqual(methods, [])

    def test_rejects_json_config_block(self):
        readme = """# Thing

## Install

```json
{
  "name": "thing",
  "version": "1.0.0"
}
```

```bash
npm install -g thing
```
"""
        methods, best = _extract_install_methods(clean_markdown(readme))
        self.assertIn("npm install", best)
        self.assertNotIn("version", best)

    def test_detects_multiple_methods(self):
        readme = """# Tool

## Installation

```bash
brew install tool
```

```bash
npm install -g tool
```

```bash
docker run tool
```
"""
        methods, _ = _extract_install_methods(clean_markdown(readme))
        kinds = {m.kind for m in methods}
        self.assertEqual(kinds, {"brew", "npm", "docker"})
        # docker 最省事，应排最前
        self.assertEqual(methods[0].kind, "docker")

    def test_word_download_is_not_install_method(self):
        """坑：宽松正则把散文里的 'Download the latest release' 判成安装方式。"""
        readme = """# Tool

## Getting started

Download the latest release from the releases page and run it.
"""
        methods, _ = _extract_install_methods(clean_markdown(readme))
        self.assertEqual(methods, [])

    def test_crlf_readme_does_not_join_lines(self):
        """坑（关键）：真实 README 用 CRLF 换行。正则里 [^\\n|] 这种字符类
        会把 \\r 也吃进去，导致跨行匹配，把不相关的行串成一条命令。
        实测：ai-data-extractor 因此把隐私章节的
        `pip install detect-secrets` 当成了自己的安装命令。
        这个 bug 用 LF 写测试永远抓不到。"""
        readme = (
            "# Tool\n"
            "\n"
            "## Install\n"
            "\n"
            "```bash\n"
            "curl -fsSL https://example.com/install.sh | sh\n"
            "```\n"
            "\n"
            "## Privacy\n"
            "\n"
            "```bash\n"
            "pip install detect-secrets --break-system-packages\n"
            "```\n"
        )
        crlf = readme.replace("\n", "\r\n")
        methods, best = _extract_install_methods(clean_markdown(crlf))
        kinds = {m.kind for m in methods}
        self.assertIn("script", kinds)
        self.assertNotIn("pip", kinds)
        self.assertIn("install.sh", best)

    def test_recognises_run_command_for_script_tools(self):
        """坑：ai-data-extractor 没有 install 自己这一步，用法就是
        `python extract.py`。不认「运行」类命令就会误判成没找到安装说明。"""
        readme = """# Extractor

## Quick start

```bash
python extract.py --all
```
"""
        methods, best = _extract_install_methods(clean_markdown(readme))
        self.assertEqual(best, "python extract.py --all")
        self.assertIn("python", {m.kind for m in methods})

    def test_region_not_truncated_by_comment_in_code_block(self):
        """坑：代码块里的 shell 注释（`# 安装依赖`）长得像标题，
        会把章节正文截断成 1 个字符（实测 region len = 1）。"""
        readme = """# Tool

## Install

```bash
# No dependencies needed
python run.py
```

## Next section
"""
        cleaned = clean_markdown(readme)
        m = _INSTALL_SECTION_RE.search(cleaned)
        body = _section_body(cleaned, m)
        self.assertGreater(len(body), 20)
        self.assertIn("python run.py", body)
        self.assertNotIn("Next section", body)


class TestTagline(unittest.TestCase):
    def test_description_takes_priority(self):
        """策略核心：description 是作者专门写的定位句，优先于 README 抽取。

        抽查 22 个真实样本后改的——README 抽取会撞上
        「求 star」「读 AGENTS.md」「讲协议」这类内容。
        """
        readme = """# Tool

If Tool worked for you, a star on GitHub is the simplest way to support the project.

## More

Some other paragraph that is long enough to be considered a candidate sentence.
"""
        tagline = _extract_tagline(
            clean_markdown(readme), "A precise one-line description from GitHub", "a/Tool"
        )
        self.assertEqual(tagline, "A precise one-line description from GitHub")

    def test_rejects_boilerplate(self):
        """坑：microsoft/mcp 的摘要成了 'For more details, visit the official
        MCP website.' —— 零信息量的页脚话术。"""
        readme = """# MCP

For more details, visit the official MCP website.

## Overview

This project implements the Model Context Protocol server for .NET applications.
"""
        # 不给 description，走 README 兜底路径
        tagline = _extract_tagline(clean_markdown(readme), "", "microsoft/mcp")
        self.assertNotIn("For more details", tagline)
        self.assertIn("Model Context Protocol", tagline)

    def test_rejects_support_request(self):
        """坑：nanobot 的摘要成了「求 star」的话。"""
        readme = """# nanobot

If nanobot worked for you, a star on GitHub is the simplest way to support the project.

## Install

Run it.
"""
        tagline = _extract_tagline(clean_markdown(readme), "", "HKUDS/nanobot")
        self.assertNotIn("star on GitHub", tagline)

    def test_prefers_long_sentence_over_short(self):
        """短句多是导航词，长句才是定位描述。"""
        readme = """# Project

Install it.

A comprehensive toolkit for building and deploying distributed systems with minimal configuration overhead.
"""
        tagline = _extract_tagline(clean_markdown(readme), "")
        self.assertIn("comprehensive toolkit", tagline)

    def test_snaps_to_word_boundary(self):
        """坑：截断产生 'intel…' 这种断在单词中间的残句。

        这里直接测 _snap_to_word —— 词边界处理是它的职责。
        端到端行为（拆句后不再需要截断）另见下面一个测试。
        """
        text = (
            "This project is a comprehensive toolkit for building distributed systems "
            "with minimal configuration overhead and strong defaults for teams."
        )
        for limit in (40, 60, 80, 100, 320):
            out = _snap_to_word(text, limit)
            if len(text) <= limit:
                self.assertEqual(out, text)
                continue
            self.assertTrue(out.endswith("…"), f"limit={limit} 应被截断：{out!r}")
            body = out[:-1]
            self.assertLessEqual(len(body), limit)
            # 关键：不能断在单词中间
            self.assertRegex(body, r"[A-Za-z.]$", f"limit={limit} 断在单词中间：{body[-15:]!r}")
            # 除了结尾标点，末尾必须是一个完整单词
            last_word = body.rstrip(".").split()[-1]
            self.assertTrue(
                last_word in text,
                f"limit={limit} 末尾 {last_word!r} 不是原文中的完整单词",
            )

    def test_long_paragraph_splits_into_sentences(self):
        """坑：把整段当一个候选评分时，段落越长长度加分越低
        （>300 字符直接 0 分），于是长段落里藏着的好句子会被压分淘汰
        —— 一种隐性的长度惩罚。拆句后应能选出完整的单句。"""
        sentence = (
            "This project is a comprehensive toolkit for building distributed "
            "systems with minimal configuration overhead and strong defaults."
        )
        long_readme = "# P\n\n" + " ".join([sentence] * 4) + "\n"
        lt = _extract_tagline(clean_markdown(long_readme), "")

        self.assertEqual(lt, sentence)  # 拆句后正好选出完整一句，无需截断
        self.assertNotIn("…", lt)
        self.assertNotIn("#", lt)

    def test_multi_paragraph_picks_single_clean_sentence(self):
        """多段文本时，每段独立成候选，应返回干净的单句，不拼接、不带标题记号。"""
        sentence = (
            "This project is a comprehensive toolkit for building distributed "
            "systems with minimal configuration overhead and strong defaults."
        )
        multi = "# P\n\n" + "\n\n".join([sentence] * 6) + "\n"
        t = _extract_tagline(clean_markdown(multi), "")
        self.assertEqual(t, sentence)
        self.assertNotIn("#", t)

    def test_falls_back_to_description(self):
        readme = "# P\n\nFor more information, see the docs.\n"
        tagline = _extract_tagline(clean_markdown(readme), "Fallback description text")
        self.assertEqual(tagline, "Fallback description text")

    def test_rejects_install_instructions(self):
        """坑：摘要抽到了安装说明而不是定位描述。
        实测样本包括
          "Prebuilt binaries are published for macOS, Linux, and Windows:"
          "Download the .dmg from Releases, drag to Applications, done."
          "The Makefile has a few rules for common operations needed during development."
        """
        for bad in (
            "Prebuilt binaries are published for macOS, Linux, and Windows:",
            "Download the .dmg from Releases, drag to Applications, done.",
            "The Makefile has a few rules for common operations needed during development.",
            "Use the guided setup or native plugin commands.",
        ):
            readme = f"# Tool\n\n{bad}\n\n## More\n"
            tagline = _extract_tagline(clean_markdown(readme), "Real description of the tool")
            self.assertNotEqual(tagline, bad, f"不该把安装说明当摘要：{bad}")

    def test_prefers_sentence_with_project_name(self):
        """提到项目名的句子通常是定位句。"""
        readme = """# OpenViking

Some unrelated preamble text that is fairly long but says nothing useful at all.

OpenViking is an open-source context database for AI agents.
"""
        tagline = _extract_tagline(clean_markdown(readme), "", "volcengine/OpenViking")
        self.assertIn("OpenViking is an open-source context database", tagline)


class TestIdleDays(unittest.TestCase):
    def test_analyze_computes_signals_itself(self):
        """坑：idle_days 原先只在 discover() 里算，直接调 analyze() 时
        还是默认值 9999，导致「昨天刚推送」被判成「两周没推送」。
        依赖调用顺序是脆弱的。

        这里必须用**真实时钟**造 pushed_at：analyze() 内部的
        ensure_signals() 不接受外部传入 now，用的是 datetime.now()。
        拿固定的 NOW 常量去造「1 天前」，随着日历往前走会变成
        「4.9 天前」，测试自己变红（实测踩到）。
        """
        repo = make_repo(pushed_at=datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual(repo.idle_days, 9999.0)  # 未计算时的默认值

        a = analyze(repo, "# T\n\nSome description of the project here.\n")
        self.assertLess(repo.idle_days, 2)
        self.assertNotIn("无推送", a.recommended_action)

    def test_stale_repo_flagged(self):
        repo = make_repo(pushed_at=NOW - timedelta(days=100))
        a = analyze(repo, "# T\n\nSome description of the project here.\n")
        self.assertIn("停滞", a.recommended_action)


class TestNoiseFilter(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_awesome_list_rejected(self):
        repo = make_repo(full_name="vinta/awesome-python")
        noise, reason = _is_noise(repo, self.settings)
        self.assertTrue(noise)

    def test_topic_blocklist(self):
        repo = make_repo(topics=["awesome-list", "python"])
        noise, _ = _is_noise(repo, self.settings)
        self.assertTrue(noise)

    def test_real_tool_kept(self):
        repo = make_repo(full_name="acme/fast-parser", topics=["parser", "rust"])
        noise, _ = _is_noise(repo, self.settings)
        self.assertFalse(noise)

    def test_archived_rejected(self):
        repo = make_repo(archived=True)
        noise, reason = _is_noise(repo, self.settings)
        self.assertTrue(noise)
        self.assertIn("归档", reason)


class TestRateLabel(unittest.TestCase):
    def test_old_repo_does_not_show_misleading_rate(self):
        """坑：OpenSpec 有 7 万星、1.1 年历史，lifetime 均值算出来
        +157.9 ★/天，显示出来会让人以为它现在每天涨 158 星。"""
        repo = make_repo(stars=70000, created_at=NOW - timedelta(days=400))
        repo.ensure_signals(NOW)
        label = _rate_label(repo)
        self.assertNotIn("/天", label)
        self.assertIn("推送", label)

    def test_new_repo_shows_rate(self):
        repo = make_repo(stars=900, created_at=NOW - timedelta(days=10))
        repo.ensure_signals(NOW)
        label = _rate_label(repo)
        self.assertIn("/天", label)

    def test_observed_delta_takes_priority(self):
        repo = make_repo(stars=70000, created_at=NOW - timedelta(days=400))
        repo.ensure_signals(NOW)
        label = _rate_label(repo, observed_delta=250)
        self.assertIn("+250", label)

    def test_summary_section_has_no_misleading_rate(self):
        """坑：详情区改成「距上次推送」后，速览区还在用 repo.velocity，
        同一份日报里两处口径不一致——等于没修。"""
        repo = make_repo(stars=70000, created_at=NOW - timedelta(days=400))
        repo.ensure_signals(NOW)
        analysis = analyze(repo, "# T\n\nA useful tool for developers to do things.\n")
        entry = DigestEntry(repo=repo, analysis=analysis)
        out = render_digest([entry], [], stats={})
        self.assertNotIn("星速", out)
        self.assertNotIn("157", out)
        self.assertNotIn("/天", out)


class TestState(unittest.TestCase):
    def test_consecutive_days(self):
        s = State(path=Path("__nonexistent_state__.json"))
        s.observe("a/b", 100, "2026-09-18")
        s.observe("a/b", 150, "2026-09-19")
        h, is_new = s.observe("a/b", 200, "2026-09-20")
        self.assertEqual(h.consecutive_days, 3)
        self.assertEqual(h.seen_count, 3)
        self.assertTrue(is_new)

    def test_gap_resets_streak(self):
        s = State(path=Path("__nonexistent_state__.json"))
        s.observe("a/b", 100, "2026-09-01")
        s.observe("a/b", 150, "2026-09-02")
        h, _ = s.observe("a/b", 200, "2026-09-10")  # 断了
        self.assertEqual(h.consecutive_days, 1)

    def test_same_day_rerun_does_not_double_count(self):
        """同一天重跑：不计数，且必须告知调用方「这不是新的一天」，
        否则当天两次运行之间的星数波动会被当成增长（噪音冒充信号）。"""
        s = State(path=Path("__nonexistent_state__.json"))
        s.observe("a/b", 100, "2026-09-20")
        h, is_new = s.observe("a/b", 120, "2026-09-20")
        self.assertEqual(h.seen_count, 1)
        self.assertEqual(h.stars_last, 120)  # 星数要刷新到最新
        self.assertFalse(is_new)  # 但不是跨天的新观察

    def test_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s1 = State(path=p)
            s1.observe("x/y", 42, "2026-09-20")
            s1.save("2026-09-20")

            s2 = State(path=p)
            self.assertIn("x/y", s2.history)
            self.assertEqual(s2.history["x/y"].stars_first, 42)
            self.assertEqual(s2.last_run, "2026-09-20")


class TestRender(unittest.TestCase):
    def test_digest_renders_both_layers(self):
        repo = make_repo()
        analysis = analyze(repo, "# Tool\n\nA useful tool for developers to do things.\n")
        entry = DigestEntry(repo=repo, analysis=analysis, trend="★ 首次出现")

        out = render_digest(
            [entry], [entry], stats={"unique": 10, "filtered_noise": 3}
        )
        self.assertIn("30 秒速览", out)
        self.assertIn("精选 1 条", out)
        self.assertIn("动态速览", out)

    def test_unauthenticated_warning(self):
        out = render_digest([], [], stats={}, authenticated=False)
        self.assertIn("未认证", out)

    def test_feed_line_truncates_at_word_boundary(self):
        repo = make_repo()
        analysis = analyze(repo, "# T\n\n" + "supercalifragilistic " * 30)
        line = render_feed_line(repo, analysis)
        self.assertNotIn("supercalifragilist…", line)


class TestFrictionScoring(unittest.TestCase):
    def test_gpu_raises_friction(self):
        plain = analyze(
            make_repo(), "# T\n\n## Install\n\n```bash\npip install x\n```\n\nA tool for developers.\n"
        )
        gpu = analyze(
            make_repo(),
            "# T\n\n## Install\n\n```bash\npip install x\n```\n\n"
            "Requires CUDA and an NVIDIA GPU with 24GB VRAM. A tool for developers.\n",
        )
        self.assertGreater(gpu.friction_score, plain.friction_score)
        self.assertGreater(gpu.setup_minutes, plain.setup_minutes)

    def test_casual_gpu_mention_is_not_a_requirement(self):
        """坑：GPU 判定原先扫全文，README 里一句「也支持 NVIDIA 显卡」
        就让纯 CPU 工具被标上「需GPU」。误导性的标签会让人直接跳过好项目。"""
        readme = (
            "# Tool\n\n"
            "## Install\n\n```bash\npip install x\n```\n\n"
            "## Performance notes\n\n"
            "Benchmarks were collected on a machine with an NVIDIA RTX 4090 GPU.\n"
        )
        a = analyze(make_repo(), readme)
        self.assertFalse(a.has_gpu_need)

    def test_no_readme_is_flagged(self):
        a = analyze(make_repo(), None)
        self.assertFalse(a.has_readme)
        self.assertIn("信息不足", a.recommended_action)

    def test_docker_lowers_friction(self):
        docker = analyze(
            make_repo(), "# T\n\n## Install\n\n```bash\ndocker run -p 8080:8080 tool\n```\n\nA tool.\n"
        )
        source = analyze(
            make_repo(),
            "# T\n\n## Install\n\n```bash\ngit clone https://github.com/a/b\n```\n\nA tool.\n",
        )
        self.assertLess(docker.friction_score, source.friction_score)

    def test_run_only_script_tool_is_light(self):
        """坑：纯脚本类工具（`python xxx.py` 就能跑）曾落到
        「没找到安装说明」的兜底分支，被判成 30 分钟，
        但它们其实比 git clone + make 轻得多。"""
        # README 要足够长，否则会触发另一个独立维度「文档偏薄」的惩罚，
        # 干扰本测试想验证的东西。用程序生成而不是手写，避免以后改字数。
        body = (
            "Extract is a command line tool that reads data out of the local "
            "stores used by various developer applications and writes a single "
            "normalized file for later analysis. "
        ) * 4
        script_tool = analyze(
            make_repo(), f"# T\n\n## Quick start\n\n```bash\npython extract.py --all\n```\n\n{body}"
        )
        source_tool = analyze(
            make_repo(),
            f"# T\n\n## Install\n\n```bash\ngit clone https://github.com/a/b\n```\n\n{body}",
        )
        self.assertGreater(script_tool.cleaned_len, 500)  # 确认没踩到文档偏薄
        self.assertLess(script_tool.friction_score, source_tool.friction_score)
        self.assertLessEqual(script_tool.setup_minutes, 15)

    def test_thin_readme_raises_friction(self):
        """README 过短本身就是上手障碍——得靠读源码摸索。"""
        thin = analyze(make_repo(), "# T\n\nA tool.\n")
        rich = analyze(
            make_repo(),
            "# T\n\n## Install\n\n```bash\npip install x\n```\n\n"
            + "A tool that does something useful. " * 40,
        )
        self.assertGreater(thin.friction_score, rich.friction_score)

    def test_new_project_not_penalised_for_no_releases(self):
        """坑：8 天前创建的项目显示「近 30 天无新版本」——
        它当然没有，因为刚诞生。列在「靠不靠谱」下面像负面信号。"""
        repo = make_repo(created_at=NOW - timedelta(days=8), pushed_at=NOW - timedelta(days=1))
        a = analyze(repo, "# T\n\nA tool for developers.\n", fetch_extra={"releases_30d": 0})
        self.assertNotIn("近 30 天无新版本", " ".join(a.quality_signals))

    def test_old_project_flagged_for_no_releases(self):
        repo = make_repo(created_at=NOW - timedelta(days=400))
        a = analyze(repo, "# T\n\nA tool for developers.\n", fetch_extra={"releases_30d": 0})
        self.assertIn("近 30 天无新版本", " ".join(a.quality_signals))


if __name__ == "__main__":
    unittest.main(verbosity=2)
