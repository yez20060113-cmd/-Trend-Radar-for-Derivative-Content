"""运行目录与落盘。所有产物默认写 sandbox，正式归档写 data/daily。"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def now_local(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def new_run_dir(base: Path, now: datetime) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M")
    candidate = base / stamp
    if candidate.exists():
        for i in range(2, 100):
            alt = base / f"{stamp}-{i}"
            if not alt.exists():
                candidate = alt
                break
    candidate.mkdir(parents=True)
    return candidate


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def clean_old(base: Path, days: int) -> list[str]:
    """删除 N 天前的运行目录，返回被删的目录名。

    年龄按**目录名里的日期**算，不用 mtime：清理本身会改动目录内容、刷新 mtime，
    用 mtime 会导致「7 天前」永远算不出来（`prune` 已踩过这个坑）。
    """
    if not base.exists():
        return []
    today = datetime.now().date()
    removed: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if _age_days(child, today) > days:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed



def _age_days(path: Path, today: "date") -> float:
    """按目录名（YYYYMMDD-HHMM）算年龄。

    不能用 mtime：清理会改动目录内容，mtime 被刷新后「7 天前」永远算不出来。
    """
    try:
        d = date(int(path.name[0:4]), int(path.name[4:6]), int(path.name[6:8]))
        return float((today - d).days)
    except (ValueError, IndexError):
        return (datetime.now().timestamp() - path.stat().st_mtime) / 86400

# ---- 分层清理 -------------------------------------------------------------

# 调试用的大文件：占单轮体积 90%，且都能重算
HEAVY_FILES = (
    "candidates.json",
    "report_data.json",
    "phrases_todo.json",
    "bgm_plan.json",
    "run.log",
    "judged.json",        # 早期版本的产物名，代码已不再生成；历史目录里还存在
)
HEAVY_DIRS = ("email",)          # preview.html/txt，只在 --no-email 时生成

# 轻量资产：一直保留（report.md 13KB + materials.json 45KB / 天）
KEEP_FILES = ("report.md", "materials.json", "meta.json")


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _drop_heavy(run_dir: Path) -> int:
    """删掉一轮里的重文件，保留 report / materials / meta。返回释放字节。"""
    freed = 0
    for name in HEAVY_FILES:
        f = run_dir / name
        if f.exists():
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    for name in HEAVY_DIRS:
        d = run_dir / name
        if d.exists():
            freed += _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
    return freed


def prune(
    runs_dir: Path,
    *,
    heavy_days: int = 7,
    log_days: int = 30,
    diag_days: int = 3,
    diag_max: int = 50,
    max_days: int = 0,
) -> dict[str, float]:
    """分层清理。

    规则：
    1. **每个自然日只保留最新一轮的重文件**（candidates.json 等），其余立即删
    2. 超过 `heavy_days` 的日期，连最新一轮的重文件也删
    3. `max_days > 0` 时，整轮目录超过该天数就全删
    4. `sandbox/logs/*.log` 超过 `log_days` 删除
    5. `sandbox/diag/` 超过 `diag_days` 删除，且总截图数不超过 `diag_max`

    返回 {"freed_mb", "removed_files", "dropped_dirs", "logs", "diag"}。
    """
    stats = {"freed_bytes": 0, "dropped_dirs": 0, "logs": 0, "diag": 0}
    now = datetime.now().timestamp()
    today = datetime.now().date()

    if runs_dir.exists():
        groups: dict[str, list[Path]] = {}
        for child in runs_dir.iterdir():
            if child.is_dir():
                groups.setdefault(child.name[:8], []).append(child)

        for date_key, dirs in groups.items():
            dirs.sort(key=lambda p: p.name, reverse=True)     # 最新在前
            newest = dirs[0]
            age = _age_days(newest, today)
            for i, d in enumerate(dirs):
                if max_days and _age_days(d, today) > max_days:
                    stats["freed_bytes"] += _dir_size(d)
                    shutil.rmtree(d, ignore_errors=True)
                    stats["dropped_dirs"] += 1
                    continue
                keep_heavy = (i == 0) and (age <= heavy_days)
                if not keep_heavy:
                    stats["freed_bytes"] += _drop_heavy(d)

    logs_dir = runs_dir.parent / "logs"
    if logs_dir.exists() and log_days > 0:
        for f in logs_dir.glob("*.log"):
            if (now - f.stat().st_mtime) / 86400 > log_days:
                stats["freed_bytes"] += f.stat().st_size
                f.unlink(missing_ok=True)
                stats["logs"] += 1

    diag_dir = runs_dir.parent / "diag"
    if diag_dir.exists() and diag_days > 0:
        shots: list[Path] = []
        for d in list(diag_dir.iterdir()):
            if not d.is_dir():
                continue
            if (now - d.stat().st_mtime) / 86400 > diag_days:
                stats["freed_bytes"] += _dir_size(d)
                shutil.rmtree(d, ignore_errors=True)
                stats["diag"] += 1
                continue
            shots.extend(sorted(d.glob("*.jpg")))
        if len(shots) > diag_max:
            for f in shots[: len(shots) - diag_max]:
                stats["freed_bytes"] += f.stat().st_size
                f.unlink(missing_ok=True)
                stats["diag"] += 1

    return stats

