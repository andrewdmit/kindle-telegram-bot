import mimetypes
import smtplib
import socket
from email.message import EmailMessage
from pathlib import Path

from config import Settings


class EmailSendError(Exception):
    pass


def build_book_message(
    settings: Settings,
    kindle_email: str,
    file_path: Path,
    original_filename: str,
    convert_pdf: bool = True,
) -> EmailMessage:
    """Готовит письмо для Send-to-Kindle.

    Слово «Convert» в теме письма просит Amazon перевести PDF в формат Kindle
    (текст будет перетекать, но вёрстка может пострадать).
    """
    message = EmailMessage()
    is_pdf = Path(original_filename).suffix.lower() == ".pdf"
    message["Subject"] = f"Convert: {original_filename}" if convert_pdf and is_pdf else original_filename
    message["From"] = settings.smtp_from_email
    message["To"] = kindle_email

    mime_type, _ = mimetypes.guess_type(original_filename)
    maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
    message.add_attachment(
        file_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=original_filename,
    )
    return message


def deliver(settings: Settings, message: EmailMessage) -> None:
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise EmailSendError("SMTP отклонил логин или пароль") from error
    except smtplib.SMTPRecipientsRefused as error:
        raise EmailSendError("Почтовый сервер отклонил адрес Kindle") from error
    except smtplib.SMTPException as error:
        raise EmailSendError(f"Ошибка SMTP: {error}") from error
    except (socket.timeout, OSError) as error:
        raise EmailSendError(f"Нет связи с SMTP-сервером: {error}") from error


def send_book_to_kindle(
    settings: Settings,
    kindle_email: str,
    file_path: Path,
    original_filename: str,
    convert_pdf: bool = True,
) -> None:
    deliver(
        settings,
        build_book_message(settings, kindle_email, file_path, original_filename, convert_pdf),
    )
