"""打开抖音视频页，读标题/文案/互动数据（CDP，零 token）。

用法：python scripts/probe_video.py <url1> [url2 ...]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

try:  # 直接运行 `python scripts/probe/xxx.py` 时，sys.path[0] 就是本目录
    from _env import strip_proxy_for_localhost
except ImportError:  # 按模块方式运行时的回退
    from scripts.probe._env import strip_proxy_for_localhost

strip_proxy_for_localhost()



def main(urls: list[str]) -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                title = page.title()
                body = page.inner_text("body")[:700].replace("\n", " ")
                print(f"URL: {url}")
                print(f"TITLE: {title}")
                print(f"BODY: {body[:500]}")
                print("-" * 60)
            except Exception as exc:  # noqa: BLE001
                print(f"URL: {url}\nERR: {exc}\n{'-' * 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
