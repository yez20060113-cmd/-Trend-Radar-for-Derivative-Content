"""探测 SandBase 接口：确认请求格式与返回字段（填好 key 后先跑这个）。

为什么要先探测：
不同服务商的参数位置（body / query）、鉴权头、返回结构都不一样。
盲猜会让整条采样链静默失败，所以先用一个已知视频试出正确格式。

用法：
    python scripts/probe_sandbase.py 7704624032147271906
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from src.config import env  # noqa: E402
from src.collectors.bgm_sampler import _find_music  # noqa: E402

BASE = env("SANDBASE_BASE_URL") or "https://api.sandbase.ai"
PATH = "/v1/api/douyin/app-v3/one-video-v3"

SHAPES = [
    ("POST json aweme_id", "post", {"json": {"aweme_id": "{id}"}}),
    ("POST json item_id", "post", {"json": {"item_id": "{id}"}}),
    ("POST json url", "post", {"json": {"url": "https://www.douyin.com/video/{id}"}}),
    ("GET query aweme_id", "get", {"params": {"aweme_id": "{id}"}}),
]


def main() -> int:
    aweme = sys.argv[1] if len(sys.argv) > 1 else "7704624032147271906"
    key = env("SANDBASE_API_KEY")
    if not key:
        print("未读到 SANDBASE_API_KEY，请先填入 .env")
        return 2

    url = BASE.rstrip("/") + PATH
    print(f"接口：{url}\n视频：{aweme}\n")

    with httpx.Client(timeout=25) as cli:
        for label, method, kwargs in SHAPES:
            payload = json.loads(json.dumps(kwargs).replace("{id}", aweme))
            try:
                if method == "post":
                    resp = cli.post(url, headers={"Authorization": f"Bearer {key}"}, **payload)
                else:
                    resp = cli.get(url, headers={"Authorization": f"Bearer {key}"}, **payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[{label}] 请求失败：{exc}")
                continue

            print(f"[{label}] status={resp.status_code} len={len(resp.text)}")
            if resp.status_code != 200:
                print("   ", resp.text[:200].replace("\n", " "))
                continue

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                print("    非 JSON 响应：", resp.text[:160])
                continue

            music = _find_music(data)
            if music:
                print("    ✅ 找到 music：")
                for k in ("title", "author", "mid", "id_str", "duration", "user_count", "listen_count"):
                    if k in music:
                        print(f"       {k} = {music[k]}")
                return 0
            print("    未找到 music，顶层键：", list(data)[:8] if isinstance(data, dict) else type(data).__name__)

    print("\n四种格式都没拿到 music。请把上面的响应片段发我，我按实际结构调整解析。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
