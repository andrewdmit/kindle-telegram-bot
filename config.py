import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

KINDLE_EMAIL_PATTERN = r"^.+@(.+\.)?kindle\..+$"

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".mobi",
    ".azw",
    ".azw3",
    ".prc",
    ".epub",
    ".txt",
    ".rtf",
    ".doc",
    ".docx",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".zip",
}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from_email: str
    smtp_use_tls: bool
    database_path: Path
    max_file_size: int


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Переменная окружения {name} не задана")
    return value


def load_settings() -> Settings:
    database_path = Path(
        os.getenv("DATABASE_PATH", "/opt/kindle-telegram-bot/data/users.db")
    )

    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        smtp_host=_require("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=_require("SMTP_USER"),
        smtp_password=_require("SMTP_PASSWORD"),
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL", os.getenv("SMTP_USER", "")).strip()
        or _require("SMTP_USER"),
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"},
        database_path=database_path,
        max_file_size=int(os.getenv("MAX_FILE_SIZE", str(20 * 1024 * 1024))),
    )
