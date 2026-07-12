from __future__ import annotations

import smtplib
import sys
from email.message import EmailMessage

from ..core.config import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USE_SSL, SMTP_USE_TLS, SMTP_USERNAME


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def send_verification_email(to_email: str, username: str, verification_url: str) -> bool:
    if not smtp_configured():
        print(f"[webtool mail] SMTP is not configured. Verification link for {username}: {verification_url}", file=sys.stderr)
        return False

    message = EmailMessage()
    message["Subject"] = "기렌 한글화 웹툴 이메일 인증"
    message["From"] = SMTP_FROM
    message["To"] = to_email
    message.set_content(
        "\n".join(
            [
                f"{username} 계정의 이메일 인증을 완료하려면 아래 링크를 여세요.",
                "",
                verification_url,
                "",
                "요청하지 않은 가입이라면 이 메일을 무시해도 됩니다.",
            ]
        )
    )

    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)
    return True
