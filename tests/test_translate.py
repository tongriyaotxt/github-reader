"""翻译模块的测试。

重点测「结构性工作」的正确性——这是易错且必须确定的部分。
LLM 的输出质量不在这里测（那是模型的职责）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghdigest.translate import (
    TRANSLATABLE,
    Unit,
    apply_translations,
    build_batch_prompt,
    is_already_chinese,
    parse_digest,
)

SAMPLE = """# GitHub 技术动态日报 · 2026-09-20

## 30 秒速览

**今天最值得看的：**

1. [acme/tool](https://github.com/acme/tool) — 上手极轻

---

## 精选 2 条（值得花时间）

### 1. [acme/tool](https://github.com/acme/tool)

`840 ★` · `均速 21.9 ★/天` · `Python` · `MIT` · 创建于 8 天前

**跟踪**：★ 首次出现

**是什么**：A fast tool that does something useful for developers.

**关键信息**：安装方式：pip

**建议**：近日仍在活跃提交；上手极轻（约 10 分钟可跑通）

**最快启动命令**：

```bash
pip install acme-tool
```

---

### 2. [acme/other](https://github.com/acme/other)

`2.0k ★` · `今天有推送` · `Go` · `MIT` · 创建于 2 个月前

**是什么**：Another tool for doing different things.

**建议**：上手成本中等（约 30 分钟）

---

## 动态速览（1 条，快速扫一眼）

- **[acme/third](https://github.com/acme/third)** `1.0w★ 今天有推送` `Rust`
  A third tool that does even more things.
"""


class TestParseDigest(unittest.TestCase):
    def test_finds_all_translatable_fields(self):
        parsed = parse_digest(SAMPLE)
        keys = {u.key() for u in parsed.units}
        self.assertEqual(
            keys,
            {
                ("detail", "acme/tool", "是什么"),
                ("detail", "acme/other", "是什么"),
                ("feed", "acme/third", "描述"),
            },
        )

    def test_skips_fields_already_in_chinese(self):
        """坑：`**建议**` 由工具生成，本来就是中文。再翻一遍会得到
        逐字重复的两行（实测：完全相同的句子叠在一起），纯噪音。"""
        parsed = parse_digest(SAMPLE)
        for unit in parsed.units:
            self.assertFalse(
                is_already_chinese(unit.text),
                f"不该把中文送进翻译队列：{unit.text[:40]!r}",
            )
        # 建议字段是中文，应被算作「无需处理」而不是待翻译
        self.assertNotIn("建议", {u.field_name for u in parsed.units})

    def test_assigns_sequential_indices(self):
        parsed = parse_digest(SAMPLE)
        self.assertEqual([u.index for u in parsed.units], [1, 2, 3])

    def test_ignores_command_blocks_and_metadata(self):
        """命令、星数、链接这些机器提取的数据绝不能进翻译队列。"""
        parsed = parse_digest(SAMPLE)
        blob = " ".join(u.text for u in parsed.units)
        self.assertNotIn("pip install", blob)
        self.assertNotIn("840", blob)
        self.assertNotIn("https://", blob)
        self.assertNotIn("★", blob)

    def test_feed_description_is_captured_with_indent(self):
        """动态速览的描述也要翻译，且要记住缩进（否则插进去会顶格）。"""
        parsed = parse_digest(SAMPLE)
        feed = [u for u in parsed.units if u.context == "feed"]
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0].repo, "acme/third")
        self.assertEqual(feed[0].indent, "  ")

    def test_same_repo_in_both_sections_translates_both(self):
        """坑：同一仓库会同时出现在精选和速览里。若只用仓库名做幂等 key，
        速览的译文会被详情的记录误判成「已翻译」而永远翻不出来。"""
        md = """## 精选 1 条

### 1. [acme/tool](https://github.com/acme/tool)

**是什么**：A fast tool for developers.

---

## 动态速览（1 条）

- **[acme/tool](https://github.com/acme/tool)** `1.0w★` `Rust`
  A fast tool for developers, described again in the feed.
"""
        parsed = parse_digest(md)
        keys = {u.key() for u in parsed.units}
        self.assertIn(("detail", "acme/tool", "是什么"), keys)
        self.assertIn(("feed", "acme/tool", "描述"), keys)
        self.assertEqual(len(parsed.units), 2)

        # 两处都应有译文
        out = apply_translations(md, {u.key(): f"译文{u.index}" for u in parsed.units})
        self.assertIn("**一句话**：译文1", out)
        self.assertIn("  **一句话**：译文2", out)

    def test_idempotent_when_already_translated(self):
        """重复运行不能把中文再翻一遍，也不能插出重复字段。"""
        parsed0 = parse_digest(SAMPLE)
        once = apply_translations(SAMPLE, {u.key(): f"译文{u.index}" for u in parsed0.units})
        reparsed = parse_digest(once)
        self.assertEqual(reparsed.units, [])
        self.assertEqual(reparsed.already_translated, len(parsed0.units))

    def test_partial_translation_only_redoes_missing(self):
        parsed0 = parse_digest(SAMPLE)
        first = parsed0.units[0]
        once = apply_translations(SAMPLE, {first.key(): "只翻了一条"})
        reparsed = parse_digest(once)
        keys = {u.key() for u in reparsed.units}
        self.assertNotIn(first.key(), keys)
        self.assertEqual(len(reparsed.units), len(parsed0.units) - 1)
        self.assertEqual(reparsed.already_translated, 1)


class TestApplyTranslations(unittest.TestCase):
    def test_chinese_goes_immediately_after_source(self):
        parsed = parse_digest(SAMPLE)
        unit = next(u for u in parsed.units if u.key() == ("detail", "acme/tool", "是什么"))
        out = apply_translations(SAMPLE, {unit.key(): "一个给开发者用的快速工具。"})
        lines = out.split("\n")
        i = next(
            n
            for n, ln in enumerate(lines)
            if ln.startswith("**是什么**：A fast tool")
        )
        self.assertEqual(lines[i + 1], "**一句话**：一个给开发者用的快速工具。")

    def test_preserves_machine_extracted_data(self):
        """核心不变量：翻译不得改动任何机器提取的内容。"""
        parsed = parse_digest(SAMPLE)
        out = apply_translations(
            SAMPLE, {u.key(): f"译文{u.index}" for u in parsed.units}
        )
        for must_keep in (
            "`840 ★`",
            "`均速 21.9 ★/天`",
            "pip install acme-tool",
            "https://github.com/acme/tool",
            "**跟踪**：★ 首次出现",
            "**关键信息**：安装方式：pip",
            "创建于 8 天前",
        ):
            self.assertIn(must_keep, out)

    def test_english_source_is_kept(self):
        """英文原文必须保留——译文覆盖原文会让重新生成变得不可能。"""
        parsed = parse_digest(SAMPLE)
        unit = next(u for u in parsed.units if u.key() == ("detail", "acme/tool", "是什么"))
        out = apply_translations(SAMPLE, {unit.key(): "一个给开发者用的快速工具。"})
        self.assertIn("A fast tool that does something useful for developers.", out)

    def test_line_count_grows_by_translation_count(self):
        before = len(SAMPLE.split("\n"))
        parsed = parse_digest(SAMPLE)
        out = apply_translations(
            SAMPLE, {u.key(): f"译文{u.index}" for u in parsed.units}
        )
        self.assertEqual(len(out.split("\n")), before + len(parsed.units))

    def test_feed_translation_keeps_indent(self):
        """坑：速览条目的译文若顶格插入，会破坏列表结构。"""
        parsed = parse_digest(SAMPLE)
        feed = next(u for u in parsed.units if u.context == "feed")
        out = apply_translations(SAMPLE, {feed.key(): "第三个做更多事情的工具。"})
        self.assertIn("  **一句话**：第三个做更多事情的工具。", out)

    def test_empty_translations_is_noop(self):
        self.assertEqual(apply_translations(SAMPLE, {}), SAMPLE)

    def test_partial_mapping_skips_missing(self):
        before = len(SAMPLE.split("\n"))
        parsed = parse_digest(SAMPLE)
        out = apply_translations(SAMPLE, {parsed.units[0].key(): "只有一条"})
        self.assertEqual(len(out.split("\n")), before + 1)

    def test_applies_to_both_entries_independently(self):
        """多个条目时行号不能互相干扰（从后往前插入的原因）。"""
        parsed = parse_digest(SAMPLE)
        t1 = next(u for u in parsed.units if u.key() == ("detail", "acme/tool", "是什么"))
        t2 = next(u for u in parsed.units if u.key() == ("detail", "acme/other", "是什么"))
        out = apply_translations(SAMPLE, {t1.key(): "第一条", t2.key(): "第二条"})
        lines = out.split("\n")
        i1 = next(n for n, ln in enumerate(lines) if ln == "**一句话**：第一条")
        i2 = next(n for n, ln in enumerate(lines) if ln == "**一句话**：第二条")
        self.assertLess(i1, i2)
        self.assertTrue(lines[i1 - 1].startswith("**是什么**：A fast tool"))
        self.assertTrue(lines[i2 - 1].startswith("**是什么**：Another tool"))


class TestBatchPrompt(unittest.TestCase):
    def test_includes_index_repo_field_text(self):
        units = [
            Unit(index=1, repo="a/b", field_name="是什么", text="A tool."),
            Unit(index=2, repo="c/d", field_name="建议", text="Easy."),
        ]
        prompt = build_batch_prompt(units)
        for token in ('"i": 1', '"i": 2', "a/b", "c/d", "A tool.", "Easy.", "是什么", "建议"):
            self.assertIn(token, prompt)

    def test_labels_map_source_to_chinese(self):
        self.assertIn("是什么", TRANSLATABLE)
        self.assertEqual(TRANSLATABLE["是什么"], "一句话")
        # 建议字段是工具生成的中文，不该出现在翻译目标里
        self.assertNotIn("建议", TRANSLATABLE)


class TestIsAlreadyChinese(unittest.TestCase):
    def test_detects_chinese(self):
        self.assertTrue(is_already_chinese("近日仍在活跃提交；上手极轻（约 20 分钟可跑通）"))
        self.assertTrue(is_already_chinese("让 agent 持续自我改进的 continual learning 基础设施"))

    def test_detects_short_chinese_sentence_with_mostly_english_terms(self):
        """坑：只用 CJK 占比会误判这类句子——中文比例低但确实是中文。"""
        self.assertTrue(is_already_chinese("给 coding agent 用的 Chrome DevTools。"))
        self.assertTrue(is_already_chinese("SpaceXAI 的 coding agent harness 与 TUI，全屏、可扩展"))

    def test_detects_short_chinese_sentence_without_trailing_punctuation(self):
        """坑：上面那条测试的样本带句号，被规则 1（中文标点）接住了，
        于是规则 2 其实**从来没兜住过它**——文档写着能兜，实际兜不住。

        真实日报里那行没有句号，就漏了：

            **一句话**：给 coding agent 用的 Chrome DevTools

        汉字占比 0.091 < 0.15，被判成「不是中文」。所以这里用**无标点**
        的真实样本测规则 3。"""
        self.assertTrue(is_already_chinese("给 coding agent 用的 Chrome DevTools"))
        self.assertTrue(is_already_chinese("你的 coding agent 所运行的 runtime"))
        self.assertTrue(is_already_chinese("为 IDE 接近度设计的 coding agent"))

    def test_rule3_boundary_errs_toward_chinese(self):
        """钉住规则 3 的边界：英文句子里夹一个中文名，会被判成「已经是中文」。

        这是刻意的取舍，不是 bug：漏判（中文当英文翻）会产出逐字重复的
        两行——那正是这个函数要防的坑；误判（英文当中文跳过）只是少一行译文。
        所以边界偏向「算中文」。把行为钉在这里，避免以后被
        「顺手调严一点」改回重复行。"""
        self.assertTrue(is_already_chinese("Xiaohongshu 笔记 downloader for macOS"))
        # 但纯英文必须仍然是 False，哪怕很长、哪怕带技术术语。
        self.assertFalse(is_already_chinese("A fast tool that does something useful."))
        self.assertFalse(
            is_already_chinese(
                "Continual learning infra for self-improving agents, with "
                "checkpointing, replay buffers and a plugin system."
            )
        )

    def test_rejects_english(self):
        self.assertFalse(is_already_chinese("A fast tool that does something useful."))
        self.assertFalse(is_already_chinese("SpaceXAI's coding agent harness and TUI."))
        self.assertFalse(is_already_chinese("CLI proxy that reduces LLM token consumption"))

    def test_handles_empty(self):
        self.assertFalse(is_already_chinese(""))


class TestRealDigest(unittest.TestCase):
    """在真实日报上验证——合成样本容易漏掉真实格式的意外。"""

    def _digest(self) -> str:
        path = Path(__file__).resolve().parent.parent / "digests" / "2026-09-20.md"
        if not path.exists():
            self.skipTest("没有真实日报可供验证")
        return path.read_text(encoding="utf-8")

    def test_real_digest_roundtrips_without_touching_machine_data(self):
        markdown = self._digest()
        parsed = parse_digest(markdown)
        if not parsed.units:
            self.skipTest("真实日报已全部翻译过，无法在此验证插入")

        fake = {u.key(): f"【{u.index}】" for u in parsed.units}
        out = apply_translations(markdown, fake)
        self.assertEqual(
            len(out.split("\n")), len(markdown.split("\n")) + len(parsed.units)
        )
        for must_keep in ("扫描了 **", "★`", "```bash", "> 领域分布", "*由 ghdigest 生成于"):
            self.assertIn(must_keep, out)
        self.assertEqual(parse_digest(out).units, [])

    def test_real_digest_no_adjacent_duplicate_lines(self):
        """回归：曾经把中文的「建议」再翻一遍，得到**相邻的**逐字重复两行。

        只查相邻重复：像 `**靠不靠谱**：` 这种标签在多个条目里重复出现
        是正常的结构，不是 bug。
        """
        allowed = {"", "---", "```bash", "```"}
        lines = self._digest().split("\n")
        for i in range(1, len(lines)):
            a, b = lines[i - 1].strip(), lines[i].strip()
            if not a or a in allowed or b in allowed:
                continue
            self.assertNotEqual(a, b, f"第 {i + 1} 行与上一行完全重复：{a[:60]!r}")

    def test_real_digest_translated_fields_are_chinese(self):
        """翻过的条目，其译文行应该是中文。"""
        markdown = self._digest()
        zh_lines = [
            ln for ln in markdown.split("\n") if ln.strip().startswith("**一句话**：")
        ]
        if not zh_lines:
            self.skipTest("这份日报还没翻译过")
        for ln in zh_lines:
            text = ln.split("：", 1)[1]
            self.assertTrue(
                is_already_chinese(text), f"译文不是中文：{text[:50]!r}"
            )


class TestLatestSync(unittest.TestCase):
    """坑：--zh-only 只改日期文件，latest.md 停在旧版本。
    用户按 README 说的「直接看 latest.md」会看到一份没有中文的日报。"""

    def test_translate_file_syncs_latest(self):
        import tempfile
        from unittest.mock import patch

        from ghdigest import translate as T

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            dated = d / "2026-09-20.md"
            latest = d / "latest.md"
            dated.write_text(SAMPLE, encoding="utf-8")
            latest.write_text(SAMPLE, encoding="utf-8")

            parsed = parse_digest(SAMPLE)
            fake = {u.key(): f"中文{u.index}" for u in parsed.units}
            with patch.object(T, "translate_units", return_value=(fake, [])):
                count, errors = T.translate_file(dated, cfg=None)  # type: ignore[arg-type]

            self.assertEqual(errors, [])
            self.assertEqual(count, len(fake))
            self.assertIn("**一句话**：中文1", dated.read_text(encoding="utf-8"))
            self.assertIn("**一句话**：中文1", latest.read_text(encoding="utf-8"))

    def test_translate_file_without_latest_still_works(self):
        import tempfile
        from unittest.mock import patch

        from ghdigest import translate as T

        with tempfile.TemporaryDirectory() as td:
            dated = Path(td) / "2026-09-20.md"
            dated.write_text(SAMPLE, encoding="utf-8")
            fake = {u.key(): f"中文{u.index}" for u in parse_digest(SAMPLE).units}
            with patch.object(T, "translate_units", return_value=(fake, [])):
                count, _ = T.translate_file(dated, cfg=None)  # type: ignore[arg-type]
            self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
