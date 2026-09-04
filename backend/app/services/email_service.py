import html
import logging
from email.utils import formataddr, parseaddr

from aiosmtplib import SMTP
from email.message import EmailMessage

from app.core.config import settings


logger = logging.getLogger(__name__)


class EmailService:
    async def send_code(self, *, recipient: str, code: str, purpose: str) -> None:
        is_verification = purpose == "email_verification"
        subject = "AVEMOD — Подтверждение электронной почты" if is_verification else "AVEMOD — Восстановление пароля"
        title = "Подтвердите электронную почту" if is_verification else "Восстановите пароль"
        description = (
            "Введите этот код в приложении, чтобы завершить регистрацию."
            if is_verification
            else "Введите этот код в приложении, чтобы создать новый пароль."
        )
        plain_title = "Ваш код подтверждения" if is_verification else "Ваш код восстановления пароля"
        safe_code = html.escape(str(code))
        _, sender_address = parseaddr(settings.email_from)
        sender_address = sender_address or settings.email_from

        message = EmailMessage()
        message["From"] = formataddr(("AVEMOD", sender_address))
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(
            f"{plain_title}: {code}\n\n"
            "Код действителен 15 минут. Если вы не запрашивали это письмо, просто проигнорируйте его."
        )
        message.add_alternative(
            f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light">
    <title>{html.escape(subject)}</title>
  </head>
  <body style="margin:0; padding:0; background:#f3f6fb; color:#13213a; font-family:Arial, Helvetica, sans-serif;">
    <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">
      {html.escape(description)} Код действует 15 минут.
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f3f6fb;">
      <tr>
        <td align="center" style="padding:40px 16px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:580px;">
            <tr>
              <td style="padding:0 0 20px 4px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td width="42" height="42" align="center" valign="middle" style="width:42px; height:42px; border-radius:13px; background:#3268e8; color:#ffffff; font-size:21px; font-weight:700; box-shadow:0 8px 18px rgba(50,104,232,.22);">A</td>
                    <td style="padding-left:11px; color:#13213a; font-size:18px; font-weight:700; letter-spacing:-.2px;">AVEMOD</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="background:#ffffff; border:1px solid #e2e8f2; border-radius:22px; box-shadow:0 14px 36px rgba(27,52,94,.08);">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="padding:38px 40px 34px;">
                      <div style="display:inline-block; padding:7px 11px; border-radius:999px; background:#edf3ff; color:#3268e8; font-size:11px; font-weight:700; letter-spacing:1.2px; text-transform:uppercase;">AVEMOD</div>
                      <h1 style="margin:20px 0 10px; color:#13213a; font-size:28px; line-height:1.2; letter-spacing:-.6px;">{html.escape(title)}</h1>
                      <p style="margin:0; color:#71809a; font-size:15px; line-height:1.6;">{html.escape(description)}</p>

                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px;">
                        <tr>
                          <td align="center" style="padding:24px 16px; border:1px solid #d8e3fb; border-radius:16px; background:#f5f8ff;">
                            <div style="margin-bottom:9px; color:#8190aa; font-size:11px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase;">Одноразовый код</div>
                            <div style="color:#245bd6; font-size:36px; line-height:1.1; font-weight:700; letter-spacing:8px;">{safe_code}</div>
                          </td>
                        </tr>
                      </table>

                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:22px;">
                        <tr>
                          <td width="26" valign="top" style="width:26px; padding-top:1px; color:#3268e8; font-size:17px;">●</td>
                          <td style="color:#71809a; font-size:14px; line-height:1.55;">Код действителен в течение <strong style="color:#40516d;">15 минут</strong>.</td>
                        </tr>
                        <tr>
                          <td width="26" valign="top" style="width:26px; padding-top:1px; color:#3268e8; font-size:17px;">●</td>
                          <td style="color:#71809a; font-size:14px; line-height:1.55;">Никому не сообщайте этот код.</td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:18px 40px 22px; border-top:1px solid #edf1f7; background:#fbfcfe; border-radius:0 0 22px 22px; color:#9aa7ba; font-size:12px; line-height:1.55;">
                      Если вы не запрашивали это письмо, просто проигнорируйте его. Ваш аккаунт остаётся в безопасности.
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:20px 10px 0; color:#9aa7ba; font-size:12px; line-height:1.5;">Письмо отправлено автоматически сервисом AVEMOD.</td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>""",
            subtype="html",
        )

        if not settings.smtp_host:
            if settings.app_env == "production":
                raise RuntimeError("SMTP_HOST must be configured in production")
            logger.warning("Email delivery is not configured. %s code for %s: %s", purpose, recipient, code)
            return

        smtp = SMTP(hostname=settings.smtp_host, port=settings.smtp_port, start_tls=True)
        await smtp.connect()
        try:
            if settings.smtp_user:
                await smtp.login(settings.smtp_user, settings.smtp_password)
            await smtp.send_message(message)
        finally:
            await smtp.quit()
