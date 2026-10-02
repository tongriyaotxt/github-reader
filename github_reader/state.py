"""跨运行状态：让「日报」变成「持续跟进」。

解决的问题：如果每天看到的都是同一批仓库，日报就退化成噪音。
所以需要记住「上次见过什么、当时多少星、连续出现几天」。

有了这份历史，日报才能说出这类话：
  「★ 首次出现」「连续 3 天在榜，+420 星」「上周见过，星数停滞」
——这正是让你不必自己回忆的关键信息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import STATE_PATH


@dataclass
class RepoHistory:
    """单个仓库的跟踪历史。"""

    full_name: str
    first_seen: str = ""  # ISO 日期
    last_seen: str = ""
    seen_count: int = 0
    consecutive_days: int = 0
    stars_first: int = 0
    stars_last: int = 0
    starred_by_user: bool = False  # 预留：手动标记「已细读」
    note: str = ""

    @property
    def star_delta(self) -> int:
        return self.stars_last - self.stars_first

    @property
    def is_new(self) -> bool:
        return self.seen_count <= 1


class State:
    """状态容器，持久化到 .cache/state.json。"""

    def __init__(self, path: Path | None = None):
        self.path = path or STATE_PATH
        self.history: dict[str, RepoHistory] = {}
        self.last_run: str = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 状态文件损坏，将重建：{exc}")
            return

        self.last_run = blob.get("last_run", "")
        for name, item in (blob.get("history") or {}).items():
            if not isinstance(item, dict):
                continue
            self.history[name] = RepoHistory(
                full_name=name,
                first_seen=item.get("first_seen", ""),
                last_seen=item.get("last_seen", ""),
                seen_count=item.get("seen_count", 0),
                consecutive_days=item.get("consecutive_days", 0),
                stars_first=item.get("stars_first", 0),
                stars_last=item.get("stars_last", 0),
                starred_by_user=item.get("starred_by_user", False),
                note=item.get("note", ""),
            )

    def save(self, today: str | None = None) -> None:
        today = today or date.today().isoformat()
        self.last_run = today
        blob = {
            "last_run": self.last_run,
            "history": {
                name: {
                    "first_seen": h.first_seen,
                    "last_seen": h.last_seen,
                    "seen_count": h.seen_count,
                    "consecutive_days": h.consecutive_days,
                    "stars_first": h.stars_first,
                    "stars_last": h.stars_last,
                    "starred_by_user": h.starred_by_user,
                    "note": h.note,
                }
                for name, h in sorted(self.history.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[warn] 状态保存失败：{exc}")

    def observe(
        self, full_name: str, stars: int, today: str | None = None
    ) -> tuple[RepoHistory, bool]:
        """记录一次「今天看到这个仓库」。

        返回 (历史, 是否是跨天的新观察)。

        第二个返回值很重要：同一天重复运行不该被算成「第 N 次出现」，
        也不该用当天两次运行之间的星数波动当成「增长」——那是噪音。
        所以调用方必须用这个标志来决定要不要展示增量。
        """
        today = today or date.today().isoformat()
        h = self.history.get(full_name)

        if h is None:
            h = RepoHistory(
                full_name=full_name,
                first_seen=today,
                last_seen=today,
                seen_count=1,
                consecutive_days=1,
                stars_first=stars,
                stars_last=stars,
            )
            self.history[full_name] = h
            return h, True

        if h.last_seen == today:
            # 同一天重复跑：只把星数刷新到最新，不计数、不算增量
            h.stars_last = stars
            return h, False

        h.seen_count += 1
        h.stars_last = stars

        # 判断是否与上次相邻
        try:
            prev = datetime.fromisoformat(h.last_seen).date()
            cur = datetime.fromisoformat(today).date()
            gap = (cur - prev).days
        except ValueError:
            gap = 99

        h.consecutive_days = h.consecutive_days + 1 if gap == 1 else 1
        h.last_seen = today
        return h, True

    def trend_line(self, h: RepoHistory, stars_now: int) -> str:
        """给日报用的一句话趋势描述。"""
        bits: list[str] = []

        if h.seen_count <= 1:
            bits.append("★ 首次出现")
        else:
            bits.append(f"第 {h.seen_count} 次出现")

        if h.consecutive_days >= 2:
            bits.append(f"连续 {h.consecutive_days} 天在榜")

        delta = stars_now - h.stars_first
        if delta > 0 and h.seen_count > 1:
            bits.append(f"累计 +{delta} ★")
        elif delta == 0 and h.seen_count >= 3:
            bits.append("星数停滞")

        return "；".join(bits)

    def prune(self, keep_days: int = 180, today: str | None = None) -> int:
        """清理长期未出现的记录，避免状态文件无限膨胀。"""
        today_date = datetime.fromisoformat(today or date.today().isoformat()).date()
        removed = 0
        for name in list(self.history):
            h = self.history[name]
            try:
                last = datetime.fromisoformat(h.last_seen).date()
            except ValueError:
                continue
            if (today_date - last).days > keep_days:
                del self.history[name]
                removed += 1
        return removed
