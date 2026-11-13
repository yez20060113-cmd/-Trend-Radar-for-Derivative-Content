"""每日自动跑：单日锁 → 确保 Edge → 生成计划 → 采集 → 判定出报 → 发邮件。

为什么要单日锁，而不是「判断今天有没有跑过」：
- 开机触发与 9:00 触发可能同时发生（9 点开机）
- 采集要 20 分钟，9:00 触发时开机那轮可能还在跑
- 采集成功但发信失败，也必须允许重试

所以锁是状态机（status + attempts + started_at），不是布尔值。

用法：
    python scripts/daily_run.py                 # 正常跑（锁决定是否跳过）
    python scripts/daily_run.py --force         # 忽略锁
    python scripts/daily_run.py --email-only    # 不跑采集，用今天最新一版出报并发信
    python scripts/daily_run.py --no-email      # 只出报，把邮件写到 sandbox 供预览
    python scripts/daily_run.py --skip-collect  # 复用现有数据，只跑计划与判定
    python scripts/daily_run.py --email-only --no-archive   # 试发一封，不覆盖当天归档

试跑与正式的边界（2026-11-09 起）：
- `python -m src.main --run` 产出的轮次带 `run.json: {"kind": "test"}`，只进 `sandbox/runs/`
- 发信前会检查这一轮的类型：`kind=test` 默认**拒绝发信**（只写预览），确需发送加 `--allow-test`
- 这样做是因为原来「先发信、再决定归档」，一次 `--email-only` 就会把试跑稿真的发出去；
  归档安全不等于邮箱安全
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_sources  # noqa: E402
from src.deliver import digest as digest_mod  # noqa: E402
from src.deliver.archive import archive_run  # noqa: E402
from src.deliver.email_html import render_html, render_text  # noqa: E402
from src.deliver.smtp import load_config as load_email_config, send as send_email  # noqa: E402

LOCK_PATH = ROOT / "data" / "state" / "daily_lock.json"
LOG_DIR = ROOT / "sandbox" / "logs"
RUNS_DIR = ROOT / "sandbox" / "runs"
STALE_RUNNING_MIN = 90      # running 超过这么久视为僵尸锁
MAX_ATTEMPTS = 2


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Lock:
    date: str = ""
    status: str = ""            # running / success / failed
    trigger: str = ""
    attempts: int = 0
    started_at: str = ""
    finished_at: str = ""
    note: str = ""

    @property
    def started_ts(self) -> float:
        try:
            return datetime.fromisoformat(self.started_at).timestamp()
        except ValueError:
            return 0.0


def _read_json(path: Path) -> dict:
    """读 JSON，失败返回空 dict（状态文件损坏不该阻断发信）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_lock() -> Lock:
    if not LOCK_PATH.exists():
        return Lock()
    try:
        return Lock(**json.loads(LOCK_PATH.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return Lock()


def save_lock(lock: Lock) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(
        json.dumps(asdict(lock), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def decide(lock: Lock, today: str, force: bool) -> tuple[bool, str]:
    """返回 (是否继续, 原因)。"""
    if force:
        return True, "force"
    if lock.date != today:
        return True, "新的一天"
    if lock.status == "success":
        return False, f"今天已成功（{lock.finished_at or lock.started_at}）"
    if lock.status == "running":
        age_min = (time.time() - lock.started_ts) / 60 if lock.started_ts else 0
        if age_min < STALE_RUNNING_MIN:
            return False, f"另一路径正在跑（已 {age_min:.0f} 分钟）"
        return True, "僵尸锁，抢占重跑"
    if lock.status == "failed" and lock.attempts >= MAX_ATTEMPTS:
        return False, f"今日已失败 {lock.attempts} 次，不再重试"
    return True, "上次失败，重试"


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"{stamp} {msg}"
    # 计划任务可能用 pythonw 启动，此时 sys.stdout 为 None，print 会静默跳过
    if sys.stdout is not None:
        print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"daily-{today_str()}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def child_env() -> dict[str, str]:
    env_vars = dict(os.environ)
    env_vars["PYTHONIOENCODING"] = "utf-8"
    # 本机 CDP 不能走代理，否则 playwright 连不上 127.0.0.1:9222
    no_proxy = "127.0.0.1,localhost"
    env_vars["NO_PROXY"] = ",".join(filter(None, [env_vars.get("NO_PROXY", ""), no_proxy]))
    env_vars["no_proxy"] = env_vars["NO_PROXY"]
    return env_vars


def run_step(name: str, args: list[str], timeout: int) -> None:
    log(f"▶ {name}：{' '.join(args[-2:])}")
    started = time.time()
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        env=child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    cost = time.time() - started
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{name} 失败（exit={proc.returncode}）：" + " / ".join(tail))
    log(f"✔ {name}（{cost:.0f}s）")


def port_ready(port: int = 9222) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def edge_path() -> str | None:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return None


def ensure_edge(headless: bool) -> subprocess.Popen | None:
    """确保 9222 可用；自己启动时返回进程句柄，跑完由调用方结束。

    不 detach：脱离父进程的 Edge 在这台机器上会被回收（此前踩过），
    而采集只需要它在跑的这 20 分钟。
    """
    if port_ready():
        log("Edge 端口已就绪")
        return None

    exe = edge_path()
    if not exe:
        raise RuntimeError("找不到 msedge.exe")
    cmd = [exe, "--remote-debugging-port=9222",
           f"--user-data-dir={ROOT / '.edge-cdp'}"]
    if headless:
        cmd += ["--headless=new", "--disable-gpu"]
    cmd.append("about:blank")
    log("Edge 未就绪，启动" + ("（headless）" if headless else "（窗口）"))

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        if port_ready():
            log("Edge 启动成功")
            return proc
        if proc.poll() is not None:
            break
        time.sleep(2)
    proc.terminate()
    raise RuntimeError("Edge 启动失败：9222 端口未就绪（可能需要重新登录抖音）")


def build_mail(run_dir: Path, cfg: dict) -> tuple[str, str, str]:
    limits = (cfg.get("delivery", {}).get("email", {}) or {}).get("limits") or {}
    d = digest_mod.build(run_dir, limits)
    if not d.date:
        d.date = today_str()
    return digest_mod.subject_line(d), render_html(d, limits=limits), render_text(d)


def notify_failure(reason: str, cfg: dict) -> None:
    """失败告警：走同一套 SMTP，发一封短邮件。发不出去就只写日志。"""
    conf = load_email_config(cfg.get("delivery", {}).get("email") or {})
    if not conf:
        log("（未配置邮箱，跳过失败告警）")
        return
    text = f"今日自动跑失败：\n\n{reason}\n\n时间：{now_iso()}\n"
    html = (
        f'<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:14px">'
        f'<h3 style="color:#e5484d;margin:0 0 8px">今日自动跑失败</h3>'
        f'<pre style="white-space:pre-wrap;font-size:13px;color:#333">{reason}</pre>'
        f'<p style="color:#8a8f99;font-size:12px">时间：{now_iso()}</p></div>'
    )
    try:
        send_email(
            conf,
            subject=f"【追星二创雷达】{today_str()} 运行失败",
            html=html,
            text=text,
        )
        log("已发送失败告警邮件")
    except Exception as exc:  # noqa: BLE001
        log(f"失败告警邮件也没发出去：{exc}")


def should_resume(today: str) -> bool:
    """今天已有**部分**采集结果时，用 `--resume` 续采，而不是从头再来。

    判据：文件是今天的，且成功查询数在 0 与计划数之间（中途被限流/页签崩掉过）。
    文件是昨天的不续采——那会把昨天的查询当成今天的成果，直接跳过。
    """
    path = ROOT / "data" / "input" / "douyin_latest.json"
    data = _read_json(path)
    if not data:
        return False
    if not str(data.get("collected_at") or "").startswith(today):
        return False
    total = int(data.get("plan_total") or 0)
    ok = sum(1 for q in (data.get("queries") or []) if not q.get("error"))
    return bool(total) and 0 < ok < total


def douyin_coverage() -> tuple[int, int]:
    """今天采到多少个查询（成功 / 计划）。"""
    path = ROOT / "data" / "input" / "douyin_latest.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0, 0
    ok = sum(1 for q in d.get("queries", []) if not q.get("error"))
    return ok, int(d.get("plan_total") or len(d.get("queries", [])))


def should_archive(
    run_dir: Path, today: str, *, force: bool = False, disable: bool = False
) -> tuple[bool, str]:
    """这一轮要不要写进 `data/daily/<日期>/`。

    规则：`official` 轮次照常写；其余轮次（`test` / `unknown`）只在
    「当天还没有归档」或显式 `force` 时写——避免一次试发把收到的那份换掉。
    """
    if disable:
        return False, "--no-archive：跳过归档"
    meta = _read_json(run_dir / "meta.json")
    archive_date = str(meta.get("date") or today)
    kind = str(_read_json(run_dir / "run.json").get("kind") or "unknown")
    if force:
        return True, f"强制归档（{kind} 轮次）"
    if kind == "official":
        return True, "official 轮次"
    if (ROOT / "data" / "daily" / archive_date / "sent.json").exists():
        return False, (
            f"{archive_date} 已有正式归档，而本轮是 {kind} 轮次"
            f"（确需覆盖请加 --archive）"
        )
    return True, f"当天尚无归档，写入 {archive_date}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="auto", choices=["auto", "boot", "schedule", "manual"])
    ap.add_argument("--force", action="store_true", help="忽略单日锁")
    ap.add_argument("--email-only", action="store_true", help="不跑管线，用今天最新一版发信")
    ap.add_argument("--no-email", action="store_true", help="只出报，邮件写到 sandbox 预览")
    ap.add_argument("--skip-collect", action="store_true", help="复用现有采集数据")
    ap.add_argument("--archive", action="store_true", help="强制归档，覆盖当天已有归档")
    ap.add_argument("--no-archive", action="store_true", help="只发信，不归档（试发用）")
    ap.add_argument("--allow-test", action="store_true",
                    help="允许从试跑轮次（run.json kind=test）发信；默认拒绝，避免把试跑稿发到邮箱")
    args = ap.parse_args()

    os.environ["RADAR_TRIGGER"] = args.trigger
    cfg = load_sources()
    auto_cfg = cfg.get("automation", {}) or {}
    email_cfg = cfg.get("delivery", {}).get("email", {}) or {}
    if not email_cfg.get("enabled", True):
        log("delivery.email.enabled = false，本轮按 --no-email 处理")
        args.no_email = True
    today = today_str()

    lock = load_lock()
    prev_lock = lock            # 预览/缺配置时用来还原，避免误占当天额度
    if not args.email_only:
        ok, why = decide(lock, today, args.force)
        log(f"锁判定：{why}")
        if not ok:
            print(f"跳过：{why}")
            return 0
        lock = Lock(
            date=today,
            status="running",
            trigger=args.trigger,
            attempts=(lock.attempts + 1) if lock.date == today else 1,
            started_at=now_iso(),
        )
        save_lock(lock)

    edge_proc: subprocess.Popen | None = None
    try:
        if args.email_only:
            run_dir = digest_mod.latest_run_for_date(RUNS_DIR, today)
            if not run_dir:
                raise RuntimeError(f"{today} 还没有任何运行目录，无法只发邮件")
            log(f"复用运行目录：{run_dir.name}")
        else:
            if auto_cfg.get("ensure_edge", True) and not args.skip_collect:
                edge_proc = ensure_edge(bool(auto_cfg.get("edge_headless", False)))

            if not args.skip_collect:
                # 热点种子：抓聚合榜 → 模型筛 → 生成「热种」查询词。
                # 放在生成计划之前，plan_queries 会读它。
                # 失败不阻塞：第三方站或模型挂了，顶多少几个查询。
                try:
                    run_step("抓取热点种子", ["scripts/collect_seeds.py"],
                             int(auto_cfg.get("seed_timeout_min", 5)) * 60)
                except Exception as exc:  # noqa: BLE001
                    log(f"⚠ 热点种子跳过（{exc}），继续用原有查询")

                run_step("生成检索计划", ["scripts/plan_queries.py"],
                         int(auto_cfg.get("plan_timeout_min", 5)) * 60)

                collect_args = ["scripts/collect_douyin.py"]
                if should_resume(today):
                    collect_args.append("--resume")
                    log("今天已有部分采集结果，改为续采（--resume），不从头再来")

                # 采集中断有两种：
                #   1) 9222 不通 —— Edge 没了，或断网时起不到可连状态（11-07 09:47）
                #   2) 9222 仍通 —— 浏览器活着，但页签/CDP 会话被关掉（11-08 三次都是这种）
                # 两种都能靠「换新进程 + --resume」恢复，所以统一重试；
                # 重试次数与等待时间可配，预算按整个采集阶段算，不会无限拖。
                budget = int(auto_cfg.get("collect_timeout_min", 90)) * 60
                max_tries = max(1, int(auto_cfg.get("collect_retries", 3)))
                wait_s = max(0, int(auto_cfg.get("collect_retry_wait_s", 60)))
                deadline = time.time() + budget
                tries = 0
                while True:
                    tries += 1
                    label = "采集抖音" if tries == 1 else f"采集抖音（续采 {tries}）"
                    remain = int(max(60, deadline - time.time()))
                    try:
                        run_step(label, collect_args, remain)
                        # 覆盖度不足不算「采完了」：间歇性软拦截会让进程正常退出，
                        # 但只覆盖 3~4 个查询。这里再续采，直到达标或预算用完。
                        ok_n, planned = douyin_coverage()
                        ratio = (ok_n / planned) if planned else 1.0
                        min_cov = float(auto_cfg.get("min_coverage", 0.7))
                        if ratio < min_cov and tries < max_tries and time.time() < deadline:
                            cov_wait = int(auto_cfg.get("coverage_wait_s", 240))
                            log(f"覆盖度 {ok_n}/{planned} 低于 {min_cov:.0%}，"
                                f"{cov_wait}s 后续采（第 {tries}/{max_tries} 次）")
                            time.sleep(cov_wait)
                            collect_args = ["scripts/collect_douyin.py", "--resume"]
                            continue
                        log(f"覆盖度 {ok_n}/{planned}")
                        break
                    except Exception as exc:  # noqa: BLE001
                        if tries >= max_tries or time.time() >= deadline:
                            raise
                        log(f"采集中断（第 {tries}/{max_tries} 次）：{str(exc)[:120]}")
                        if port_ready():
                            log("9222 仍通（疑似页签被关闭），直接续采")
                        else:
                            log("9222 不通（Edge 未就绪或被回收），重启 Edge 后续采")
                            edge_proc = ensure_edge(
                                bool(auto_cfg.get("edge_headless", False))
                            ) or edge_proc
                        log(f"{wait_s}s 后重试")
                        time.sleep(wait_s)
                        collect_args = ["scripts/collect_douyin.py", "--resume"]

            run_step("判定并出报",
                     ["-m", "src.main", "--run", "--prod", "--origin", "official"],
                     int(auto_cfg.get("judge_timeout_min", 10)) * 60)

            run_dir = digest_mod.latest_run(RUNS_DIR)
            if not run_dir:
                raise RuntimeError("没有找到运行产物")

        subject, html, text = build_mail(run_dir, cfg)
        log(f"摘要：{subject}")

        # 试跑不许发信：data/daily 只收「真内容」，邮箱也一样。
        # 注意 must 在 send 之前判断——原实现是「先发信、再决定要不要归档」，
        # 所以只要跑过一次 --email-only，试跑稿就会真的发出去。
        kind = str(_read_json(run_dir / "run.json").get("kind") or "unknown")
        if kind == "test" and not args.allow_test:
            out_dir = run_dir / "email"
            out_dir.mkdir(exist_ok=True)
            (out_dir / "preview.html").write_text(html, encoding="utf-8")
            (out_dir / "preview.txt").write_text(text, encoding="utf-8")
            log(f"本轮是试跑（kind=test），按规则不发信；预览已写到 {out_dir}")
            log("确需发送请显式加 --allow-test")
            if not args.email_only:
                save_lock(prev_lock)
            return 0

        conf = load_email_config(email_cfg)
        if args.no_email or not conf:
            out_dir = run_dir / "email"
            out_dir.mkdir(exist_ok=True)
            (out_dir / "preview.html").write_text(html, encoding="utf-8")
            (out_dir / "preview.txt").write_text(text, encoding="utf-8")
            why = "已指定 --no-email" if args.no_email else "未配置 SMTP_USER / SMTP_PASS"
            log(f"{why}，邮件已写到 {out_dir}")
            # 预览/缺配置不算「今天已跑」：否则填完授权码后当天就再也跑不了
            if not args.email_only:
                save_lock(prev_lock)
                log("单日锁已还原（未实际发信，不占用今天的额度）")
            return 0

        attachments = []
        if conf.attach_markdown:
            md = run_dir / "report.md"
            if md.exists():
                attachments.append(md)

        send_email(conf, subject=subject, html=html, text=text, attachments=attachments)
        log(f"✔ 邮件已发送至 {', '.join(conf.to)}")

        # 只有真正发出去才归档：data/daily/<date>/ 是「我收到过的日报」。
        # 试发（--email-only / 手改规则后重跑）不能覆盖当天那份正式归档。
        do_archive, why = should_archive(
            run_dir, today, force=args.archive, disable=args.no_archive
        )
        if do_archive:
            dest = archive_run(
                run_dir,
                sent_at=now_iso(),
                to=conf.to,
                subject=subject,
                trigger=args.trigger,
                html=html,
                text=text,
            )
            log(f"已归档：{dest}（{why}）")
        else:
            log(f"未归档：{why}")

        if not args.email_only:
            lock.status = "success"
            lock.finished_at = now_iso()
            lock.note = f"邮件已发送；运行目录 {run_dir.name}"
            save_lock(lock)
        return 0

    except Exception as exc:  # noqa: BLE001
        log(f"✘ 失败：{exc}")
        if not args.email_only:
            lock.status = "failed"
            lock.finished_at = now_iso()
            lock.note = str(exc)[:300]
            save_lock(lock)
        notify_failure(str(exc), cfg)
        return 1
    finally:
        # 只结束「本次自己启动」的 Edge；用户原本开着的不要动
        if edge_proc is not None and edge_proc.poll() is None:
            edge_proc.terminate()
            log("已结束本次启动的 Edge")


if __name__ == "__main__":
    raise SystemExit(main())
