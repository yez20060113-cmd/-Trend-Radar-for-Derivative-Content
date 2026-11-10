"""把「每日自动跑」注册成 Windows 计划任务（三条触发器 + 单日锁）。

为什么用 Python 生成 XML 而不是写 .ps1 / .bat：
本项目的路径含中文（`D:\\沃克巴蒂\\产品探索`），批处理脚本在部分代码页下会把中文路径写坏；
计划任务 XML 用 UTF-16 写盘，`schtasks /XML` 读取时不受控制台代码页影响。

三条触发器：
1. **登录后 2 分钟** —— 适合「关机过夜」的情况
2. **每天 09:00** —— 适合「长期不关机」的情况
3. **每天 11:30** —— 只在前一次失败（attempts < 2）时才真正执行，成功会被单日锁挡住

三者调用同一个脚本，由 `data/state/daily_lock.json` 保证一天只成功跑一次。

用法：
    python scripts/install_task.py            # 注册/覆盖
    python scripts/install_task.py --query    # 查看状态
    python scripts/install_task.py --run-now  # 立刻触发一次（验证用）
    python scripts/install_task.py --remove   # 删除
    python scripts/install_task.py --save-xml sandbox/daily_task.xml   # 生成 XML 手动导入
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_sources  # noqa: E402

TASK_NAME = "StarlightRadarDaily"
SCRIPT = ROOT / "scripts" / "daily_run.py"


def auto_cfg() -> dict:
    """读 `automation` 段；配置缺失时退回内置默认，不让注册失败。"""
    try:
        return load_sources().get("automation", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def python_exe() -> str:
    """优先用 pythonw.exe：计划任务到点不会弹出黑色控制台窗口。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    return f"{domain}\\{user}" if domain and user else user


def next_start(time_str: str) -> datetime:
    hour, minute = (int(x) for x in time_str.split(":"))
    now = datetime.now()
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return start


def build_xml(time_str: str, delay_min: int, retry_time: str = "11:30") -> str:
    start = next_start(time_str)
    retry = next_start(retry_time)
    user = current_user()
    py = python_exe()
    args = f'"{SCRIPT}" --trigger auto'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>追星二创雷达：采集抖音/榜单并邮件投递日报（每日一次）</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT{delay_min}M</Delay>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>{start.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
    <!-- 失败兜底：只在前一次失败（attempts<2）时才真正执行，成功则被单日锁挡住 -->
    <CalendarTrigger>
      <StartBoundary>{retry.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        encoding="gbk",
        errors="replace",
    )


def register(time_str: str, delay_min: int, retry_time: str) -> int:
    xml = build_xml(time_str, delay_min, retry_time)
    # schtasks 要求 XML 为 Unicode；utf-16 会自带 BOM
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", delete=False, encoding="utf-16"
    ) as fh:
        fh.write(xml)
        tmp = fh.name
    try:
        proc = schtasks("/Create", "/TN", TASK_NAME, "/XML", tmp, "/F")
    finally:
        Path(tmp).unlink(missing_ok=True)

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0:
        print("✅ 已注册计划任务")
        print(f"   任务名：{TASK_NAME}")
        print(f"   触发器：登录后 {delay_min} 分钟 ＋ 每天 {time_str}（失败则 {retry_time} 重试）")
        print(f"   命令：{python_exe()} {SCRIPT} --trigger auto")
        print(f"   工作目录：{ROOT}")
        print("\n提示：单日锁在 data/state/daily_lock.json，三条触发器只会成功跑一次。")
        return 0
    print("❌ 注册失败：", out)
    print("\n若提示权限不足：用「以管理员身份运行」的终端再执行一次；")
    print("或用 --save-xml 生成 XML，在「任务计划程序 → 导入任务」里手动导入。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    cfg = auto_cfg()
    ap.add_argument("--time", default=str(cfg.get("daily_time", "09:00")),
                    help="每日触发时间 HH:MM（默认取 config 的 automation.daily_time）")
    ap.add_argument("--delay-min",
                    type=int, default=max(1, int(cfg.get("boot_delay_seconds", 120)) // 60),
                    help="登录后延迟分钟数（默认取 automation.boot_delay_seconds）")
    ap.add_argument("--retry-time", default=str(cfg.get("retry_time", "11:30")),
                    help="失败重试时间 HH:MM")
    ap.add_argument("--save-xml", default="", help="只生成任务 XML（手动导入任务计划程序用）")
    ap.add_argument("--query", action="store_true")
    ap.add_argument("--run-now", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    if not SCRIPT.exists():
        print(f"❌ 找不到 {SCRIPT}")
        return 2

    if args.save_xml:
        out = Path(args.save_xml).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_xml(args.time, args.delay_min, args.retry_time), encoding="utf-16")
        print(f"已生成任务 XML：{out}")
        print("导入方式：任务计划程序 → 右侧「导入任务」→ 选择该文件 → 确定")
        return 0

    if args.query:
        proc = schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
        print((proc.stdout or proc.stderr or "").strip())
        return 0 if proc.returncode == 0 else 1

    if args.run_now:
        proc = schtasks("/Run", "/TN", TASK_NAME)
        print((proc.stdout or proc.stderr or "").strip())
        return 0 if proc.returncode == 0 else 1

    if args.remove:
        proc = schtasks("/Delete", "/TN", TASK_NAME, "/F")
        print((proc.stdout or proc.stderr or "").strip())
        return 0 if proc.returncode == 0 else 1

    return register(args.time, args.delay_min, args.retry_time)


if __name__ == "__main__":
    raise SystemExit(main())
