"""GitHub API 客户端。

三个关键设计：
1. 认证复用 `gh auth token`（你已登录 GitHub CLI，无需再配密钥）。
   未认证限额是 60 次/小时，认证后是 5000 次/小时——这个差别决定方案可行性。
2. 磁盘缓存。同一天跑多次不会重复烧配额。
3. 搜索接口 30 次/分钟，所以搜索走串行 + 主动间隔；README 抓取走并发。
   搞混这两者会撞限流。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import CACHE_DIR

API = "https://api.github.com"
UA = "ghdigest/0.1 (+local personal digest tool)"


class GitHubError(RuntimeError):
    """API 层错误，带上足够上下文便于排查。"""

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


def resolve_token() -> str | None:
    """按优先级找 token：环境变量 → gh CLI。"""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(var)
        if token:
            return token.strip()

    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        token = proc.stdout.strip()
        if token:
            return token
    return None


@dataclass
class RateLimit:
    remaining: int = 0
    limit: int = 0
    reset_at: int = 0

    @property
    def is_low(self) -> bool:
        return self.remaining <= 3


class GitHubClient:
    """带缓存和退避的极简 GitHub REST 客户端。"""

    def __init__(
        self,
        token: str | None = None,
        cache_ttl_minutes: int = 180,
        offline: bool = False,
        verbose: bool = False,
    ):
        self.token = token if token is not None else resolve_token()
        self.authenticated = bool(self.token)
        # 搜索结果的时效性要求高，README 变化慢。分开设 TTL，
        # 否则统一的 3 小时缓存会把「日报」变成「三日报」。
        self.cache_ttl = cache_ttl_minutes * 60
        self.readme_ttl = max(self.cache_ttl, 6 * 3600)
        self.offline = offline
        self.verbose = verbose
        self._mem: dict[str, Any] = {}
        self.rate = RateLimit()
        self._last_search_at = 0.0
        self._search_min_interval = 2.2  # 30/min → 保守取 ~27/min

    # ------------------------------------------------------------- 底层请求
    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return CACHE_DIR / "http" / f"{digest}.json"

    def _read_cache(self, key: str, ttl: float) -> Any | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - blob.get("_cached_at", 0) > ttl:
            return None
        data = blob.get("data")
        self._mem[key] = data
        return data

    def _write_cache(self, key: str, data: Any) -> None:
        self._mem[key] = data
        path = self._cache_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"_cached_at": time.time(), "data": data}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # 缓存写不进去不该让整个任务失败

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        retries: int = 3,
        use_cache: bool = True,
        ttl: float | None = None,
    ) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        cache_key = f"{accept}|{url}"
        if use_cache:
            if cache_key in self._mem:
                if self.verbose:
                    print(f"    [mem] {url}")
                return self._mem[cache_key]
            hit = self._read_cache(cache_key, ttl if ttl is not None else self.cache_ttl)
            if hit is not None:
                if self.verbose:
                    print(f"    [cache] {url}")
                return hit
            if self.offline:
                raise GitHubError(f"离线模式且无缓存: {url}", url=url)

        headers = {"User-Agent": UA, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last_error: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._update_rate(resp.headers)
                    raw = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(raw) if raw.strip() else None
                    if use_cache:
                        self._write_cache(cache_key, data)
                    return data

            except urllib.error.HTTPError as exc:
                self._update_rate(exc.headers)
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                last_error = GitHubError(
                    f"HTTP {exc.code} {url} :: {body}", status=exc.code, url=url
                )
                # 403/429：限流。按 Retry-After 或退避等待。
                if exc.code in (403, 429):
                    wait = self._retry_after(exc.headers, attempt)
                    if attempt < retries - 1:
                        if self.verbose:
                            print(f"    [ratelimit] {exc.code}，等待 {wait:.0f}s 后重试")
                        time.sleep(wait)
                        continue
                # 404/451：资源不存在或不可用，重试没意义
                if exc.code in (404, 451):
                    raise last_error from exc
                if exc.code >= 500 and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc

            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = GitHubError(f"网络错误 {url} :: {exc}", url=url)
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc

        raise last_error or GitHubError(f"请求失败: {url}", url=url)

    def _retry_after(self, headers: Any, attempt: int) -> float:
        try:
            ra = headers.get("Retry-After")
            if ra:
                return min(float(ra), 60.0)
        except (TypeError, ValueError):
            pass
        try:
            reset = int(headers.get("X-RateLimit-Reset", "0"))
            if reset:
                delta = reset - time.time()
                if 0 < delta <= 90:
                    return delta + 1
        except (TypeError, ValueError):
            pass
        return min(5.0 * (2**attempt), 45.0)

    def _update_rate(self, headers: Any) -> None:
        try:
            self.rate = RateLimit(
                remaining=int(headers.get("X-RateLimit-Remaining", 0)),
                limit=int(headers.get("X-RateLimit-Limit", 0)),
                reset_at=int(headers.get("X-RateLimit-Reset", 0)),
            )
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------- 公开接口
    def search_repos(self, query: str, *, sort: str = "stars", per_page: int = 30) -> list[dict]:
        """搜索仓库。搜索接口限流更严（30/min），所以主动间隔。"""
        elapsed = time.time() - self._last_search_at
        if elapsed < self._search_min_interval:
            time.sleep(self._search_min_interval - elapsed)
        self._last_search_at = time.time()

        data = self._request(
            f"{API}/search/repositories",
            params={"q": query, "sort": sort, "order": "desc", "per_page": per_page},
        )
        items = (data or {}).get("items", [])
        return items if isinstance(items, list) else []

    def get_readme(self, full_name: str, max_bytes: int = 40000) -> str | None:
        """取 README 纯文本。返回 None 表示没有 README。"""
        try:
            data = self._request(f"{API}/repos/{full_name}/readme", ttl=self.readme_ttl)
        except GitHubError as exc:
            if exc.status in (404, 451):
                return None
            if self.verbose:
                print(f"    [readme-fail] {full_name}: {exc}")
            return None

        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not content:
            return None
        try:
            decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
        # 统一换行符：真实 README 多为 CRLF，残留的 \r 会让下游所有
        # 依赖 $ 锚点的多行正则静默失配。在源头规范化一次，比让每个
        # 消费方各自防御可靠。
        decoded = decoded.replace("\r\n", "\n").replace("\r", "\n")
        return decoded[:max_bytes]

    def get_json(self, path: str) -> Any:
        """任意 GET，path 形如 '/repos/owner/name/releases'。"""
        return self._request(f"{API}{path}")

    def probe_rate_limit(self) -> RateLimit:
        """主动查一次配额，让启动时就能显示真实数字而不是 '?'。"""
        try:
            data = self._request(f"{API}/rate_limit", use_cache=False)
            core = (data or {}).get("resources", {}).get("core", {})
            self.rate = RateLimit(
                limit=int(core.get("limit", 0) or 0),
                remaining=int(core.get("remaining", 0) or 0),
                reset_at=int(core.get("reset", 0) or 0),
            )
        except (GitHubError, TypeError, ValueError):
            pass
        return self.rate

    def prefetch_readmes(
        self,
        full_names: Iterable[str],
        *,
        workers: int = 8,
        on_done: Callable[[str], None] | None = None,
    ) -> dict[str, str | None]:
        """并发抓取多个 README。

        并发是安全的：README 走的是 core 限额（5000/h），不是搜索限额。
        """
        names = list(dict.fromkeys(full_names))
        results: dict[str, str | None] = {}
        if not names:
            return results

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.get_readme, n): n for n in names}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception:
                    results[name] = None
                if on_done:
                    on_done(name)
        return results
