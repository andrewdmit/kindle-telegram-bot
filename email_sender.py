import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from config import Settings


class EmailSendError(Exception):
    pass


def send_book_to_kindle(
    settings: Settings,
    kindle_email: str,
    file_path: Path,
    original_filename: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = f"Convert: {original_filename}"
    message["From"] = settings.smtp_from_email
    message["To"] = kindle_email

    mime_type, _ = mimetypes.guess_type(original_filename)
    maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
    file_data = file_path.read_bytes()

    message.add_attachment(
        file_data,
        maintype=maintype,
        subtype=subtype,
        filename=original_filename,
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPException as error:
        raise EmailSendError(f"Ошибка SMTP: {error}") from error
