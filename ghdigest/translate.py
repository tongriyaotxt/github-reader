"""把日报里的英文摘要与建议翻成中文。

## 为什么是「新增字段」而不是「替换原文」

中文摘要写成独立的 `**一句话**` 字段，英文原文保留在 `**是什么**` 里。三个理由：

1. 机器提取的原文可以随时重新生成，被译文覆盖就永久丢失了。
2. 译文可以单独重新生成，不必重跑抓取（省配额、省时间）。
3. 译文出错时能对照原文排查——只看到一句错译时你无从判断错在哪。

## 确定性边界（重要）

这个模块负责**所有**易错的结构性工作：定位字段、组装批次、解析模型返回、
校验对齐、写回文件。LLM 只负责一件事：把给定的英文句子翻成中文。

这样划分是为了让「翻译」这一步可重复、可测试——否则每次运行的格式、
批大小、失败处理都靠临场发挥，日报会一天一个样。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMConfig, LLMError, chat_json

# 需要翻译的字段：标签 → 用作译文的新标签
#
# 注意 `**建议**` 不在这里：它由工具生成，本来就是中文，翻译它只会得到
# 逐字重复的一行（实测踩到）。is_already_chinese() 是第二道防线，
# 用于仓库 description 本身就是中文的情况。
TRANSLATABLE: dict[str, str] = {
    "是什么": "一句话",
    "描述": "一句话",  # 动态速览里的条目用这个标签
}

_HEADING_RE = re.compile(r"^#{2,4}\s+(?:\d+\.\s*)?\[([\w.-]+/[\w.-]+)\]")

# CJK 统一表意文字
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 中文标点：译文的强信号。英文原文几乎不会出现这些字符，
# 而中文译文哪怕术语全是英文也一定会用它们。
_CJK_PUNCT_RE = re.compile(r"[。，、；：！？（）【】「」]")
# 英文词（技术术语、专有名词、URL 主机名）
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*")


def is_already_chinese(
    text: str, threshold: float = 0.15, word_threshold: float = 0.3
) -> bool:
    """判断文本是否已经是中文。

    为什么需要：日报里的 `**建议**` 字段是工具自己生成的中文，
    再「翻译」一遍只会得到逐字重复的一行（实测：两行完全相同叠在一起）。

    判定用三条规则，任一成立即可：
      1. 含中文标点 —— 译文的强信号，不受术语占比影响
      2. CJK 字符占比达到阈值
      3. 汉字与英文词的数量比达到阈值 —— 兜住「给 coding agent 用的
         Chrome DevTools」这类术语密集、汉字被稀释的短句

    为什么规则 3 不能并进规则 2：那句话的 CJK **字数**占比只有 0.091
    （3 个汉字 / 33 个字符），任何低到能抓住它、又不会误伤英文的阈值都不存在。
    但换成「每个英文词配几个汉字」就有了干净的分离度——实测真实日报的
    34 个中文字段（译文和本身就是中文的源字段）最低 0.429，而 5 个英文
    源字段恒为 0.000（英文里没有汉字）。0.3 落在中间，两边都留了余量。

    已知边界：规则 3 偏向「算中文」。英文句子里夹一个中文名
    （`Xiaohongshu 笔记 downloader for macOS`）会被判成已经是中文。
    这是刻意的取舍——漏判（中文当英文翻）会产出逐字重复的两行，那正是
    这个函数要防的坑；误判只是少一行译文。边界由测试钉住。

    坑：这条规则原先只写在文档里。单元测试用的是带句号的
    「给 coding agent 用的 Chrome DevTools。」，被规则 1 接住了，
    所以规则 2 其实从来没兜住过它——真实日报里那句没有句号，就漏了。
    """
    if not text:
        return False
    if _CJK_PUNCT_RE.search(text):
        return True
    cjk = len(_CJK_RE.findall(text))
    if cjk / max(len(text), 1) >= threshold:
        return True
    words = len(_LATIN_WORD_RE.findall(text))
    return cjk >= 2 and cjk / max(cjk + words, 1) >= word_threshold
_BULLET_ITEM_RE = re.compile(r"^-\s+\*\*\[([\w.-]+/[\w.-]+)\]")
# 动态速览条目：缩进两格的描述文字，捕获 (缩进, 正文)
_FEED_DESC_RE = re.compile(r"^( {2,})(\S.*)$")

SYSTEM_PROMPT = """你是资深开发者，为一份「GitHub 技术动态日报」撰写中文摘要。

读者是想快速了解技术动态的工程师。他只有两分钟，需要一眼看懂「这是什么、值不值得看」。

铁律：
1. 只输出 JSON，格式 {"items":[{"i":序号,"zh":"译文"}]}，序号与输入一致，不增不减。
2. 术语保持英文原样，不要硬翻：agent、prompt、LLM、RAG、MCP、token、embedding、
   inference、harness、CLI、SDK、runtime、framework 等。
3. 保留原文里的事实：星数、时间、安装方式、协议名、限制条件都不能丢也不能改。
4. 「是什么」翻成一句人话，控制在 40 字以内，不要罗列功能。
   坏例子：「一个基于……的……的……的工具」（堆定语）
   好例子：「让 agent 从你的使用习惯里持续自我改进」
5. 「建议」是对行动建议的口语化转述，保留其中的数字和判断（比如「约 20 分钟」）。
6. 原文过短或没有实质内容时，如实翻出即可，不要自行补充想象的信息。
7. 不输出任何解释、前后缀、markdown 代码块。"""


@dataclass
class Unit:
    """一个待翻译单元：日报里某个条目的某个字段。

    context 用于区分「精选详情」和「动态速览」——同一个仓库会同时出现在
    两处，若只用仓库名做 key，速览的译文会被详情的记录误判成「已翻译」，
    导致速览永远翻不出来（测试 test_same_repo_in_both_sections 守着这条）。
    """

    index: int
    repo: str
    field_name: str
    text: str
    context: str = "detail"  # detail | feed
    indent: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.context, self.repo, self.field_name)

    def slot(self) -> tuple[str, str]:
        """幂等判断用的槽位：同一条目的同一字段。"""
        return (self.context, self.repo)


@dataclass
class ParseResult:
    units: list[Unit] = field(default_factory=list)
    # 行号 → 需要插入译文的位置（在该行之后插入）
    insert_after: dict[int, Unit] = field(default_factory=dict)
    already_translated: int = 0


def parse_digest(markdown: str) -> ParseResult:
    """从日报里找出需要翻译的字段。

    幂等性靠**两阶段扫描**实现：

    阶段一：先走一遍全文，收集每个条目已经出现过的译文标签。
    阶段二：再走一遍，把「译文标签还没出现过」的源字段收进待翻译队列。

    为什么必须分两阶段（踩坑记录）：最初想在**一趟**扫描里边走边记录
    「这个条目翻过了」，但源字段总在它自己的译文之前出现——走到源字段时
    还没见过译文，于是每次都把它重新收进队列，重复运行会把中文再翻一遍。
    顺序依赖是这类 bug 的根源，分阶段扫描可以彻底消除它。
    """
    lines = markdown.split("\n")
    zh_labels = set(TRANSLATABLE.values())

    # ---------- 阶段一：扫描已存在的译文 ----------
    present: dict[tuple[str, str], set[str]] = {}
    current_repo: str | None = None
    context = "detail"

    for line in lines:
        if line.startswith("## 动态速览"):
            context, current_repo = "feed", None
            continue
        if line.startswith("## 精选"):
            context = "detail"
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            current_repo, context = heading.group(1), "detail"
            continue
        bullet = _BULLET_ITEM_RE.match(line)
        if bullet:
            current_repo, context = bullet.group(1), "feed"
            continue
        if current_repo is None:
            continue

        stripped = line.strip()
        for zh_label in zh_labels:
            if stripped.startswith(f"**{zh_label}**："):
                present.setdefault((context, current_repo), set()).add(zh_label)
                break

    # ---------- 阶段二：收集待翻译的源字段 ----------
    result = ParseResult()
    current_repo = None
    context = "detail"
    idx = 0

    for lineno, line in enumerate(lines):
        if line.startswith("## 动态速览"):
            context, current_repo = "feed", None
            continue
        if line.startswith("## 精选"):
            context = "detail"
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            current_repo, context = heading.group(1), "detail"
            continue
        bullet = _BULLET_ITEM_RE.match(line)
        if bullet:
            current_repo, context = bullet.group(1), "feed"
            continue
        if current_repo is None:
            continue

        stripped = line.strip()
        slot = (context, current_repo)
        done = present.get(slot, set())

        matched_field = False
        for label, zh_label in TRANSLATABLE.items():
            prefix = f"**{label}**："
            if not stripped.startswith(prefix):
                continue
            matched_field = True
            if zh_label in done:
                result.already_translated += 1
                break
            text = stripped[len(prefix) :].strip()
            # 已经是中文的字段不需要翻译（否则会得到逐字重复的一行）
            if text and not is_already_chinese(text):
                idx += 1
                unit = Unit(
                    index=idx,
                    repo=current_repo,
                    field_name=label,
                    text=text,
                    context=context,
                    indent="",
                )
                result.units.append(unit)
                result.insert_after[lineno] = unit
            elif text:
                result.already_translated += 1
            break

        if matched_field:
            continue

        # 已是译文行则跳过（阶段一已记录）
        if any(stripped.startswith(f"**{z}**：") for z in zh_labels):
            continue

        # 动态速览的缩进描述行
        desc = _FEED_DESC_RE.match(line)
        if desc and not stripped.startswith(("**", "-", ">", "#", "|")):
            text = desc.group(2).strip()
            if len(text) < 20:
                continue
            if "一句话" in done:
                result.already_translated += 1
                continue
            idx += 1
            unit = Unit(
                index=idx,
                repo=current_repo,
                field_name="描述",
                text=text,
                context="feed",
                indent=desc.group(1),
            )
            result.units.append(unit)
            result.insert_after[lineno] = unit

    return result


def build_batch_prompt(units: list[Unit]) -> str:
    """把一批单元组装成一个请求。"""
    payload = [
        {"i": u.index, "repo": u.repo, "field": u.field_name, "text": u.text}
        for u in units
    ]
    return (
        "翻译下面每一条。field 为「是什么」的是项目简介，"
        "为「建议」的是行动建议。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )


def translate_units(
    cfg: LLMConfig,
    units: list[Unit],
    *,
    batch_size: int = 12,
    progress=None,
) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """分批翻译，返回 (译文映射, 错误列表)。

    单批失败不影响其它批次——翻译是可选增强，不该因为一次网络抖动
    就让整份日报没有中文。
    """
    translations: dict[tuple[str, str, str], str] = {}
    errors: list[str] = []

    batches = [units[i : i + batch_size] for i in range(0, len(units), batch_size)]
    for bi, batch in enumerate(batches, 1):
        if progress:
            progress(bi, len(batches), len(batch))
        try:
            payload = chat_json(cfg, SYSTEM_PROMPT, build_batch_prompt(batch))
        except LLMError as exc:
            errors.append(f"第 {bi} 批调用失败：{exc}")
            continue

        items = payload.get("items")
        if not isinstance(items, list):
            errors.append(f"第 {bi} 批返回结构不对（没有 items 数组）")
            continue

        by_index = {u.index: u for u in batch}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            zh = item.get("zh")
            unit = by_index.get(i)
            if unit is None or not isinstance(zh, str) or not zh.strip():
                continue
            translations[unit.key()] = zh.strip()

        missing = [u.index for u in batch if u.key() not in translations]
        if missing:
            errors.append(f"第 {bi} 批缺少 {len(missing)} 条译文（序号 {missing[:6]}）")

    return translations, errors


def apply_translations(markdown: str, translations: dict[tuple[str, str, str], str]) -> str:
    """把译文插到对应字段的下一行。

    纯函数：同样的输入得到同样的输出，便于测试。
    动态速览的条目要保持原有缩进，否则 `**一句话**` 会顶格，破坏列表结构。
    """
    if not translations:
        return markdown

    parsed = parse_digest(markdown)
    if not parsed.insert_after:
        return markdown

    lines = markdown.split("\n")
    # 从后往前插，避免行号位移
    for lineno in sorted(parsed.insert_after, reverse=True):
        unit = parsed.insert_after[lineno]
        zh = translations.get(unit.key())
        if not zh:
            continue
        zh_label = TRANSLATABLE[unit.field_name]
        # 速览条目用两格缩进，详情条目顶格
        indent = unit.indent or ("  " if unit.context == "feed" else "")
        lines.insert(lineno + 1, f"{indent}**{zh_label}**：{zh}")

    return "\n".join(lines)


def translate_file(
    path: Path,
    cfg: LLMConfig,
    *,
    batch_size: int = 12,
    force: bool = False,
    progress=None,
) -> tuple[int, list[str]]:
    """就地翻译一份日报。返回 (翻译条数, 错误列表)。

    如果同目录下有 latest.md，一并同步——否则「跑完看 latest.md」会看到
    一份没有中文的旧文件（实测踩到：日期文件已翻译，latest.md 还停在几小时前）。
    """
    markdown = path.read_text(encoding="utf-8")
    parsed = parse_digest(markdown)

    if not parsed.units:
        return 0, []

    translations, errors = translate_units(
        cfg, parsed.units, batch_size=batch_size, progress=progress
    )
    if not translations:
        return 0, errors

    updated = apply_translations(markdown, translations)
    path.write_text(updated, encoding="utf-8")

    # 同步 latest.md（仅当被翻译的就是某个带 latest 的目录里的日报）
    latest = path.parent / "latest.md"
    if latest.exists() and latest.name != path.name:
        latest.write_text(updated, encoding="utf-8")

    return len(translations), errors
