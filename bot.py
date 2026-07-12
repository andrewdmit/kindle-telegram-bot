#!/usr/bin/env python3
import logging
import re
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ALLOWED_EXTENSIONS, KINDLE_EMAIL_PATTERN, Settings, load_settings
from database import Database
from email_sender import EmailSendError, send_book_to_kindle

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WAITING_EMAIL = 1

settings: Settings
database: Database


def is_kindle_email(email: str) -> bool:
    return bool(re.match(KINDLE_EMAIL_PATTERN, email.strip(), re.IGNORECASE))


def is_allowed_book(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    registered = database.get_user(user.id)
    if registered:
        await update.message.reply_text(
            "Привет! Вы уже зарегистрированы.\n\n"
            f"Книги отправляются на: {registered.kindle_email}\n\n"
            "Просто пришлите файл книги этому боту.\n"
            "Команды: /status, /register (изменить email), /help"
        )
        return

    await update.message.reply_text(
        "Привет! Я отправляю электронные книги на ваш Kindle по email.\n\n"
        "Сначала нужно зарегистрироваться: /register\n"
        "Справка: /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Как пользоваться ботом:\n\n"
        "1. Зарегистрируйтесь: /register\n"
        "2. Укажите Kindle email (например, name@kindle.com)\n"
        "3. В Amazon добавьте адрес отправителя бота в список одобренных:\n"
        "   Content & Devices → Preferences → Personal Document Settings\n"
        f"   Адрес отправителя: {settings.smtp_from_email}\n"
        "4. Отправьте боту файл книги как документ\n\n"
        "Поддерживаемые форматы: PDF, EPUB, MOBI, AZW, TXT, DOC, DOCX и др.\n"
        "Максимальный размер файла: 20 МБ."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    registered = database.get_user(user.id)
    if not registered:
        await update.message.reply_text(
            "Вы ещё не зарегистрированы. Используйте /register"
        )
        return

    await update.message.reply_text(
        f"Ваш Kindle email: {registered.kindle_email}\n"
        f"Отправитель бота: {settings.smtp_from_email}"
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END

    await update.message.reply_text(
        "Введите ваш Kindle email.\n"
        "Обычно он выглядит так: name@kindle.com или name@free.kindle.com\n\n"
        "Найти его можно в Amazon: Content & Devices → ваш Kindle → Send-to-Kindle Email.\n\n"
        "Отмена: /cancel"
    )
    return WAITING_EMAIL


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user is None or update.message is None or update.message.text is None:
        return ConversationHandler.END

    email = update.message.text.strip()
    if not is_kindle_email(email):
        await update.message.reply_text(
            "Это не похоже на Kindle email. Пример: name@kindle.com\n"
            "Попробуйте ещё раз или /cancel"
        )
        return WAITING_EMAIL

    database.upsert_user(
        telegram_id=user.id,
        kindle_email=email.lower(),
        username=user.username,
        first_name=user.first_name,
    )

    await update.message.reply_text(
        f"Готово! Книги будут отправляться на {email.lower()}.\n\n"
        "Важно: добавьте адрес отправителя в Amazon:\n"
        "Content & Devices → Preferences → Personal Document Settings\n"
        f"Адрес: {settings.smtp_from_email}\n\n"
        "Теперь пришлите файл книги."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END

    await update.message.reply_text("Регистрация отменена.")
    return ConversationHandler.END


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if user is None or message is None or message.document is None:
        return

    registered = database.get_user(user.id)
    if not registered:
        await message.reply_text(
            "Сначала зарегистрируйтесь: /register"
        )
        return

    document = message.document
    filename = document.file_name or "book"
    extension = Path(filename).suffix.lower()

    if not is_allowed_book(filename):
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await message.reply_text(
            f"Формат {extension or '(без расширения)'} не поддерживается.\n"
            f"Допустимые форматы: {allowed}"
        )
        return

    if document.file_size and document.file_size > settings.max_file_size:
        await message.reply_text(
            f"Файл слишком большой ({document.file_size // 1024 // 1024} МБ). "
            f"Максимум: {settings.max_file_size // 1024 // 1024} МБ."
        )
        return

    status_message = await message.reply_text("Получил файл, отправляю на Kindle...")

    try:
        telegram_file = await document.get_file()
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / filename
            await telegram_file.download_to_drive(custom_path=str(local_path))
            send_book_to_kindle(
                settings=settings,
                kindle_email=registered.kindle_email,
                file_path=local_path,
                original_filename=filename,
            )
    except EmailSendError as error:
        logger.exception("SMTP error for user %s", user.id)
        await status_message.edit_text(
            f"Не удалось отправить книгу: {error}\n"
            "Проверьте настройки SMTP на сервере."
        )
        return
    except Exception:
        logger.exception("Unexpected error for user %s", user.id)
        await status_message.edit_text(
            "Произошла ошибка при отправке. Попробуйте позже."
        )
        return

    await status_message.edit_text(
        f"Книга «{filename}» отправлена на {registered.kindle_email}.\n"
        "Она появится в библиотеке Kindle через несколько минут."
    )


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()

    registration = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            WAITING_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_email)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(registration)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return application


def main() -> None:
    global settings, database

    settings = load_settings()
    database = Database(settings.database_path)

    application = build_application()
    logger.info("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
