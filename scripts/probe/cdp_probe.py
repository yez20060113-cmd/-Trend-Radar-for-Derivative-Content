"""CDP 冒烟测试：连上已开的 Edge，打开抖音搜索页，抽视频卡片。

用法：python scripts/cdp_probe.py "A2O LITE"
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

try:  # 直接运行 `python scripts/probe/xxx.py` 时，sys.path[0] 就是本目录
    from _env import strip_proxy_for_localhost
except ImportError:  # 按模块方式运行时的回退
    from scripts.probe._env import strip_proxy_for_localhost

strip_proxy_for_localhost()


JS_EXTRACT = """
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/video/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!href || text.length < 8 || seen.has(href)) return;
    seen.add(href);
    out.push({ url: href, text });
  });
  return out;
}
"""


def main(keyword: str) -> int:
    q = urllib.parse.quote(keyword)
    url = f"https://www.douyin.com/search/{q}?type=video&publish_time=7"
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(8000)
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(2500)
        items = page.evaluate(JS_EXTRACT)
        body = page.inner_text("body")[:200].replace("\n", " ")
        print(f"title={page.title()[:60]}")
        print(f"body={body}")
        Path("sandbox").mkdir(parents=True, exist_ok=True)
        page.screenshot(path="sandbox/cdp_probe.png", full_page=False)
        print(f"keyword={keyword} items={len(items)}")
        for it in items[:8]:
            print(json.dumps(it, ensure_ascii=False)[:160])
        # 只关自己开的页签：browser.close() 会把整个 Edge 关掉，下次还得重开
        page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "A2O LITE"))
