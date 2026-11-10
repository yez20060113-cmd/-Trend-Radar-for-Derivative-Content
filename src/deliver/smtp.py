"""SMTP 发送（QQ 邮箱实测参数：smtp.qq.com:465 + SSL）。

为什么不用 WorkBuddy 的邮箱 MCP：
定时任务拉起的是**独立 Python 进程**，跑在系统里，而 MCP 工具只存在于
WorkBuddy 会话内。无人值守的 9:00 投递，只有 SMTP 这条路能走通。

QQ 邮箱两个坑：
1. 密码必须是 **16 位授权码**，不是登录密码（535 认证失败多半是这里）。
2. `From` 必须等于登录账号，否则 553 拒发——所以这里固定用 SMTP_USER。
"""

from __future__ import annotations

import smtplib
import ssl
import time
from dataclasses import dataclass
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from ..config import env


@dataclass
class EmailConfig:
    host: str
    port: int
    security: str          # ssl / starttls / none
    user: str
    password: str
    to: list[str]
    from_name: str = "追星二创雷达"
    timeout: float = 20.0
    attach_markdown: bool = True

    @property
    def sender(self) -> str:
        return formataddr((str(Header(self.from_name, "utf-8")), self.user))


def load_config(cfg: dict[str, Any] | None = None) -> EmailConfig | None:
    """从 sources.yaml 的 delivery.email 段 + .env 组装配置；缺关键项返回 None。"""
    cfg = cfg or {}
    user = env("SMTP_USER")
    password = env("SMTP_PASS")
    to_raw = env("SMTP_TO") or user
    if not (user and password):
        return None
    to = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
    if not to:
        return None
    return EmailConfig(
        host=str(cfg.get("smtp_host", "smtp.qq.com")),
        port=int(cfg.get("smtp_port", 465)),
        security=str(cfg.get("security", "ssl")).lower(),
        user=user,
        password=password,
        to=to,
        from_name=str(cfg.get("from_name", "追星二创雷达")),
        timeout=float(cfg.get("timeout_seconds", 20)),
        attach_markdown=bool(cfg.get("attach_markdown", True)),
    )


def send(
    conf: EmailConfig,
    *,
    subject: str,
    html: str,
    text: str,
    attachments: list[Path] | None = None,
    retries: int = 1,
) -> None:
    """发送一封 multipart 邮件（纯文本 + HTML + 可选附件），失败重试一次。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = conf.sender
    msg["To"] = ", ".join(conf.to)
    # 纯文本在前、HTML 在后：客户端按能力选择，转发时也保留可读版本
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    for path in attachments or []:
        if not path.exists():
            continue
        data = path.read_bytes()
        msg.add_attachment(
            data,
            maintype="text",
            subtype="markdown",
            filename=path.name,
        )

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if conf.security == "ssl":
                with smtplib.SMTP_SSL(
                    conf.host, conf.port, timeout=conf.timeout,
                    context=ssl.create_default_context(),
                ) as smtp:
                    smtp.login(conf.user, conf.password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(conf.host, conf.port, timeout=conf.timeout) as smtp:
                    smtp.ehlo()
                    if conf.security == "starttls":
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                    smtp.login(conf.user, conf.password)
                    smtp.send_message(msg)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(f"邮件发送失败：{last}") from last
