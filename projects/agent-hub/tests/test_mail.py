import smtplib
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_hub.mail import Mailer
from agent_hub.security import HubError


@pytest.mark.parametrize(
    "stage,failure,expected",
    [
        ("connect", TimeoutError(), "MAIL_CONNECTION_FAILED"),
        ("connect", ssl.SSLError("certificate failed"), "MAIL_TLS_FAILED"),
        ("login", smtplib.SMTPException("unexpected server response"), "MAIL_DELIVERY_FAILED"),
        ("login", smtplib.SMTPAuthenticationError(535, b"credential rejected"), "MAIL_AUTHENTICATION_FAILED"),
        (
            "send_message",
            smtplib.SMTPSenderRefused(550, b"rejected", "sender@example.org"),
            "MAIL_SENDER_REJECTED",
        ),
        (
            "send_message",
            smtplib.SMTPRecipientsRefused({"recipient@example.org": (550, b"rejected")}),
            "MAIL_RECIPIENT_REJECTED",
        ),
        ("send_message", smtplib.SMTPDataError(550, b"quota"), "MAIL_DELIVERY_REJECTED"),
    ],
)
def test_mail_failures_are_distinct_without_returning_credentials(monkeypatch, stage, failure, expected):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    factory = MagicMock(return_value=smtp)
    if stage == "connect":
        factory.side_effect = failure
    else:
        getattr(smtp, stage).side_effect = failure
    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    config = {
        "smtp_host": "smtp.example.org",
        "smtp_port": 465,
        "smtp_transport": "tls",
        "smtp_from": "sender@example.org",
        "smtp_username": "sender@example.org",
        "smtp_password": "test-credential-that-must-not-appear",
    }
    mailer = Mailer(SimpleNamespace(mail_mode="smtp", otp_seconds=600))
    with pytest.raises(HubError) as caught:
        mailer.send_code("recipient@example.org", "12345678", config)
    assert caught.value.code == expected
    assert caught.value.details == {}
    assert config["smtp_password"] not in str(caught.value)
    if stage in {"connect", "login"}:
        smtp.send_message.assert_not_called()
