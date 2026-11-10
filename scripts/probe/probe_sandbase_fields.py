"""探测 SandBase 返回的完整字段：找「使用人数」和「时间」类字段。

只调用一次，把响应原样存到 sandbox/probe_<aweme_id>.json，便于反复分析。

用法：
    python scripts/probe_sandbase_fields.py 7704624032147271906
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", encoding="utf-8")

sys.path.insert(0, str(ROOT))
from src.config import env  # noqa: E402

BASE = env("SANDBASE_BASE_URL") or "https://api.sandbase.ai"
PATH = "/v1/api/douyin/app-v3/one-video-v3"

WANT = ("count", "time", "stat", "use", "play", "duration", "create", "publish")


def walk(node, path="", depth=0, out=None):
    if out is None:
        out = []
    if depth > 5:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                walk(v, f"{path}.{k}" if path else k, depth + 1, out)
            elif any(w in k.lower() for w in WANT):
                out.append((f"{path}.{k}" if path else k, v))
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):
            walk(v, f"{path}[{i}]", depth + 1, out)
    return out


def main() -> int:
    aweme = sys.argv[1] if len(sys.argv) > 1 else "7704624032147271906"
    key = env("SANDBASE_API_KEY")
    if not key:
        print("未读到 SANDBASE_API_KEY")
        return 2

    url = BASE.rstrip("/") + PATH
    print(f"调用一次：{aweme}")
    resp = httpx.post(
        url,
        json={"aweme_id": aweme},
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    print("status:", resp.status_code, "size:", len(resp.content))

    data = resp.json()
    out = ROOT / "sandbox" / f"probe_{aweme}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已保存:", out)

    if isinstance(data, dict):
        print("\n顶层键:", list(data)[:15])

    hits = walk(data)
    print(f"\n含 count/time/stat/use 的字段（{len(hits)} 个）：")
    for k, v in hits[:40]:
        print(f"  {k} = {v}")

    # 定位 music 对象并完整打印
    def find_music(node, depth=0):
        if depth > 6:
            return None
        if isinstance(node, dict):
            if isinstance(node.get("music"), dict) and node["music"].get("title"):
                return node["music"]
            for v in node.values():
                r = find_music(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node[:5]:
                r = find_music(v, depth + 1)
                if r:
                    return r
        return None

    music = find_music(data)
    if music:
        print("\nmusic 对象全部字段：")
        for k, v in music.items():
            if not isinstance(v, (dict, list)):
                print(f"  {k} = {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
