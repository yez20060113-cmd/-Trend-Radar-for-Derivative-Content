"""OpenAI 兼容的模型客户端：缓存、重试、审计、失败降级。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ROOT, env

log = logging.getLogger("radar.llm")

# prompt 有实质改动就改这个，旧缓存自然失效
PROMPT_VERSION = "v1"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass
class LLMResult:
    """一次调用的结果。`ok=False` 时调用方必须走降级分支，不要抛异常。"""

    ok: bool = False
    data: Any = None
    error: str = ""
    raw: str = ""
    model: str = ""
    cached: bool = False
    elapsed_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON。

    三种常见脏输出都要能处理：```json 围栏、前后带解释文字、直接给对象。
    抠不出来返回 None —— 由调用方决定降级，绝不猜。
    """
    if not text:
        return None
    body = text.strip()
    m = _FENCE_RE.search(body)
    if m:
        body = m.group(1).strip()
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        pass
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(body[start : end + 1])
        except Exception:  # noqa: BLE001
            return None
    return None


class LLM:
    """一层薄封装，刻意不做成框架。

    用法::

        llm = LLM(cfg, run_dir=run_dir)
        if not llm.available:
            ...           # 没配 key，直接跳过
        res = llm.json_call(layer="material", system=..., user=...)
        if res.ok:
            ...
    """

    def __init__(
        self, cfg: dict[str, Any] | None = None, *, run_dir: Path | None = None
    ) -> None:
        cfg = cfg or {}
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", False))
        self.base_url = str(cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.model = str(cfg.get("model") or "deepseek-flash")
        self.key_env = str(cfg.get("api_key_env") or "RADAR_LLM_KEY")
        self.temperature = float(cfg.get("temperature", 0))
        self.timeout = float(cfg.get("timeout_seconds", 60))
        self.max_retries = int(cfg.get("max_retries", 2))
        self.max_tokens = int(cfg.get("max_output_tokens", 4000))
        self.json_mode = bool(cfg.get("json_mode", True))
        # 透传给请求体的额外字段（如关掉 thinking）。不确定参数名就不要填，
        # 这里只做透传，不猜任何供应商的私有字段。
        self.extra_body: dict[str, Any] = dict(cfg.get("extra_body") or {})

        cache_path = str(cfg.get("cache") or "data/state/llm_cache.json")
        self.cache_path = Path(cache_path) if Path(cache_path).is_absolute() else ROOT / cache_path
        self.cache_ttl_s = float(cfg.get("cache_ttl_days", 3)) * 86400
        self.run_dir = run_dir

        self._cache: dict[str, dict[str, Any]] = self._load_cache()
        self._seq = 0
        self.stats: dict[str, Any] = {
            "calls": 0, "ok": 0, "fail": 0, "cached": 0,
            "in_tokens": 0, "out_tokens": 0,
        }

    # ---------------------------------------------------------------- 可用性

    @property
    def key(self) -> str:
        return env(self.key_env)

    @property
    def available(self) -> bool:
        """enabled 且真的拿到了 key。少一个都算不可用，调用方要能识别并跳过。"""
        return self.enabled and bool(self.key)

    def why_unavailable(self) -> str:
        if not self.enabled:
            return "llm.enabled = false"
        if not self.key:
            return f"缺少环境变量 {self.key_env}（写在项目根目录 .env 里）"
        return ""

    # ---------------------------------------------------------------- 缓存

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("模型缓存读取失败，按空处理：%s", exc)
            return {}

    def save_cache(self) -> None:
        if not self._cache:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近 500 条，避免文件无限膨胀
        if len(self._cache) > 500:
            items = sorted(self._cache.items(), key=lambda kv: kv[1].get("ts", 0))[-500:]
            self._cache = dict(items)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _cache_key(self, layer: str, system: str, user: str) -> str:
        raw = f"{PROMPT_VERSION}|{self.model}|{self.temperature}|{layer}|{system}|{user}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---------------------------------------------------------------- 审计

    def _audit(self, layer: str, payload: dict[str, Any]) -> None:
        if not self.run_dir:
            return
        out = self.run_dir / "llm_calls"
        out.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        path = out / f"{self._seq:03d}-{layer}.json"
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("审计落盘失败（不影响主流程）：%s", exc)

    # ---------------------------------------------------------------- 调用

    def _post(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("响应里没有 choices")
        message = choices[0].get("message") or {}
        # 只取 content。reasoning_content 是思考过程，不是答案。
        text = str(message.get("content") or "")
        return text, dict(payload.get("usage") or {})

    def json_call(
        self,
        *,
        layer: str,
        system: str,
        user: str,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> LLMResult:
        """要一个 JSON 对象回来。任何失败都返回 ok=False，不抛。"""
        if not self.available:
            return LLMResult(ok=False, error=self.why_unavailable(), model=self.model)

        key = self._cache_key(layer, system, user)
        if use_cache:
            hit = self._cache.get(key)
            if hit and (time.time() - float(hit.get("ts", 0))) < self.cache_ttl_s:
                self.stats["cached"] += 1
                self.stats["calls"] += 1
                self.stats["ok"] += 1
                return LLMResult(
                    ok=True, data=hit.get("data"), model=self.model, cached=True
                )

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)

        t0 = time.time()
        last_err = ""
        # 第一轮带 response_format；若供应商不认，退化到不带的版本再试
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                text, usage = self._post(body)
                data = extract_json(text)
                elapsed = int((time.time() - t0) * 1000)
                self.stats["calls"] += 1
                self.stats["in_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.stats["out_tokens"] += int(usage.get("completion_tokens") or 0)
                self._audit(
                    layer,
                    {
                        "layer": layer,
                        "model": self.model,
                        "prompt_version": PROMPT_VERSION,
                        "attempt": attempt + 1,
                        "elapsed_ms": elapsed,
                        "usage": usage,
                        "request_chars": {"system": len(system), "user": len(user)},
                        "raw": text,
                        "parsed": data,
                    },
                )
                if data is None:
                    last_err = "输出不是合法 JSON"
                    # 重试一次：首轮回放 24 组里有 1 组是这个错，重跑后同一组成功了。
                    # 只重试一次 —— 同样的输入连续给坏格式，说明是模型侧问题，再试也是浪费。
                    if attempt == 0:
                        continue
                    break
                self.stats["ok"] += 1
                if use_cache:
                    self._cache[key] = {"ts": time.time(), "data": data}
                return LLMResult(
                    ok=True, data=data, raw=text, model=self.model,
                    elapsed_ms=elapsed, usage=usage,
                )
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last_err = f"HTTP {exc.code}: {detail}"
                # 400 且是 response_format 引起的，去掉再试一次
                if exc.code == 400 and "response_format" in body:
                    body.pop("response_format", None)
                    continue
                if exc.code in (401, 403):
                    break          # key 不对，重试无意义
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                time.sleep(min(8, 1.5 * (attempt + 1) ** 2))

        self.stats["fail"] += 1
        log.warning("模型调用失败（%s）：%s", layer, last_err[:160])
        self._audit(
            layer,
            {"layer": layer, "model": self.model, "error": last_err,
             "request_chars": {"system": len(system), "user": len(user)}},
        )
        return LLMResult(ok=False, error=last_err, model=self.model)


def from_config(
    cfg: dict[str, Any] | None, *, run_dir: Path | None = None
) -> LLM:
    """按 `sources.yaml` 的 `llm` 段建客户端。缺配置时返回不可用实例。"""
    return LLM(cfg or {}, run_dir=run_dir)


def probe(cfg: dict[str, Any] | None, *, run_dir: Path | None = None) -> LLM:
    """诊断 / 回放用的客户端：**故意不看** `enabled` 总开关。

    `enabled` 管的是「每天 09:00 的正式跑批要不要花钱」，不是「key 能不能用」。
    自检、离线回放、查询生成预览都是人主动敲命令，本来就该能跑。
    """
    merged = dict(cfg or {})
    merged["enabled"] = True
    return LLM(merged, run_dir=run_dir)


def available(cfg: dict[str, Any] | None) -> bool:
    return from_config(cfg).available


def check(cfg: dict[str, Any]) -> int:
    """连通性自检：`python -m src.llm.client`。

    刻意**不看** `enabled`：这是诊断命令，用来回答「key 通不通」。
    总开关关着也该能测 —— 否则配好 key 却因为开关是 false 而看不出到底有没有问题。
    """
    probe_llm = probe(cfg)
    if not probe_llm.available:
        print(f"❌ 不可用：{probe_llm.why_unavailable()}")
        return 2
    llm = probe_llm
    print(f"model={llm.model}  base_url={llm.base_url}")
    res = llm.json_call(
        layer="check",
        system="你是一个连通性探测服务。只输出 JSON。",
        user='返回 {"ok": true, "echo": "pong"} 这个 JSON，不要多余文字。',
        max_tokens=64,
        use_cache=False,
    )
    if res.ok:
        print(f"✅ 连通（{res.elapsed_ms} ms，token={res.usage}）：{res.data}")
        return 0
    print(f"❌ 调用失败：{res.error}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.path.insert(0, str(ROOT))
    from ..config import load_sources

    raise SystemExit(check(load_sources().get("llm", {})))
