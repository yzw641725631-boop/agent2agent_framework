import smtplib
import ssl
from email.message import EmailMessage

from .security import HubError


class Mailer:
    def __init__(self, settings):
        self.settings = settings
        self.messages: list[dict] = []

    def send_code(self, email: str, code: str, smtp_config=None):
        cfg = self.settings
        if cfg.mail_mode == "memory":
            self.messages.append({"email": email, "code": code})
            return
        msg = EmailMessage()
        msg["Subject"] = "Agent Hub 邮箱与设备验证"
        msg["To"] = email
        msg.set_content(f"验证码：{code}\n有效期 {cfg.otp_seconds // 60} 分钟，仅可使用一次。")
        self.deliver(msg, smtp_config)

    def send_test(self, email, smtp_config):
        if self.settings.mail_mode == "memory":
            self.messages.append({"email": email, "code": None})
            return
        msg = EmailMessage()
        msg["Subject"], msg["To"] = "Agent Hub 发信设置测试", email
        msg.set_content("此邮件用于确认 Agent Hub 的发信服务可以正常投递。")
        self.deliver(msg, smtp_config)

    def deliver(self, msg, smtp_config=None):
        cfg = self.settings
        data = smtp_config or {
            "smtp_host": cfg.smtp_host,
            "smtp_port": cfg.smtp_port,
            "smtp_from": cfg.smtp_from,
            "smtp_username": cfg.smtp_username,
            "smtp_password": cfg.smtp_password.get_secret_value(),
            "smtp_transport": "tls" if cfg.smtp_ssl else "starttls" if cfg.smtp_starttls else "local",
        }
        if not data["smtp_host"] or not data["smtp_from"]:
            raise HubError("MAIL_NOT_CONFIGURED", 503)
        msg["From"] = data["smtp_from"]
        try:
            transport = smtplib.SMTP_SSL if data["smtp_transport"] == "tls" else smtplib.SMTP
            options = {"context": ssl.create_default_context()} if data["smtp_transport"] == "tls" else {}
            with transport(data["smtp_host"], data["smtp_port"], timeout=10, **options) as smtp:
                if data["smtp_transport"] == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if data["smtp_username"]:
                    smtp.login(data["smtp_username"], data["smtp_password"])
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise HubError("MAIL_AUTHENTICATION_FAILED", 503) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise HubError("MAIL_SENDER_REJECTED", 503) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise HubError("MAIL_RECIPIENT_REJECTED", 503) from exc
        except smtplib.SMTPDataError as exc:
            raise HubError("MAIL_DELIVERY_REJECTED", 503) from exc
        except (ssl.SSLError, smtplib.SMTPNotSupportedError) as exc:
            raise HubError("MAIL_TLS_FAILED", 503) from exc
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            raise HubError("MAIL_CONNECTION_FAILED", 503) from exc
        except smtplib.SMTPException as exc:
            raise HubError("MAIL_DELIVERY_FAILED", 503) from exc
        except OSError as exc:
            raise HubError("MAIL_CONNECTION_FAILED", 503) from exc
