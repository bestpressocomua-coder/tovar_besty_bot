"""Відправка листів через SMTP (Gmail) з вкладеннями xlsx."""
import os
import ssl
import smtplib
from email.message import EmailMessage

XLSX_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_message(subject, body, from_addr, to_addr, attachments):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments:
        with open(path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype=XLSX_SUBTYPE,
                           filename=os.path.basename(path))
    return msg


def send_email(subject, body, to_addr, attachments, cfg):
    """Кидає виняток, якщо відправка не вдалася (обробляється у боті — резерв Telegram)."""
    msg = build_message(subject, body, cfg.EMAIL_FROM, to_addr, attachments)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
        s.send_message(msg)
