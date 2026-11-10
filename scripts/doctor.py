"""环境自检：一条命令把「跑不起来」的常见原因全查一遍。

2026-11-09 踩过的三类坑，本来都要靠一层层剥才知道：
- shell 带 `HTTP_PROXY`，Playwright 连 127.0.0.1:9222 被代理拦成 502
- Edge 没起 / 起了又被回收，端口不通
- `--limit` 覆盖采集文件，数据被冲掉

用法：
    python scripts/doctor.py            # 只做本地检查，不联网、不花钱
    python scripts/doctor.py --llm      # 额外测一次模型连通性（1 次调用，几厘钱）
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "✓", "⚠", "✗"
failures: list[str] = []
warnings: list[str] = []


def line(mark: str, title: str, detail: str = "") -> None:
    print(f" {mark} {title}" + (f"：{detail}" if detail else ""))


def check_python() -> None:
    v = sys.version_info
    mark = OK if v >= (3, 10) else BAD
    if mark == BAD:
        failures.append("Python 版本过低（需要 3.10+）")
    line(mark, "Python", f"{v.major}.{v.minor}.{v.micro}")


def check_deps() -> None:
    # 与 requirements.txt 对齐：项目用 httpx，不是 requests
    mods = ("playwright", "yaml", "dotenv", "pydantic", "httpx")
    missing = []
    for mod in mods:
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    if missing:
        failures.append(f"缺少依赖：{', '.join(missing)}（pip install -r requirements.txt）")
        line(BAD, "依赖", "缺失 " + ", ".join(missing))
    else:
        line(OK, "依赖", " / ".join(mods))


def mask(value: str) -> str:
    if not value:
        return ""
    return "…" + value[-4:] if len(value) > 8 else "已设置"


def check_env() -> None:
    from src.config import env

    required = ["SMTP_USER", "SMTP_PASS"]
    optional = ["SMTP_TO", "SANDBASE_API_KEY", "RADAR_LLM_KEY"]
    for key in required:
        if env(key):
            line(OK, f".env {key}", mask(env(key)))
        else:
            failures.append(f".env 缺少 {key}（发信会失败）")
            line(BAD, f".env {key}", "未设置")
    for key in optional:
        if env(key):
            line(OK, f".env {key}", mask(env(key)))
        else:
            line(WARN, f".env {key}", "未设置（相关功能会自动降级）")
            warnings.append(f"{key} 未设置")


def check_proxy() -> None:
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
    live = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if not live:
        line(OK, "代理变量", "无")
        return
    seen = ", ".join(f"{k}={v}" for k, v in list(live.items())[:2])
    # 采集脚本在启动 Playwright 之前会自动清掉，所以这里只是提醒，不算失败
    line(WARN, "代理变量", seen + "（采集脚本启动时会自动清掉）")
    warnings.append("存在代理变量，若其他工具连 localhost 失败请留意")


def check_edge() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        ready = s.connect_ex(("127.0.0.1", 9222)) == 0
    if ready:
        line(OK, "Edge 调试端口", "127.0.0.1:9222 可连")
    else:
        line(BAD, "Edge 调试端口", "不通（跑采集前执行 python scripts/launch_edge.py）")
        failures.append("9222 不通，采集无法启动")


def check_data() -> None:
    latest = ROOT / "data" / "input" / "douyin_latest.json"
    plan = ROOT / "data" / "input" / "query_plan.json"
    if latest.exists():
        try:
            d = json.loads(latest.read_text(encoding="utf-8"))
            qs = d.get("queries") or []
            ok = sum(1 for q in qs if not q.get("error"))
            items = sum(len(q.get("items") or []) for q in qs)
            mark = OK if ok else WARN
            line(mark, "采集数据", f"{ok}/{len(qs)} 个查询成功，{items} 条内容")
            if not ok:
                warnings.append("采集文件里没有成功查询")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"采集文件损坏：{exc}")
            line(BAD, "采集数据", f"解析失败：{exc}")
    else:
        line(WARN, "采集数据", "还没有 douyin_latest.json")

    if plan.exists():
        try:
            d = json.loads(plan.read_text(encoding="utf-8"))
            n = len(d.get("queries") or [])
            line(OK, "检索计划", f"{n} 个查询（{d.get('date', '')}）")
        except Exception as exc:  # noqa: BLE001
            line(BAD, "检索计划", f"解析失败：{exc}")
            failures.append("query_plan.json 解析失败")
    else:
        line(WARN, "检索计划", "还没有 query_plan.json")


def check_llm_cfg() -> None:
    from src.config import load_sources

    try:
        cfg = load_sources().get("llm", {}) or {}
    except Exception as exc:  # noqa: BLE001
        failures.append(f"配置读取失败：{exc}")
        line(BAD, "配置", str(exc))
        return
    layers = cfg.get("layers", {}) or {}
    on = [k for k, v in layers.items() if (v or {}).get("enabled")]
    if not cfg.get("enabled"):
        line(WARN, "模型层", "总开关关着（llm.enabled=false）")
        return
    line(OK, "模型层", f"{cfg.get('model')}｜启用层：{', '.join(on) or '无'}")
    if cfg.get("enabled") and not os.getenv(cfg.get("api_key_env", "RADAR_LLM_KEY")):
        failures.append("模型层已开启但缺少 key")
        line(BAD, "模型 key", "llm.enabled=true 但 key 未设置")


def check_runs() -> None:
    runs = ROOT / "sandbox" / "runs"
    if not runs.exists():
        line(WARN, "运行记录", "sandbox/runs 不存在")
        return
    dirs = sorted([p for p in runs.iterdir() if p.is_dir()], reverse=True)[:1]
    if not dirs:
        line(WARN, "运行记录", "还没有跑过")
        return
    d = dirs[0]
    meta = d / "meta.json"
    if meta.exists():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            line(OK, "最近一轮", f"{d.name}｜{m.get('materials', '?')} 张素材卡｜"
                                 f"{m.get('generated_at', '')}")
        except Exception:  # noqa: BLE001
            line(OK, "最近一轮", d.name)
    else:
        line(OK, "最近一轮", d.name)


def check_llm_call() -> None:
    from src.config import load_sources
    from src.llm.client import probe

    llm = probe(load_sources().get("llm", {}))
    if not llm.available:
        line(WARN, "模型连通", llm.why_unavailable())
        return
    res = llm.json_call(
        layer="check",
        system="你是连通性探测服务。只输出 JSON。",
        user='返回 {"ok": true} 这个 JSON。',
        max_tokens=64,
        use_cache=False,
    )
    if res.ok:
        line(OK, "模型连通", f"{llm.model}｜{res.elapsed_ms} ms")
    else:
        failures.append(f"模型调用失败：{res.error[:60]}")
        line(BAD, "模型连通", res.error[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="额外测一次模型连通（会产生调用）")
    args = ap.parse_args()

    print("== 环境自检 ==")
    check_python()
    check_deps()
    check_env()
    check_proxy()
    check_edge()
    check_data()
    check_llm_cfg()
    check_runs()
    if args.llm:
        check_llm_call()

    print()
    if failures:
        print(f"❌ {len(failures)} 项必须处理：")
        for f in failures:
            print(f"   · {f}")
    if warnings:
        print(f"⚠️  {len(warnings)} 项提醒：")
        for w in warnings:
            print(f"   · {w}")
    if not failures:
        print("✅ 关键项全部通过" + ("（有提醒项，不影响主流程）" if warnings else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
