"""启动带调试端口的 Edge（脱离父进程，会话结束不会被回收）。

用法：
    python scripts/launch_edge.py           # 启动并等待端口就绪
    python scripts/launch_edge.py --check   # 只检查端口是否已就绪
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / ".edge-cdp"
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9222


def port_ready(timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查端口")
    ap.add_argument("--headless", action="store_true", help="无窗口启动（定时任务用，省得弹窗）")
    ap.add_argument("--url", default="about:blank")
    args = ap.parse_args()

    if args.check:
        print("ready" if port_ready() else "not-ready")
        return 0 if port_ready() else 1

    if port_ready():
        print("已在运行（端口就绪）")
        return 0

    edge = next((p for p in CANDIDATES if Path(p).exists()), None)
    if not edge:
        print("未找到 msedge.exe")
        return 2

    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [
        edge,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
    ]
    if args.headless:
        cmd.append("--headless=new")
        cmd.append("--disable-gpu")
    cmd.append(args.url)
    subprocess.Popen(
        cmd,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

    for _ in range(30):
        if port_ready():
            print("已启动，端口就绪")
            return 0
        time.sleep(1)
    print("启动超时：端口未就绪", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
