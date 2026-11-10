"""探测：抖音视频页能否通过 CDP 拦截到真实 BGM 信息。

思路：不伪造签名，只监听浏览器**自己发出**的请求响应。
若 detail 接口返回 music.title，就能以零成本拿到每条视频的真实 BGM。

用法：
    python scripts/probe_music.py 7704624032147271906
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

try:  # 直接运行 `python scripts/probe/xxx.py` 时，sys.path[0] 就是本目录
    from _env import strip_proxy_for_localhost
except ImportError:  # 按模块方式运行时的回退
    from scripts.probe._env import strip_proxy_for_localhost

strip_proxy_for_localhost()


TARGET_HINTS = ("aweme/detail", "aweme/v1/web/aweme", "music")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python scripts/probe_music.py <aweme_id>")
        return 2
    aweme_id = sys.argv[1].strip()
    url = f"https://www.douyin.com/video/{aweme_id}"

    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        def on_response(resp) -> None:
            u = resp.url
            if not any(h in u for h in ("aweme/detail", "aweme/v1/web/aweme")):
                return
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                return
            captured.append({"url": u[:120], "len": len(body), "body": body[:200000]})

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(9000)

        # 兜底：页面内嵌 state
        state_keys = page.evaluate(
            "() => Object.keys(window).filter(k => /state|DATA|ROUTER/i.test(k)).slice(0, 20)"
        )
        page.close()

    print(f"捕获响应：{len(captured)} 条")
    found = False
    for item in captured:
        try:
            data = json.loads(item["body"])
        except Exception:  # noqa: BLE001
            continue
        detail = data.get("aweme_detail") or {}
        music = detail.get("music") or {}
        if music.get("title"):
            found = True
            print("=== 命中 music ===")
            print("  歌名:", music.get("title"))
            print("  作者:", music.get("author"))
            print("  music_id:", music.get("mid") or music.get("id_str"))
            print("  时长:", music.get("duration"), "ms")
            print("  使用量字段:", {k: music.get(k) for k in ("user_count", "listen_count")})
    print("页面内嵌 state 键:", state_keys[:10])
    if not found:
        print("未从响应中解析出 music。")
        for item in captured[:3]:
            print("  样本:", item["url"], "len=", item["len"])
            print("   ", item["body"][:200].replace("\n", " "))
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
