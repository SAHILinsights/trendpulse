"""Optional alerting: generic JSON webhook and/or SMTP email.

Both channels are disabled unless their environment variables are set.
Alert delivery failures are logged and swallowed — an alerting outage must
never fail the pipeline itself.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import requests

from trendpulse.logging_utils import get_logger
from trendpulse.settings import Secrets

log = get_logger("alerts")


def send_alert(secrets: Secrets, subject: str, body: str, session: requests.Session | None = None) -> list[str]:
    sent: list[str] = []
    if secrets.alert_webhook_url:
        try:
            # "text" works for Slack incoming webhooks; "content" for Discord.
            resp = (session or requests).post(secrets.alert_webhook_url,
                                              json={"text": f"*{subject}*\n{body}", "content": f"**{subject}**\n{body}"},
                                              timeout=10)
            if resp.status_code < 300:
                sent.append("webhook")
            else:
                log.warning("webhook alert returned HTTP %s", resp.status_code)
        except requests.RequestException as exc:
            log.warning("webhook alert failed: %s", exc)
    if secrets.smtp_host and secrets.smtp_user and secrets.smtp_password and secrets.alert_email_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[TrendPulse] {subject}"
            msg["From"] = secrets.smtp_user
            msg["To"] = secrets.alert_email_to
            msg.set_content(body)
            with smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(secrets.smtp_user, secrets.smtp_password)
                smtp.send_message(msg)
            sent.append("email")
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("email alert failed: %s", exc)
    return sent
