"""Synchronous SMTP transport, called from the notification worker thread."""

import smtplib
import ssl
from email.message import EmailMessage

from .config import EmailNotifyConfig


def send_email(config: EmailNotifyConfig, subject: str, text: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender or config.smtp_user or "wechat-native@localhost"
    message["To"] = config.recipients
    message.set_content(text)
    password = config.load_password() if config.smtp_user else ""
    transport = smtplib.SMTP_SSL if config.smtp_security == "ssl" else smtplib.SMTP
    with transport(config.smtp_host, config.smtp_port, timeout=15) as server:
        if config.smtp_security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if config.smtp_user:
            server.login(config.smtp_user, password)
        server.send_message(message)
