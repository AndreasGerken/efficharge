"""Sending mail through the SMTP server of a mailbox (e.g. one at the domain's mail provider).

Only encrypted: port 465 speaks TLS from the start, every other port must offer STARTTLS.
Off unless ``EFFICHARGE_SMTP_HOST`` is set.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from efficharge.config import Config

TIMEOUT_S = 30


class Mailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender = sender

    @classmethod
    def from_config(cls, config: Config) -> Mailer | None:
        if not config.smtp_host:
            return None
        sender = config.mail_from or config.smtp_user
        if not sender:
            raise RuntimeError("EFFICHARGE_SMTP_HOST needs EFFICHARGE_MAIL_FROM or _SMTP_USER")
        c = config
        return cls(c.smtp_host, c.smtp_port, c.smtp_user, c.smtp_password, sender)

    def message(self, to: list[str], subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        msg["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or None)
        msg.set_content(body)
        return msg

    def _send(self, msg: EmailMessage) -> None:
        tls = ssl.create_default_context()
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT_S, context=tls)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_S)
        with server:
            if self.port != 465:
                server.starttls(context=tls)  # raises if the server can't: never in plain text
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)

    async def send(self, to: list[str], subject: str, body: str) -> None:
        await asyncio.to_thread(self._send, self.message(to, subject, body))
