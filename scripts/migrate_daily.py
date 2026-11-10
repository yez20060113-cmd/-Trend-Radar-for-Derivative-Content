"""一次性迁移：把已经发过的历史日报归档成 `data/daily/<日期>/`。

背景：早期 `--prod` 把正式 JSON 写成 `data/daily/2026-11-06.json`，
而日报本体留在 `sandbox/runs/<时间戳>/report.md`，两处分散。
本脚本把「确实发过邮件」的那一轮复制成日期目录，并把旧 `.json` 收进去。

幂等：重复执行只会覆盖同名文件，不会产生第二份。

用法：
    python scripts/migrate_daily.py            # 迁移内置的两天
    python scripts/migrate_daily.py --dry-run  # 只打印计划
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import env  # noqa: E402
from src.deliver.archive import DAILY_DIR, archive_run  # noqa: E402

# 日期 → (运行目录名, 发送时间, 实际发出的邮件主题)
KNOWN = {
    "2026-11-06": (
        "20261106-1731", "2026-11-06T17:35:06",
        "【追星二创雷达】11-06｜圈内8 跨圈3 新歌8 文案4",
    ),
    "2026-11-07": (
        "20261107-1153", "2026-11-07T11:53:19",
        "【追星二创雷达】11-07｜圈内8 跨圈1 新歌8",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    to = env("SMTP_TO") or env("SMTP_USER") or "unknown"
    runs = ROOT / "sandbox" / "runs"
    ok = 0

    for date, (run_name, sent_at, subject) in KNOWN.items():
        run_dir = runs / run_name
        if not (run_dir / "report.md").exists():
            print(f"跳过 {date}：找不到 {run_dir}")
            continue

        print(f"{date} ← {run_name}（sent_at={sent_at}）")
        if args.dry_run:
            continue

        # 历史上的 report_data.json 已被清理，重建只会得到残缺邮件，
        # 先清掉之前误生成的版本
        stale = DAILY_DIR / date
        for name in ("email.html", "email.txt"):
            (stale / name).unlink(missing_ok=True)

        dest = archive_run(
            run_dir,
            sent_at=sent_at,
            to=[to],
            subject=subject,
            trigger="manual",
        )

        # 旧的 data/daily/<date>.json 收进目录，改名为 daily.json
        legacy = DAILY_DIR / f"{date}.json"
        target = dest / "daily.json"
        if legacy.exists() and not target.exists():
            shutil.move(str(legacy), str(target))
            print(f"  旧 JSON 已并入：{target.name}")

        files = sorted(p.name for p in dest.iterdir())
        print(f"  归档完成：{dest}")
        print(f"  内容：{', '.join(files)}")
        ok += 1

    print(f"\n共处理 {ok} 天。归档根目录：{DAILY_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
