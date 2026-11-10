"""probe 脚本共用：连本地 Edge 前必须清掉代理。

本机 shell 常带 `HTTP_PROXY=http://127.0.0.1:xxxxx`。Playwright 请求
`http://127.0.0.1:9222/json/version` 时会被丢给代理，表现成
`Unexpected status 502 when connecting to 127.0.0.1:9222`。

清理必须在 `sync_playwright()` **之前**——Playwright 的 Node 驱动在那一刻
就继承了环境，之后再改 `os.environ` 对它无效。
"""

from __future__ import annotations

import os

_PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
)


def strip_proxy_for_localhost() -> list[str]:
    """清掉代理变量并放行 localhost，返回被清掉的键名。"""
    removed = [k for k in _PROXY_KEYS if os.environ.pop(k, None) is not None]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost,::1"
    return removed
