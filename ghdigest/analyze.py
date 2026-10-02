"""README 启发式分析。

目标：回答一个具体问题——**「这个项目值不值得我花时间，要花多少时间」**。

做法是纯启发式（不依赖 LLM，因此免费、离线可跑、结果稳定可复现）：
  1. 清洗：剥掉 HTML 块、base64 图片、徽章、贡献者头像墙
     （实测踩坑：openclaw 的 README 里全是 1500 字符的贡献者头像 <a><img> 墙，
      不清洗会把摘要提取的注意力预算全吃掉）
  2. 提取：定位描述、安装方式、代码示例、章节骨架
  3. 判定：上手成本、首次见效时间、质量证据、是否值得看

这些启发式理解不了语义，但 README 的**结构信息量很大**：
有没有 docker 一行启动、要不要 API key、是不是先配 GPU，
足以在点开仓库之前做出判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .discover import Repo

# --------------------------------------------------------------- 清洗

_HTML_TAG_RE = re.compile(r"<[^>]{1,4000}>")
_HTML_BLOCK_RE = re.compile(
    r"<(details|summary|div|table|p|picture|picture|h[1-6]|br|center|a)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BADGE_RE = re.compile(
    r"!\[[^\]]*\]\([^)]*(shields\.io|badge|badgen|travis|circleci|codecov|"
    r"actions/workflows|opencollective|buymeacoffee|ko-fi|star-history|"
    r"contrib\.rocks|visitor|count)[^)]*\)",
    re.IGNORECASE,
)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 只匹配真正的「引用式链接定义」，形如行首 [label]: url
# 注意：不能在 label 里允许 ! 和 ]，否则会把徽章行 [![npm]](url) 误删。
# 实测踩坑：这条规则一旦过宽，会吃掉 README 里的徽章-链接对。
_LINK_REF_RE = re.compile(r"^\[[^\]!\n]{1,80}\]:\s+\S+\s*$", re.MULTILINE)
_BASE64_RE = re.compile(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]{40,}")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_COMMENT_SPAN_RE = re.compile(r"<a\s[^>]*>\s*<img[^>]*>\s*</a>", re.IGNORECASE)


def clean_markdown(text: str) -> str:
    """把 README 里对「快速理解」无帮助的内容剥掉。"""
    # 统一换行符。实测踩坑：真实 README 多为 CRLF，残留的 \r 会让
    # 所有用 $ 锚点的多行正则静默失配，也会让 [^\n] 类字符匹配跨行。
    # 在这里一次性规范化，比在每个正则里各自防御可靠。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text = _COMMENT_RE.sub("", text)
    text = _BASE64_RE.sub("", text)
    # 贡献者头像墙：成对的 <a><img></a>，通常一行几百到上千字符
    text = _HTML_COMMENT_SPAN_RE.sub("", text)
    text = _HTML_BLOCK_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _BADGE_RE.sub("", text)
    text = _LINK_REF_RE.sub("", text)
    # 剩下的图片：换成占位标记，保留「这里有张图」的信息但不占空间
    text = _IMG_RE.sub("[图]", text)
    # 折叠过多空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 压缩行内多余空格。
    # 用 [^\S\n]（「是空白但不是换行」）而不是 [ \t]：
    # 语义更清楚，也避免将来有人在字符类里加入会误伤换行的东西。
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    return text.strip()


def strip_code_blocks(text: str) -> str:
    """用于提取散文的部分——去掉代码块，避免命令行文本污染摘要。"""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


# --------------------------------------------------------------- 结构提取

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]{1,80})`")


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """返回代码块在文本中的区间，用于排除「伪标题」。"""
    return [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]


def _in_fence(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)

_INSTALL_SECTION_RE = re.compile(
    r"^#{1,4}\s*.*(install|installation|getting\s*started|get\s*started|quick\s*start|"
    r"quickstart|setup|usage|run|开始|安装|快速|使用|运行).*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class InstallMethod:
    kind: str  # docker / pip / npm / cargo / go / brew / binary / source / script / other
    command: str
    line: int = 0


@dataclass
class ReadmeAnalysis:
    """一篇 README 的结构化解读结果。"""

    has_readme: bool = False
    cleaned_len: int = 0
    headings: list[str] = field(default_factory=list)
    tagline: str = ""  # 一句话定位
    install_methods: list[InstallMethod] = field(default_factory=list)
    best_command: str = ""  # 最可信的「最快启动命令」
    code_blocks: int = 0
    has_usage_example: bool = False
    has_architecture: bool = False
    has_comparison: bool = False
    has_benchmark: bool = False
    has_screenshot: bool = False
    has_api_key_need: bool = False
    has_gpu_need: bool = False
    has_docker: bool = False
    has_lang_requirement: list[str] = field(default_factory=list)
    friction_score: int = 0  # 越高越难上手
    setup_minutes: int = 5
    quality_signals: list[str] = field(default_factory=list)
    recommended_action: str = ""


# 各类安装方式的识别规则：命令片段 → 归类
#
# 这些正则必须足够严。实测踩坑：宽松的 /download|releases\// 会把
# 「Download the latest release」这种散文判成安装命令，
# 而 /install\.ps1/ 又会把文档里提到脚本的句子也算进去。
# 宁可漏判（回落到「需自行阅读安装说明」），也不要误判出错误命令——
# 用户会照着敲。
_INSTALL_PATTERNS = [
    ("docker", re.compile(r"^\s*\$?\s*docker\s+(run|compose|pull)\b", re.I)),
    ("pip", re.compile(r"^\s*\$?\s*(pip|pip3|uv)\s+(install|add|sync)\b", re.I)),
    ("npm", re.compile(r"^\s*\$?\s*(npm|pnpm|yarn|bun)\s+(i|install|add|create)\b", re.I)),
    ("cargo", re.compile(r"^\s*\$?\s*cargo\s+(install|add|build|run)\b", re.I)),
    ("go", re.compile(r"^\s*\$?\s*go\s+(install|get|run|build)\b", re.I)),
    ("brew", re.compile(r"^\s*\$?\s*brew\s+(install|tap)\b", re.I)),
    ("winget", re.compile(r"^\s*\$?\s*(winget|scoop|choco)\s+install\b", re.I)),
    (
        "script",
        # 字符类必须同时排除 \n 和 \r。实测踩坑：真实 README 用 CRLF 换行，
        # 只排除 \n 会让 [^\n|]{0,120} 吃掉 \r 跨行匹配，
        # 把不相关的行串成一条命令（于是 detect-secrets 被当成了安装命令）。
        re.compile(
            r"(curl|wget|irm|iwr)[^\r\n|]{0,120}\|\s*(sudo\s+)?(ba)?sh\b"
            r"|(install|setup)\.(sh|ps1)\b",
            re.I,
        ),
    ),
    # 只认真正的可执行产物扩展名，不认「download」这个词
    (
        "binary",
        re.compile(r"\.(AppImage|dmg|exe|msi|deb|rpm|apk|zip|tar\.gz|tgz)\b|releases/download/", re.I),
    ),
    ("source", re.compile(r"^\s*\$?\s*git\s+clone\b", re.I)),
    ("make", re.compile(r"^\s*\$?\s*(make|cmake|ninja)\s+\S", re.I)),
    # 「运行」类：本身不安装任何东西，但对「最快跑起来」同样有用。
    # 实测踩坑：ai-data-extractor 的 README 没有 pip install 自己，
    # 它的用法就是 `python extract.py`——不认这类命令就会回退扫全文，
    # 反而把隐私章节的 `pip install detect-secrets` 当成安装命令。
    ("uvx", re.compile(r"^\s*\$?\s*(uvx|npx|pnpx|bunx)\s+\S", re.I)),
    ("python", re.compile(r"^\s*\$?\s*python3?\s+[\w./-]+\.py\b", re.I)),
]

# 哪些类别代表「真的安装了东西」——用于挑选最可信的启动命令
_REAL_INSTALL_KINDS = {
    "docker",
    "pip",
    "npm",
    "cargo",
    "go",
    "brew",
    "winget",
    "script",
    "binary",
    "source",
    "make",
}

# 这些代码块就算含安装命令也不该当「启动命令」推荐——它们是配置或脚本内容
_NON_SHELL_HINTS = re.compile(
    r"^\s*(\{|\[|"                     # JSON / TOML 起始
    r"[\w.-]+\s*[:=]\s*\S|"            # key: value / key=value（YAML/env/配置）
    r"//|/\*|<!--|"                    # 注释起始
    r"You:|User:|Assistant:|"          # 对话示例
    r"#!\/)",                          # shebang 的完整脚本
    re.I,
)


def _looks_like_shell(text: str) -> bool:
    """粗略判断代码块内容是否为「可执行命令」而非配置/对话/脚本正文。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return False
    # 对话示例几乎一定含 "You:" / "User:" 这类角色标记
    if re.search(r"^\s*(You|User|Human|Assistant|AI)\s*:", text, re.I | re.M):
        return False
    # 配置块特征：大量 key = value 且没有明显命令。
    # 只对非注释行计数——否则以注释开头的 bash 块（很常见）会被误判成配置。
    code_lines = [
        ln for ln in lines if not ln.strip().startswith(("#", "//", ";", "<!--"))
    ]
    if not code_lines:
        return False
    config_like = sum(1 for ln in code_lines if _NON_SHELL_HINTS.match(ln))
    if config_like >= max(2, len(code_lines) * 0.6):
        return False
    return True

_ARCH_KEYWORDS = (
    "architecture",
    "how it works",
    "design",
    "internals",
    "架构",
    "原理",
)
_COMPARE_KEYWORDS = ("comparison", "vs ", "alternatives", "why not", "对比", "竞品")
_BENCH_KEYWORDS = ("benchmark", "performance", "latency", "throughput", "基准", "性能")

_LANG_REQ_PATTERNS = [
    ("Node.js", re.compile(r"node(\.js)?\s*(>=|≥|v)?\s*\d{2,}", re.I)),
    ("Python", re.compile(r"python\s*(>=|≥|3\.\d+)", re.I)),
    ("Rust", re.compile(r"rust(c)?\s*(>=|≥|1\.\d+)", re.I)),
    ("Go", re.compile(r"\bgo\s*(>=|≥|1\.\d+)", re.I)),
    ("Java", re.compile(r"\b(jdk|java)\s*(>=|≥|17|21|11)", re.I)),
    # 「requires Python 3.x」「Python 3.9+ required」这类自然语序。
    # 实测踩坑：只匹配 "python 3.x" 会漏掉 README 里写的
    # "python --version # 3.9+ required"，于是判成没有环境要求。
    ("Python", re.compile(r"python[^\n]{0,20}\d\.\d+\s*\+?", re.I)),
]


# 「零信息量」的套话。实测踩坑：microsoft/mcp 的 README 开头是
# "For more details, visit the official MCP website."，被当成摘要输出。
_LOW_VALUE_RE = re.compile(
    r"^(for more (details|information)|see the|visit the|read the|"
    r"check out the|learn more|refer to|please (see|visit|refer)|"
    r"this (repo|repository) (is|contains)|documentation (is )?available|"
    r"welcome to|table of contents|getting started$|installation$|"
    r"more (details|info) (at|in|on))",
    re.IGNORECASE,
)

# 「这是什么」的反面：安装说明、贡献指南、维护者备忘、求关注。
_NOT_A_TAGLINE_RE = re.compile(
    r"^\s*("
    r"(prebuilt|pre-built|binary|binaries)\b|"
    r"(download|install|run|build)\s+(the|it|this|from|with|using)\b|"
    r"(docker|npm|pip|brew|cargo|git)\s+(run|install|clone|pull|build)\b|"
    r"```|"
    r"(the\s+)?(makefile|script|repository|repo|project)\s+(has|contains|includes|is\s+for)\b|"
    r"(contribut|maintainer|developer)\w*\s+(guide|note|instruction|doc)\b|"
    r"(choose|pick|select|follow)\s+(one|the|your)\b|"
    r"you\s+(can|should|need|must|already)\b|"
    r"(this|the)\s+(section|chapter|page|folder|directory)\b|"
    # 代码示例/使用说明的引导语。实测踩坑：
    # "As a minimal example, start Reef as a pure inference server:"
    # "bin/ (notably bin/protoc) can download and run."
    r"as\s+(a|an|the)\s+\w+\s+example\b|"
    r"^\S*/\S*\s|"                              # 以路径开头（bin/protoc、src/main）
    r"(start|launch|invoke)\s+\w+\s+as\s+(a|an)\b|"
    r"(example|examples|usage|step\s*\d)\s*[:：]|"
    # 求 star / 求赞助 / 社区运营话术。
    # 实测踩坑：nanobot 的摘要成了
    # "If nanobot worked for you, a star on GitHub is the simplest way to support the project."
    # 它还因为含 "is the" 拿了 7 分，把真正的描述挤掉了。
    r"if\s+\w+\s+worked\s+for\s+you\b|"
    r"star\s+(on\s+github|us|the\s+repo|this\s+repo)\b|"
    r"if\s+you\s+(like|find|enjoy|use)\s+(it|this|the\s+project)\b|"
    r"(buy\s+me\s+a\s+coffee|sponsor|donat|support\s+(the|this|our)\s+project)\w*|"
    r"we\s+(welcome|appreciate|encourage)\b|"
    r"(issues?|prs?|pull\s+requests?)\s+(are\s+)?(welcome|encouraged)\b|"
    r"(join|follow)\s+(us|our|the)\b|"
    r"(license|licence|mit|apache|gpl)\b[^.]{0,40}\b(licensed|license)\b"
    r")",
    re.IGNORECASE,
)

# 像「这是什么」的正面信号
_LIKE_A_TAGLINE_RE = re.compile(
    r"\b(is|are)\s+(an?|the)\b"          # "X is a tool that..."
    r"|\b(a|an)\s+\w+\s+(tool|library|framework|platform|server|app|application|"
    r"engine|agent|assistant|cli|sdk|runtime|database|protocol|system|suite)\b"
    r"|\b(open[- ]source|lightweight|self[- ]hosted|fast|minimal|simple)\b"
    r"|\b(helps?|lets?|allows?|enables?|provides?|makes?)\s+(you|your|developers?|teams?|users?)\b"
    # 说明「做什么」的动词。实测踩坑：microsoft/mcp 的
    # "This project implements the Model Context Protocol server ..." 只有 2 分，
    # 因为它既没有 "is a" 也没有 "helps you"，但它其实是一句好描述。
    r"|\b(implements?|provides?|offers?|supports?|integrates?|exposes?|wraps?|"
    r"generates?|converts?|extracts?|monitors?|manages?|orchestrates?|automates?|"
    r"visuali[sz]es?|analy[sz]es?|tracks?|renders?|serves?|hosts?|runs?)\b\s+(the|a|an|your|\w+ing)\b",
    re.IGNORECASE,
)


def _snap_to_word(text: str, limit: int) -> str:
    """在词边界截断，避免出现 'intel…' 这种断在单词中间的残句。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


def _split_sentences(text: str) -> list[str]:
    """把长段落拆成句子。

    为什么需要：README 里一段常含多句话。原先把整段当一个候选来评分，
    段落越长长度加分越低（>300 字符直接 0 分），于是长段落里即使藏着
    一句完美的定位描述也会被压分淘汰——这是一种隐性的长度惩罚。
    拆句后每句独立评分，既避免压分，也不用事后截断。
    """
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) >= 25:
            out.append(part)
    return out


def _tagline_score(text: str, project_name: str = "") -> int:
    """给候选句子打分，判断它有多像「这是什么」。

    比「取第一个长句」可靠：README 开头常是安装说明或维护者备忘，
    它们同样又长又完整，但不是定位描述。

    打分刻意保守——拿不准时宁可低分，让调用方退回 GitHub 的
    description 字段（那一句通常更准）。硬凑一句错摘要比留空更糟。
    """
    score = 0

    if _NOT_A_TAGLINE_RE.search(text):
        return -100  # 明确是安装/示例/求关注，直接淘汰

    # 以冒号结尾 = 「下面开始敲命令」的引导语，不是定位描述。
    # 实测踩坑："To create a macOS application bundle:" 被当成摘要。
    if text.rstrip().endswith((":", "：")):
        return -100

    if _LIKE_A_TAGLINE_RE.search(text):
        score += 3

    # 提到项目名本身通常是定位句（"OpenViking is an open-source ..."）
    if project_name:
        short = project_name.split("/")[-1].lower().replace("-", " ")
        if short and short in text.lower().replace("-", " "):
            score += 2

    length = len(text)
    if 60 <= length <= 300:
        score += 2
    elif 30 <= length < 60:
        score += 1

    # 以动词开头的祈使句多半是操作说明（"Use Reef when..." / "Download the..."）
    if re.match(
        r"^\s*(use|download|install|run|build|choose|add|set|create|start|open|clone|"
        r"see|read|check|visit|follow|make|put|copy|point|ensure)\b",
        text,
        re.I,
    ):
        score -= 4

    return score


def _extract_tagline(cleaned: str, fallback: str = "", project_name: str = "") -> str:
    """抽取「这个项目是什么」的一句话。

    **策略：优先用 GitHub 的 description 字段，README 只做兜底。**

    这是抽查 22 个真实样本后改的（原先以 README 为主）。
    description 是作者专门为「这是什么」写的一句话，准确率明显更高：
        ✅ "SpaceXAI's coding agent harness and TUI. Fullscreen, mouse interactive, extensible."
        ✅ "Chrome DevTools for agents ... lets your coding agent control a live Chrome browser."
    而从 README 里抽句子会撞上各种意外：
        ❌ "If nanobot worked for you, a star on GitHub is the simplest way to support the project."
        ❌ "if you are an ai agent helping with this repository, read AGENTS.md ..."
        ❌ "Orca is free and open source under the MIT License."
        ❌ "To create a macOS application bundle:"（以冒号结尾 = 操作引导语）

    README 只在 description 缺失或明显不适用时才用，且必须过较严的门槛。
    """
    clean_fallback = (fallback or "").strip()
    fallback_ok = bool(
        clean_fallback
        and len(clean_fallback) >= 20
        and not _LOW_VALUE_RE.match(clean_fallback)
        and not _NOT_A_TAGLINE_RE.search(clean_fallback)
    )

    if fallback_ok:
        return _snap_to_word(clean_fallback, 320)

    # ---- 兜底：从 README 抽 ----
    prose = strip_code_blocks(cleaned)
    lines = prose.split("\n")
    seen_title = False

    candidates: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        joined = " ".join(pending).strip()
        pending.clear()
        if len(joined) >= 25:
            candidates.append(joined)

    for raw in lines:
        line = raw.strip()

        if not line:
            flush()
            continue

        # 标题判定必须在剥离 markdown 之前做。
        # 踩坑记录：原先先把行内的 * ` 等符号删掉再判断，结果紧贴正文的
        # 下一段标题会被当成普通文本拼进上一段，污染摘要。
        if re.match(r"^#{1,6}\s", line):
            flush()
            seen_title = True
            continue
        if not seen_title:
            continue
        # 噪音行：列表、引用、表格、目录、图占位
        if line.startswith(("|", ">", "-", "*", "+", "[!", "[图]", "==", "```")):
            flush()
            continue
        if line.lower().startswith(("http://", "https://", "<")):
            continue
        if re.match(r"^\s*\[.+\]\(.+\)\s*$", line):  # 纯链接行
            continue

        # 去掉行内 markdown，保留文字
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = re.sub(r"(?<!\w)\*(?!\w)", "", line)  # 只删单独作强调符的 *
        line = re.sub(r"\s+", " ", line).strip()
        if line.startswith("#"):  # 兜底：任何残留的标题记号都不要
            continue
        if len(line) < 12:
            continue

        pending.append(line)

    flush()

    best = ""
    best_score = 0
    # 拆句后逐句评分：避免长段落被长度规则压分（见 _split_sentences）
    scored: list[str] = []
    for cand in candidates[:40]:  # 只看前 40 段，避免在无关内容里挑
        if _LOW_VALUE_RE.match(cand):
            continue
        parts = _split_sentences(cand) or [cand]
        for part in parts:
            if _LOW_VALUE_RE.match(part):
                continue
            scored.append(part)

    for cand in scored:
        s = _tagline_score(cand, project_name)
        if s > best_score:
            best_score = s
            best = cand

    # 门槛设成 4：要求出现明确的定位信号（"X is a tool that..."）。
    # 勉强及格的句子（3 分）经常是求 star、讲协议、贡献指南这类内容，
    # 宁可留空也不要给一句误导性的「是什么」。
    if best and best_score >= 4:
        return _snap_to_word(best, 320)

    return _snap_to_word(clean_fallback or "", 320)


def _section_body(cleaned: str, heading_match: re.Match[str]) -> str:
    """取出某个标题下的正文，直到下一个真标题为止。

    两个踩坑记录：
    1. off-by-one：曾在「已包含当前标题」的子串上从 [1:] 找下一个标题，
       结果匹配到的就是当前标题本身，切出的正文只有 1 个字符。
       必须从标题所在行的**下一行**开始找。
    2. 伪标题：代码块里的注释（如 `# No dependencies -- standard library only`）
       长得像一级标题，会把章节错误地截断。所以必须跳过代码块内部。
    """
    line_end = cleaned.find("\n", heading_match.start())
    if line_end == -1:
        return ""
    body_start = line_end + 1
    spans = _fence_spans(cleaned)

    for m in re.finditer(r"^#{1,4}\s+\S", cleaned[body_start:], re.MULTILINE):
        pos = body_start + m.start()
        if _in_fence(pos, spans):
            continue  # 代码块内的注释不是标题
        return cleaned[body_start:pos]
    return cleaned[body_start:]


def _extract_install_methods(cleaned: str) -> tuple[list[InstallMethod], str]:
    """从代码块里找安装方式，并挑出一条最可信的「最快启动命令」。

    返回 (安装方式列表, 最快启动命令)。

    两条关键规则（都是实测踩坑换来的）：

    1. 只在**安装/快速开始章节内**找命令。原先扫全文，结果
       ai-data-extractor 把「隐私」章节里的 `pip install detect-secrets`
       当成了自己的安装命令——照着敲会装错东西。
       为了凑出一条命令而扫全文，是错误的回退策略。

    2. 命令分「安装」和「运行」两类。有些工具（尤其是脚本类）没有
       install 自己这一步，用法就是 `python xxx.py`。
       不认这类命令就会误判成「没找到安装说明」。
    """
    # 第一条规则：只在安装/快速开始/运行章节内找命令，不扫全文。
    section = _INSTALL_SECTION_RE.search(cleaned)
    region = _section_body(cleaned, section) if section else cleaned

    methods: list[InstallMethod] = []
    seen_kinds: set[str] = set()
    best_command = ""
    best_score = -1

    for order, match in enumerate(_FENCE_RE.finditer(region)):
        block = match.group(2)
        if not _looks_like_shell(block):
            continue
        for raw_line in block.split("\n"):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for kind, pattern in _INSTALL_PATTERNS:
                if not pattern.search(line):
                    continue
                if kind not in seen_kinds:
                    seen_kinds.add(kind)
                    methods.append(InstallMethod(kind=kind, command=line[:160]))

                # 选择「最快启动命令」：位置优先，但真安装类胜过纯运行类，
                # 因为「装好它」比「跑一下例子」更接近用户想要的下一步。
                score = (100 - order) * 2
                if kind in _REAL_INSTALL_KINDS:
                    score += 50
                if section:
                    score += 20  # 来自安装章节，可信度更高
                if score > best_score:
                    best_score = score
                    best_command = line[:160]
                break

    # 排序：越省事的越靠前
    priority = {
        "docker": 0,
        "script": 1,
        "brew": 2,
        "winget": 3,
        "npm": 4,
        "pip": 5,
        "cargo": 6,
        "go": 7,
        "uvx": 8,
        "binary": 9,
        "source": 10,
        "make": 11,
        "python": 12,
    }
    methods.sort(key=lambda m: priority.get(m.kind, 99))
    return methods, best_command


def analyze(repo: Repo, readme: str | None, *, fetch_extra: dict[str, Any] | None = None) -> ReadmeAnalysis:
    """分析一个仓库的 README，产出可读判断。"""
    extra = fetch_extra or {}
    # 时间类信号可能还没被 discover() 算过，这里按需补算。
    repo.ensure_signals()

    if not readme:
        a = ReadmeAnalysis(has_readme=False)
        a.recommended_action = "无 README，信息不足——除非已在别处见过，否则跳过"
        a.friction_score = 5
        return a

    cleaned = clean_markdown(readme)
    a = ReadmeAnalysis(has_readme=True, cleaned_len=len(cleaned))

    prose = strip_code_blocks(cleaned)
    proselow = prose.lower()

    # 标题抽取要跳过代码块内部：README 里的 shell 注释（`# 安装依赖`）
    # 长得很像标题，混进来会让结构判断失真。
    spans = _fence_spans(cleaned)
    a.headings = [
        m.group(2).strip()[:80]
        for m in _HEADING_RE.finditer(cleaned)
        if not _in_fence(m.start(), spans)
    ][:25]
    a.tagline = _extract_tagline(cleaned, repo.description, repo.full_name)
    a.code_blocks = len(_FENCE_RE.findall(cleaned))
    a.install_methods, a.best_command = _extract_install_methods(cleaned)

    # 结构信号
    a.has_architecture = any(k in proselow for k in _ARCH_KEYWORDS)
    a.has_comparison = any(k in proselow for k in _COMPARE_KEYWORDS)
    a.has_benchmark = any(k in proselow for k in _BENCH_KEYWORDS)
    a.has_screenshot = "[图]" in cleaned
    a.has_usage_example = bool(
        re.search(r"^#{1,4}\s*.*(usage|example|quick\s*start|快速开始|示例|用法).*$", prose, re.I | re.M)
    )

    # 摩擦信号。
    # 实测踩坑：GPU / API Key 原先扫全文，误报严重——README 里一句
    # 「也支持 NVIDIA 显卡」就会让一个纯 CPU 工具被标上「需GPU」。
    # 这类要求通常写在安装/运行章节附近，所以限定在一个窄窗口内侦测，
    # 宁可漏报也不要误报：误导性的「需要 GPU」会让人直接跳过好项目。
    section = _INSTALL_SECTION_RE.search(cleaned)
    req_zone = cleaned[section.start() : section.start() + 1500] if section else cleaned[:2000]
    req_low = req_zone.lower()

    a.has_api_key_need = bool(
        re.search(
            r"(api[_\s-]?key|access[_\s-]?token|openai_api|secret[_\s-]?key|"
            r"export\s+\w+_key|\.env\b)",
            req_low,
        )
    )
    a.has_gpu_need = bool(
        re.search(
            r"(requires?\s+(an?\s+)?(nvidia|cuda|gpu)|"
            r"cuda\s*(runtime|toolkit|version|>=)|"
            r"nvidia\s+(driver|gpu|container)|"
            r"\d+\s*gb\s+(of\s+)?(vram|gpu)|"
            r"gpu\s+(is\s+)?required|"
            r"install\s+cuda)",
            req_low,
        )
    )
    a.has_docker = any(m.kind == "docker" for m in a.install_methods)

    # 环境要求只在前部扫描：README 后段常在「对比」「贡献指南」里
    # 提到别的语言的版本号，扫全文会误判成本项目的依赖。
    head = readme[:3000]
    for label, pattern in _LANG_REQ_PATTERNS:
        if pattern.search(head):
            a.has_lang_requirement.append(label)

    a.quality_signals = _quality_signals(repo, a, extra)
    a.friction_score, a.setup_minutes = _friction(repo, a, extra)
    a.recommended_action = _recommend(repo, a, extra)
    return a


# --------------------------------------------------------------- 判断逻辑


def _quality_signals(repo: Repo, a: ReadmeAnalysis, extra: dict[str, Any]) -> list[str]:
    """收集「这个项目靠谱」的证据。只列事实，不吹。"""
    signals: list[str] = []

    if repo.license and repo.license not in ("NOASSERTION", "NONE"):
        signals.append(f"有明确开源协议（{repo.license}）")
    else:
        signals.append("⚠ 无明确协议，商用需谨慎")

    if a.install_methods:
        kinds = "、".join(dict.fromkeys(m.kind for m in a.install_methods))
        signals.append(f"提供 {len(a.install_methods)} 种安装方式（{kinds}）")

    if a.has_architecture:
        signals.append("README 讲了架构/原理")
    if a.has_benchmark:
        signals.append("有性能数据")
    if a.has_comparison:
        signals.append("有竞品对比")
    if a.has_screenshot:
        signals.append("有截图/演示图")

    stats = extra.get("contributors")
    if isinstance(stats, int) and stats >= 10:
        signals.append(f"{stats}+ 位贡献者")

    rel = extra.get("releases_30d")
    if isinstance(rel, int):
        if rel >= 2:
            signals.append(f"近 30 天发了 {rel} 个版本")
        elif rel == 1:
            signals.append("近 30 天发了 1 个版本")
        elif repo.age_days > 60:
            # 只有老项目「无新版本」才是值得注意的信号。
            # 新项目本来就还没发版，列出来等于噪音（实测踩坑）。
            signals.append("近 30 天无新版本")
        elif repo.idle_days <= 3:
            signals.append("早期项目，主干近期有提交")

    return signals


def _friction(repo: Repo, a: ReadmeAnalysis, extra: dict[str, Any]) -> tuple[int, int]:
    """估算上手成本。返回 (摩擦分 0-10, 预估首次跑通分钟数)。"""
    score = 0
    minutes = 3
    kinds = {m.kind for m in a.install_methods}

    if a.has_docker:
        score += 0
        minutes = 5
    elif kinds & {"script", "brew", "winget"}:
        score += 1
        minutes = 5
    elif kinds & {"npm", "pip", "cargo", "go", "uvx"}:
        score += 2
        minutes = 10
    elif "python" in kinds:
        # 纯脚本类工具（`python xxx.py` 就能跑）。
        # 实测踩坑：这类项目没有 install 自己这一步，曾落到「没找到安装说明」
        # 的兜底分支被判成 30 分钟，但它们其实比 git clone+make 轻得多。
        score += 3
        minutes = 15
    elif "source" in kinds:
        score += 5
        minutes = 40
    elif "make" in kinds:
        score += 6
        minutes = 60
    elif "binary" in kinds:
        score += 3
        minutes = 20
    else:
        score += 4
        minutes = 30  # 真的没找到任何可执行说明

    if a.has_api_key_need:
        score += 2
        minutes += 10
    if a.has_gpu_need:
        score += 4
        minutes += 60
    if repo.language in ("C++", "C", "Rust", "CUDA"):
        score += 1
        minutes += 15

    # README 质量也影响实际上手难度
    if a.cleaned_len < 500:
        score += 2
        minutes += 15
    elif a.cleaned_len > 40000:
        score += 1
        minutes += 10

    return min(score, 10), minutes


def _recommend(repo: Repo, a: ReadmeAnalysis, extra: dict[str, Any]) -> str:
    """给出一句话行动建议。这是日报里最该读的一行。"""
    tags: list[str] = []

    if repo.age_days <= 21 and repo.velocity > 8:
        tags.append("新增速度很快，属于早期高关注项目")
    elif repo.age_days <= 21:
        tags.append("新项目")

    if repo.idle_days > 45:
        tags.append(f"⚠ 已 {int(repo.idle_days)} 天无推送，疑似停滞")
    elif repo.idle_days > 14:
        tags.append(f"⚠ 已 {int(repo.idle_days)} 天无推送，节奏放缓")
    elif repo.idle_days <= 2:
        tags.append("近日仍在活跃提交")

    if a.friction_score <= 2:
        tags.append(f"上手极轻（约 {a.setup_minutes} 分钟可跑通）")
    elif a.friction_score <= 5:
        tags.append(f"上手成本中等（约 {a.setup_minutes} 分钟）")
    else:
        tags.append(f"上手较重（预估 {a.setup_minutes} 分钟以上）")

    if not a.has_readme or a.cleaned_len < 400:
        tags.append("文档偏薄，需读源码判断")
    if a.has_gpu_need:
        tags.append("需要 GPU 资源")

    return "；".join(tags)
