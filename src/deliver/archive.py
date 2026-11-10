"""把「真正发出去的日报」归档到 `data/daily/<YYYY-MM-DD>/`。

为什么归档动作放在发送之后：
`data/daily` 的语义是「我收到过的日报」，不是「跑过的轮次」。
所以只有邮件发送成功才归档；试跑、预览、发送失败都留在 `sandbox/runs/`。

一个日期目录长这样：

    data/daily/2026-11-06/
    ├── report.md        # 人看
    ├── daily.json       # 程序读（正式产物）
    ├── materials.json   # 素材卡原始数据
    ├── meta.json        # 数据源状态、耗时
    ├── email.html       # 实际发出的邮件
    ├── email.txt        # 纯文本版
    └── sent.json        # 发送记录：时间/收件人/主题/来自哪一轮
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_sources
from . import digest as digest_mod
from .email_html import render_html, render_text

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / "data" / "daily"

# 归档保留的文件（不含 candidates.json 这类中间产物）
KEEP_FILES = ("report.md", "materials.json", "meta.json", "daily.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def archive_run(
    run_dir: Path,
    *,
    sent_at: str,
    to: list[str] | str,
    subject: str = "",
    trigger: str = "",
    html: str = "",
    text: str = "",
    daily_dir: Path | None = None,
) -> Path:
    """把一轮运行归档为当天的日报。返回归档目录。

    重复调用（比如重发）会覆盖同名文件并刷新 `sent.json`，不会产生第二份。
    """
    dest_root = daily_dir or DAILY_DIR
    meta = _read_json(run_dir / "meta.json")
    date = str(meta.get("date") or "")
    if not date:
        stamp = run_dir.name[:8]
        date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"

    dest = dest_root / date
    dest.mkdir(parents=True, exist_ok=True)

    for name in KEEP_FILES:
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)

    # 没有传入渲染结果时，用本轮数据重建（迁移历史目录时会走这里）
    # 但如果 report_data.json 已被清理，硬重建会得到「新歌0/文案0」的残缺邮件，
    # 那还不如不生成，只留 report.md 与 sent.json 说明。
    rebuilt = bool(html)
    note = ""
    if not html and (run_dir / "report_data.json").exists():
        try:
            cfg = load_sources().get("delivery", {}).get("email", {}) or {}
            limits = cfg.get("limits") or {}
            d = digest_mod.build(run_dir, limits)
            if not d.date:
                d.date = date
            subject = subject or digest_mod.subject_line(d)
            html = render_html(d, limits=limits)
            text = render_text(d)
            rebuilt = True
        except Exception:  # noqa: BLE001 - 归档不能因为渲染失败而中断
            html, text = "", ""
    elif not html:
        note = "email.html 未生成：本轮 report_data.json 已按清理规则删除，无法重建"

    if html:
        (dest / "email.html").write_text(html, encoding="utf-8")
    if text:
        (dest / "email.txt").write_text(text, encoding="utf-8")

    to_list = [to] if isinstance(to, str) else list(to)
    sent = {
        "sent_at": sent_at,
        "to": to_list,
        "subject": subject,
        "trigger": trigger,
        "run_dir": run_dir.name,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
    }
    if note:
        sent["note"] = note
    (dest / "sent.json").write_text(
        json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 给本轮目录打个标记，方便日后区分「发过的」和「试跑」
    marker = run_dir / "run.json"
    info = _read_json(marker) or {"kind": "unknown", "date": date, "created_at": ""}
    info.update({"sent": True, "sent_at": sent_at})
    marker.write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest
