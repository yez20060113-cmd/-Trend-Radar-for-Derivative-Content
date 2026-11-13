"""统一 HTTP 访问：超时、重试、单源频控。"""

from __future__ import annotations

import time
from typing import Any

import httpx


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, user_agent: str, timeout: float = 20.0, follow_redirects: bool = True):
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        self._last_call = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self, qps_ms: int) -> None:
        if qps_ms <= 0:
            return
        wait = qps_ms / 1000 - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        qps_ms: int = 0,
        retries: int = 1,
    ) -> Any:
        headers = {"Referer": referer} if referer else None
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle(qps_ms)
            try:
                resp = self._client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - 采集层需要兜住所有异常
                last_err = exc
                if attempt < retries:
                    time.sleep(2)
        raise FetchError(f"{url} 请求失败: {last_err}") from last_err

    def get_text(
        self,
        url: str,
        *,
        referer: str | None = None,
        qps_ms: int = 0,
        retries: int = 1,
    ) -> str:
        """抓 HTML。给服务端渲染的第三方榜用（如 tunbang），不需要浏览器。"""
        headers = {"Referer": referer} if referer else None
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle(qps_ms)
            try:
                resp = self._client.get(url, headers=headers)
                resp.raise_for_status()
                if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = "utf-8"
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < retries:
                    time.sleep(2)
        raise FetchError(f"{url} 请求失败: {last_err}") from last_err
