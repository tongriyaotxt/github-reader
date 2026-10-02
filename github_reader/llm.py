"""LLM 调用（用于把摘要与建议翻成中文）。

设计要点：

1. **复用已有凭据**。这个 harness 已在 ~/.dsh/.credentials.yaml 里存了
   DEEPSEEK_API_KEY，所以用户不需要配任何东西。也支持环境变量覆盖。

2. **只用标准库**。整个项目零依赖，这一层也不例外——直接 POST 到
   OpenAI 兼容的 /chat/completions。

3. **失败必须显式**。翻译是可选增强，不能因为它失败就让整份日报生成不出来。
   所以错误向上抛成 LLMError，由调用方决定降级策略。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: int = 120
    _source: str = field(default="", repr=False)

    @property
    def source(self) -> str:
        """凭据来源，用于告诉用户「我用了哪个 key」。"""
        return self._source

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def _read_dsh_key() -> str:
    """从 ~/.dsh/.credentials.yaml 里取 DEEPSEEK_API_KEY。

    只做极简解析，不引入 yaml 依赖——这个文件的格式是固定的
    「KEY: value」逐行列出，够用且不会因为格式微调就崩。
    """
    if not DSH_CREDENTIALS.exists():
        return ""
    try:
        text = DSH_CREDENTIALS.read_text(encoding="utf-8")
    except OSError:
        return ""

    for line in text.splitlines():
        stripped = line.strip()
        # 顶层键，形如 "DEEPSEEK_API_KEY: sk-xxx"
        if stripped.startswith("DEEPSEEK_API_KEY"):
            _, _, value = stripped.partition(":")
            value = value.strip().strip("'\"")
            if value:
                return value
    return ""


def resolve_config(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMConfig:
    """按优先级找配置：显式参数 → 环境变量 → DSH 凭据文件。"""
    cfg = LLMConfig()

    if api_key:
        cfg.api_key, cfg._source = api_key, "命令行参数"
    else:
        env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if env_key:
            cfg.api_key, cfg._source = env_key.strip(), "环境变量"
        else:
            dsh_key = _read_dsh_key()
            if dsh_key:
                cfg.api_key, cfg._source = dsh_key, "~/.dsh 凭据"

    cfg.base_url = (
        base_url
        or os.environ.get("DEEPSEEK_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    cfg.model = model or os.environ.get("GITHUB_READER_LLM_MODEL") or DEFAULT_MODEL
    return cfg


def chat_json(
    cfg: LLMConfig,
    system: str,
    user: str,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    retries: int = 3,
) -> dict:
    """调用 chat/completions，要求返回 JSON。

    用 response_format=json_object 让服务端保证 JSON 结构，
    否则模型很容易在 JSON 前后加解释性文字，解析就得靠正则猜——
    猜错的代价是静默丢失翻译。
    """
    if not cfg.api_key:
        raise LLMError("没有可用的 API key")

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{cfg.base_url}/chat/completions"

    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
                "User-Agent": "github-reader/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            content = (
                data.get("choices", [{}])[0].get("message", {}).get("content") or ""
            )
            if not content.strip():
                raise LLMError("模型返回了空内容")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise LLMError(f"模型返回的 JSON 不是对象：{type(parsed).__name__}")
            return parsed

        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            last_error = LLMError(f"HTTP {exc.code}：{detail}")
            # 429/5xx 值得重试；401/400 重试没意义
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                import time

                time.sleep(2.0 * (attempt + 1))
                continue
            raise last_error from exc

        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = LLMError(f"网络错误：{exc}")
            if attempt < retries - 1:
                import time

                time.sleep(2.0 * (attempt + 1))
                continue
            raise last_error from exc

        except json.JSONDecodeError as exc:
            last_error = LLMError(f"返回内容不是合法 JSON：{exc}")
            if attempt < retries - 1:
                import time

                time.sleep(1.0 * (attempt + 1))
                continue
            raise last_error from exc

    raise last_error or LLMError("调用失败")
