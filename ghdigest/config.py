"""配置：关注领域的关键词、筛选阈值、路径。

这里的领域定义是「发现引擎」的入口。改这个文件就能换关注方向，
不需要动代码逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
STATE_PATH = ROOT / ".cache" / "state.json"
DIGEST_DIR = ROOT / "digests"
CONFIG_PATH = ROOT / "config.json"


# ---------------------------------------------------------------- 领域关键词
# 每个领域是一组搜索词。发现引擎会把它们和动量过滤器组合成 GitHub 搜索查询。
# 关键词用 OR 语义（GitHub 搜索里同一个 query 内多个词默认是 AND，所以这里
# 拆成独立查询再合并去重，覆盖面比单条长查询更广）。

DEFAULT_DOMAINS: dict[str, list[str]] = {
    "AI / LLM / Agent": [
        "llm",
        "llm agent",
        "ai agent framework",
        "rag retrieval augmented",
        "llm inference",
        "mcp model context protocol",
        "ai coding assistant",
        "llm fine-tuning",
        "embedding vector search",
        "multimodal model",
    ],
    "通用工具 / CLI / 效率": [
        "cli tool",
        "developer productivity",
        "terminal tool",
        "tui",
        "self-hosted",
        "automation tool",
        "devtool",
        "command line utility",
    ],
    "跨领域动量股": [
        "framework",
        "runtime",
        "database",
        "compiler",
        "observability",
        "protocol",
    ],
}


@dataclass
class Settings:
    """一次运行的全部可调参数。"""

    # --- 发现阶段 ---
    domains: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_DOMAINS.items()}
    )
    # 只看最近多少天内创建的新项目（捕捉「新东西」）
    fresh_days: int = 21
    # 只看最近多少天内有推送的项目（捕捉「还活着的东西」）
    active_days: int = 7
    # 新项目的星数门槛（低门槛，因为新项目星少但可能重要）
    min_stars_fresh: int = 150
    # 老项目的星数门槛（高门槛，避免常青巨库刷屏）
    min_stars_established: int = 2000
    # 超过这个星数的「常青巨型库」直接降权——它们几乎每天都推送，
    # 但不是新闻。这是实测踩到的坑：按星数排序全是 freeCodeCamp 这类。
    evergreen_star_threshold: int = 60000
    # 每个领域最多取多少个候选
    per_domain_limit: int = 30

    # --- 排序阶段 ---
    # 星数动量权重：新项目看「星数/年龄」，老项目看「近期是否真在推进」
    top_picks: int = 5
    feed_picks: int = 20

    # --- 过滤开关 ---
    exclude_awesome_lists: bool = True
    exclude_forks: bool = True
    exclude_archived: bool = True
    # 排除明显不是「工具/库」的仓库（教程、面试题、资源集合）
    blocklist_topics: tuple[str, ...] = (
        "awesome",
        "awesome-list",
        "list",
        "resource",
        "resources",
        "collection",
        "tutorial",
        "tutorials",
        "course",
        "courses",
        "book",
        "books",
        "interview",
        "interview-questions",
        "roadmap",
        "cheatsheet",
        "cheat-sheet",
        "examples",
        "learning",
        "guide",
        "free",
        "public-apis",
    )

    # --- 运行控制 ---
    cache_ttl_minutes: int = 180
    offline: bool = False
    verbose: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """从 config.json 读取覆盖项；文件不存在就用默认值。"""
        path = path or CONFIG_PATH
        s = cls()
        if not path.exists():
            return s
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] config.json 读取失败，使用默认配置：{exc}")
            return s

        for key, value in raw.items():
            # 下划线开头的键视为注释（JSON 不支持注释，用这种方式自文档）
            if key.startswith("_"):
                continue
            if not hasattr(s, key):
                print(f"[warn] config.json 里有未知配置项，已忽略：{key}")
                continue
            current = getattr(s, key)
            # JSON 里写 list，但内部用 tuple 的字段做一次转换
            if isinstance(current, tuple) and isinstance(value, list):
                value = tuple(value)
            setattr(s, key, value)
        return s


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
