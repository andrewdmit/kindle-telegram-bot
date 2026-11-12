#!/usr/bin/env python3
"""Kindle Telegram Bot: принимает книги в Telegram и отправляет их на Kindle по email.

Интерфейс построен на HTML-виджетах Bot API 10.4 (регистрация, приём файлов,
прогресс отправки). Если виджеты недоступны или выключены (WIDGETS_ENABLED=false),
бот работает обычными текстовыми сообщениями.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    BaseHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ALLOWED_EXTENSIONS, KINDLE_EMAIL_PATTERN, Settings, load_settings
from database import (
    STAGE_DONE,
    STAGE_DOWNLOAD,
    STAGE_FAILED,
    STAGE_PREPARE,
    STAGE_QUEUED,
    STAGE_SMTP,
    Batch,
    Database,
    Inbox,
)
from email_sender import EmailSendError, build_book_message, deliver, send_book_to_kindle
from widget_api import (
    WIDGET_UPDATE_TYPES,
    ThrottledWidget,
    WidgetAPI,
    WidgetQuery,
    extract_widget_query,
    fit_state,
    is_widget_update,
    load_widget_html,
    shorten,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Новые файлы добавляются в открытый список приёма, если он обновлялся недавно.
# Иначе создаётся новый список внизу чата, а старый переносится в него.
INBOX_REUSE_SECONDS = 300

TEST_DOCUMENT = (
    "Kindle Telegram Bot\n\n"
    "Это тестовый документ. Если вы его видите, бот настроен правильно.\n"
    "Можно удалить его из библиотеки.\n"
)


def is_kindle_email(email: str) -> bool:
    return bool(re.match(KINDLE_EMAIL_PATTERN, email.strip(), re.IGNORECASE))


def is_allowed_book(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def short_error(error: BaseException) -> str:
    if isinstance(error, EmailSendError):
        return shorten(str(error), 120)
    if isinstance(error, TelegramError):
        return shorten(f"Telegram: {error}", 120)
    return "Внутренняя ошибка, подробности в логе"


class WidgetQueryHandler(BaseHandler[Update, ContextTypes.DEFAULT_TYPE, None]):
    """Ловит апдейты widget_query, которых PTB пока не знает."""

    def check_update(self, update: object) -> bool:
        return is_widget_update(update)


class KindleBot:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.db = database
        self.now = now
        self.html = {name: load_widget_html(name) for name in ("registration", "inbox", "progress")}
        self._rev = 0
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- служебное ---

    def rev(self) -> int:
        """Монотонная ревизия состояния: виджет игнорирует состояния старше уже показанного."""
        self._rev = max(self._rev + 1, int(self.now() * 1000))
        return self._rev

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Фоновая задача упала", exc_info=task.exception())

    async def wait_background(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    # --- состояния виджетов ---

    def registration_state(self, user_id: int) -> dict[str, Any]:
        user = self.db.get_user(user_id)
        reg = self.db.get_registration(user_id)
        if user is None or reg.awaiting_email:
            step = 1
        elif not user.sender_confirmed:
            step = 2
        else:
            step = 3
        state: dict[str, Any] = {
            "r": self.rev(),
            "s": step,
            "e": user.kindle_email if user else None,
            "f": self.settings.smtp_from_email,
        }
        if reg.test_status:
            state["t"] = reg.test_status
        if reg.test_error:
            state["te"] = shorten(reg.test_error, 160)
        return state

    def inbox_state(self, inbox: Inbox) -> dict[str, Any]:
        user = self.db.get_user(inbox.telegram_id)
        files = self.db.list_inbox_files(inbox.id)
        rev = self.rev()

        def build(name_limit: int, max_items: int) -> dict[str, Any]:
            shown = files[:max_items]
            state: dict[str, Any] = {
                "r": rev,
                "id": inbox.id,
                "st": inbox.status,
                "to": user.kindle_email if user else "",
                "cv": int(inbox.convert_pdf),
                "f": [
                    [f.id, shorten(f.file_name, name_limit), f.file_size, extension_of(f.file_name)[:5], f.error]
                    for f in shown
                ],
            }
            if len(files) > len(shown):
                state["x"] = len(files) - len(shown)
            return state

        return fit_state(build, len(files))

    def progress_state(self, batch: Batch) -> dict[str, Any]:
        items = self.db.list_batch_items(batch.id)
        rev = self.rev()

        def build(name_limit: int, max_items: int) -> dict[str, Any]:
            shown, hidden = items[:max_items], items[max_items:]
            state: dict[str, Any] = {
                "r": rev,
                "id": batch.id,
                "to": batch.kindle_email,
                "t0": int(batch.started_at),
                "f": [
                    [shorten(i.file_name, name_limit), i.stage, shorten(i.error, 90) if i.error else ""]
                    for i in shown
                ],
            }
            if batch.finished_at:
                state["t1"] = int(batch.finished_at)
            if hidden:
                state["x"] = len(hidden)
                state["xo"] = sum(1 for i in hidden if i.stage == STAGE_DONE)
                state["xe"] = sum(1 for i in hidden if i.stage == STAGE_FAILED)
            return state

        return fit_state(build, len(items))

    # --- регистрация ---

    async def send_registration_widget(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        if not self.settings.widgets_enabled:
            return False
        try:
            message_id = await WidgetAPI(bot).send(
                chat_id,
                self.html["registration"],
                self.registration_state(user_id),
                caption="Регистрация в Kindle-боте",
            )
        except TelegramError:
            logger.exception("Не удалось отправить виджет регистрации")
            return False
        self.db.update_registration(user_id, message_id=message_id)
        return True

    async def refresh_registration_widget(self, bot: Bot, user_id: int) -> None:
        reg = self.db.get_registration(user_id)
        if reg.message_id and self.settings.widgets_enabled:
            try:
                await WidgetAPI(bot).edit(user_id, reg.message_id, self.registration_state(user_id))
            except TelegramError:
                logger.exception("Не удалось обновить виджет регистрации")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None:
            return

        registered = self.db.get_user(user.id)
        if registered:
            await message.reply_text(
                "Привет! Вы уже зарегистрированы.\n\n"
                f"Книги отправляются на: {registered.kindle_email}\n\n"
                "Просто пришлите файл книги этому боту.\n"
                "Команды: /status, /register (изменить email), /help"
            )
            return

        self.db.update_registration(user.id, awaiting_email=True)
        await message.reply_text(
            "Привет! Я отправляю электронные книги на ваш Kindle по email.\n"
            "Начнём с регистрации."
        )
        if not await self.send_registration_widget(context.bot, message.chat_id, user.id):
            await message.reply_text(self.registration_prompt_text())

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        await update.message.reply_text(
            "Как пользоваться ботом:\n\n"
            "1. Зарегистрируйтесь: /register\n"
            "2. Укажите Kindle email (например, name@kindle.com)\n"
            "3. В Amazon добавьте адрес отправителя бота в список одобренных:\n"
            "   Content & Devices → Preferences → Personal Document Settings\n"
            f"   Адрес отправителя: {self.settings.smtp_from_email}\n"
            "4. Пришлите боту файлы книг как документы, можно несколько сразу,\n"
            "   и нажмите «Отправить на Kindle» в карточке приёма\n\n"
            "Поддерживаемые форматы: PDF, EPUB, MOBI, AZW, TXT, DOC, DOCX и др.\n"
            f"Максимальный размер файла: {self.settings.max_file_size // 1024 // 1024} МБ."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None:
            return
        registered = self.db.get_user(user.id)
        if not registered:
            await message.reply_text("Вы ещё не зарегистрированы. Используйте /register")
            return
        if not await self.send_registration_widget(context.bot, message.chat_id, user.id):
            await message.reply_text(
                f"Ваш Kindle email: {registered.kindle_email}\n"
                f"Отправитель бота: {self.settings.smtp_from_email}"
            )

    def registration_prompt_text(self) -> str:
        return (
            "Введите ваш Kindle email.\n"
            "Обычно он выглядит так: name@kindle.com или name@free.kindle.com\n\n"
            "Найти его можно в Amazon: Content & Devices → ваш Kindle → Send-to-Kindle Email.\n\n"
            "Отмена: /cancel"
        )

    async def register(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None:
            return
        self.db.update_registration(user.id, awaiting_email=True, test_status="", test_error="")
        if not await self.send_registration_widget(context.bot, message.chat_id, user.id):
            await message.reply_text(self.registration_prompt_text())

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None:
            return
        reg = self.db.get_registration(user.id)
        if not reg.awaiting_email:
            await message.reply_text("Отменять нечего.")
            return
        self.db.update_registration(user.id, awaiting_email=False)
        await self.refresh_registration_widget(context.bot, user.id)
        await message.reply_text("Регистрация отменена.")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None or message.text is None:
            return

        text = message.text.strip()
        registered = self.db.get_user(user.id)
        reg = self.db.get_registration(user.id)
        wants_email = reg.awaiting_email or (registered is None and "@" in text)

        if not wants_email:
            if registered is None:
                await message.reply_text("Сначала зарегистрируйтесь: /register")
            else:
                await message.reply_text(
                    "Пришлите файл книги как документ. Сменить Kindle email: /register"
                )
            return

        if not is_kindle_email(text):
            await message.reply_text(
                "Это не похоже на Kindle email. Пример: name@kindle.com\n"
                "Попробуйте ещё раз или /cancel"
            )
            return

        email = text.lower()
        self.db.upsert_user(
            telegram_id=user.id,
            kindle_email=email,
            username=user.username,
            first_name=user.first_name,
        )
        reg = self.db.update_registration(user.id, awaiting_email=False, test_status="", test_error="")

        if reg.message_id and self.settings.widgets_enabled:
            await self.refresh_registration_widget(context.bot, user.id)
            await message.reply_text(
                f"Сохранил: {email} ✓\nСледующий шаг — в карточке регистрации.",
                reply_to_message_id=reg.message_id,
                allow_sending_without_reply=True,
            )
            return

        await message.reply_text(
            f"Готово! Книги будут отправляться на {email}.\n\n"
            "Важно: добавьте адрес отправителя в Amazon:\n"
            "Content & Devices → Preferences → Personal Document Settings\n"
            f"Адрес: {self.settings.smtp_from_email}\n\n"
            "Теперь пришлите файл книги."
        )

    async def run_test_send(self, bot: Bot, user_id: int) -> None:
        user = self.db.get_user(user_id)
        if user is None:
            return
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "kindle-bot-test.txt"
                path.write_text(TEST_DOCUMENT, encoding="utf-8")
                await asyncio.to_thread(
                    send_book_to_kindle, self.settings, user.kindle_email, path, "Kindle Bot test.txt"
                )
        except Exception as error:  # noqa: BLE001 — показываем пользователю любую причину
            logger.exception("Тестовая отправка для %s не удалась", user_id)
            self.db.update_registration(user_id, test_status="err", test_error=short_error(error))
        else:
            self.db.update_registration(user_id, test_status="ok", test_error="")
        await self.refresh_registration_widget(bot, user_id)

    async def on_registration_query(self, bot: Bot, query: WidgetQuery, action: str) -> None:
        api = WidgetAPI(bot)
        user = self.db.get_user(query.user_id)

        if action == "edit":
            self.db.update_registration(query.user_id, awaiting_email=True, test_status="", test_error="")
        elif action == "ok" and user:
            self.db.set_sender_confirmed(query.user_id, True)
        elif action == "test" and user and user.sender_confirmed:
            reg = self.db.get_registration(query.user_id)
            if reg.test_status != "run":
                self.db.update_registration(query.user_id, test_status="run", test_error="")
                self.spawn(self.run_test_send(bot, query.user_id))

        await api.answer(query.id, self.registration_state(query.user_id))
        # chat_data тоже обновляем: виджет может быть пересоздан и прочитает его заново
        await self.refresh_registration_widget(bot, query.user_id)

    # --- приём файлов ---

    def validate_document(self, filename: str, size: int) -> str:
        if not is_allowed_book(filename):
            return "fmt"
        if size and size > self.settings.max_file_size:
            return "big"
        return ""

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, message = update.effective_user, update.message
        if user is None or message is None or message.document is None:
            return

        registered = self.db.get_user(user.id)
        if not registered:
            self.db.update_registration(user.id, awaiting_email=True)
            await message.reply_text("Сначала зарегистрируйтесь — это займёт минуту.")
            if not await self.send_registration_widget(context.bot, message.chat_id, user.id):
                await message.reply_text(self.registration_prompt_text())
            return

        if not self.settings.widgets_enabled:
            await self.legacy_send_document(update)
            return

        document = message.document
        filename = document.file_name or "book"
        size = document.file_size or 0
        error = self.validate_document(filename, size)
        now = self.now()
        api = WidgetAPI(context.bot)

        old = self.db.get_open_inbox(user.id)
        if old and old.message_id and now - old.updated_at <= INBOX_REUSE_SECONDS:
            self.db.add_inbox_file(old.id, document.file_id, filename, size, error, now)
            old = self.db.get_inbox(old.id)
            assert old is not None and old.message_id is not None
            if await api.edit(message.chat_id, old.message_id, self.inbox_state(old)):
                return
            # сообщение со списком удалено: файлы переедут в новый список ниже
            old = Inbox(old.id, old.telegram_id, None, old.status, old.convert_pdf, old.updated_at)
        new_inbox = self.db.create_inbox(user.id, now, convert_pdf=old.convert_pdf if old else True)
        if old:
            # старый список устарел (давно не обновлялся или удалён): переносим его файлы вниз
            self.db.move_inbox_files(old.id, new_inbox.id)
            self.db.update_inbox(old.id, now, status="moved")
            if old.message_id:
                moved = self.db.get_inbox(old.id)
                assert moved is not None
                await api.edit(message.chat_id, old.message_id, self.inbox_state(moved))
        if not any(f.file_id == document.file_id for f in self.db.list_inbox_files(new_inbox.id)):
            self.db.add_inbox_file(new_inbox.id, document.file_id, filename, size, error, now)

        try:
            message_id = await api.send(
                message.chat_id,
                self.html["inbox"],
                self.inbox_state(new_inbox),
                caption="Приём книг: нажмите «Отправить на Kindle»",
            )
        except TelegramError:
            logger.exception("Не удалось отправить виджет приёма, перехожу на текст")
            self.db.update_inbox(new_inbox.id, now, status="cleared")
            await self.legacy_send_document(update)
            return
        self.db.update_inbox(new_inbox.id, now, message_id=message_id)

    async def on_inbox_query(self, bot: Bot, query: WidgetQuery, action: str, args: list[str]) -> None:
        api = WidgetAPI(bot)
        inbox = self.db.get_inbox(int(args[0])) if args and args[0].isdigit() else None
        if inbox is None or inbox.telegram_id != query.user_id:
            await api.answer(query.id)
            return

        now = self.now()
        batch: Batch | None = None

        if inbox.status == "open":
            if action == "rm" and len(args) > 1 and args[1].isdigit():
                self.db.remove_inbox_file(inbox.id, int(args[1]))
                self.db.update_inbox(inbox.id, now)
            elif action == "cv":
                self.db.update_inbox(inbox.id, now, convert_pdf=not inbox.convert_pdf)
            elif action == "clr":
                self.db.claim_inbox(inbox.id, "cleared", now)
            elif action == "send":
                user = self.db.get_user(query.user_id)
                files = [f for f in self.db.list_inbox_files(inbox.id) if not f.error]
                if user and files and self.db.claim_inbox(inbox.id, "sent", now):
                    batch = self.db.create_batch(
                        user.telegram_id,
                        user.kindle_email,
                        inbox.convert_pdf,
                        [(f.file_id, f.file_name, f.file_size) for f in files],
                        now,
                    )

        inbox = self.db.get_inbox(inbox.id)
        assert inbox is not None
        state = self.inbox_state(inbox)
        await api.answer(query.id, state)
        if inbox.message_id:
            await api.edit(query.user_id, inbox.message_id, state)

        if batch is not None:
            await self.start_batch(bot, batch, reply_to=inbox.message_id)

    # --- отправка и прогресс ---

    async def start_batch(self, bot: Bot, batch: Batch, reply_to: int | None) -> None:
        try:
            message_id = await WidgetAPI(bot).send(
                batch.telegram_id,
                self.html["progress"],
                self.progress_state(batch),
                caption=f"Отправка на {batch.kindle_email}",
                reply_to=reply_to,
            )
        except TelegramError:
            logger.exception("Не удалось отправить виджет прогресса")
            message_id = None
        self.db.update_batch(batch.id, message_id=message_id)
        self.spawn(self.run_batch(bot, batch.id))

    async def run_batch(self, bot: Bot, batch_id: int) -> None:
        batch = self.db.get_batch(batch_id)
        if batch is None:
            return
        widget = (
            ThrottledWidget(WidgetAPI(bot), batch.telegram_id, batch.message_id)
            if batch.message_id
            else None
        )

        async def report() -> None:
            current = self.db.get_batch(batch_id)
            if widget and current:
                await widget.push(self.progress_state(current))

        async with self.user_lock(batch.telegram_id):
            for item in self.db.list_batch_items(batch_id):
                if item.stage != STAGE_QUEUED:
                    continue
                try:
                    self.db.set_item_stage(item.id, STAGE_DOWNLOAD)
                    await report()
                    with tempfile.TemporaryDirectory() as temp_dir:
                        local_path = Path(temp_dir) / Path(item.file_name).name
                        telegram_file = await bot.get_file(item.file_id)
                        await telegram_file.download_to_drive(custom_path=str(local_path))

                        self.db.set_item_stage(item.id, STAGE_PREPARE)
                        await report()
                        email = await asyncio.to_thread(
                            build_book_message,
                            self.settings,
                            batch.kindle_email,
                            local_path,
                            item.file_name,
                            batch.convert_pdf,
                        )

                        self.db.set_item_stage(item.id, STAGE_SMTP)
                        await report()
                        await asyncio.to_thread(deliver, self.settings, email)
                except Exception as error:  # noqa: BLE001 — ошибка одного файла не валит партию
                    logger.exception("Не удалось отправить %s (партия %s)", item.file_name, batch_id)
                    self.db.set_item_stage(item.id, STAGE_FAILED, short_error(error))
                else:
                    self.db.set_item_stage(item.id, STAGE_DONE)
                await report()

        self.db.update_batch(batch_id, finished_at=self.now())
        final = self.db.get_batch(batch_id)
        if final is None:
            return
        if widget:
            await widget.close(self.progress_state(final))
        else:
            await self.report_batch_as_text(bot, final)

    async def report_batch_as_text(self, bot: Bot, batch: Batch) -> None:
        items = self.db.list_batch_items(batch.id)
        lines = [
            ("✅ " if i.stage == STAGE_DONE else "⚠️ ") + i.file_name + (f" — {i.error}" if i.error else "")
            for i in items
        ]
        await bot.send_message(batch.telegram_id, f"Отправка на {batch.kindle_email}:\n" + "\n".join(lines))

    async def on_progress_query(self, bot: Bot, query: WidgetQuery, action: str, args: list[str]) -> None:
        api = WidgetAPI(bot)
        batch = self.db.get_batch(int(args[0])) if args and args[0].isdigit() else None
        if batch is None or batch.telegram_id != query.user_id:
            await api.answer(query.id)
            return

        restart = action == "retry" and self.db.claim_finished_batch(batch.id, self.now())
        if restart:
            self.db.reset_failed_items(batch.id)
        batch = self.db.get_batch(batch.id)
        assert batch is not None
        await api.answer(query.id, self.progress_state(batch))
        if restart:
            self.spawn(self.run_batch(bot, batch.id))

    async def recover_batches(self, bot: Bot) -> None:
        """Партии, прерванные перезапуском, помечаются ошибкой с кнопкой «Повторить»."""
        for batch_id in self.db.fail_unfinished_batches(self.now()):
            batch = self.db.get_batch(batch_id)
            if batch and batch.message_id:
                try:
                    await WidgetAPI(bot).edit(batch.telegram_id, batch.message_id, self.progress_state(batch))
                except TelegramError:
                    logger.exception("Не удалось обновить прерванную партию %s", batch_id)

    # --- маршрутизация запросов виджетов ---

    async def handle_widget_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = extract_widget_query(update)
        if query is None:
            return
        parts = query.data.split(":")
        kind, action, args = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]
        try:
            if kind == "reg":
                await self.on_registration_query(context.bot, query, action)
            elif kind == "in":
                await self.on_inbox_query(context.bot, query, action, args)
            elif kind == "pr":
                await self.on_progress_query(context.bot, query, action, args)
            else:
                await WidgetAPI(context.bot).answer(query.id)
        except Exception:
            logger.exception("Ошибка обработки widget_query %r", query.data)
            await WidgetAPI(context.bot).answer(query.id)

    # --- текстовый режим без виджетов ---

    async def legacy_send_document(self, update: Update) -> None:
        user, message = update.effective_user, update.message
        assert user is not None and message is not None and message.document is not None
        registered = self.db.get_user(user.id)
        assert registered is not None

        document = message.document
        filename = document.file_name or "book"
        extension = Path(filename).suffix.lower()
        error = self.validate_document(filename, document.file_size or 0)
        if error == "fmt":
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            await message.reply_text(
                f"Формат {extension or '(без расширения)'} не поддерживается.\n"
                f"Допустимые форматы: {allowed}"
            )
            return
        if error == "big":
            await message.reply_text(
                f"Файл слишком большой ({(document.file_size or 0) // 1024 // 1024} МБ). "
                f"Максимум: {self.settings.max_file_size // 1024 // 1024} МБ."
            )
            return

        status_message = await message.reply_text("Получил файл, отправляю на Kindle...")
        try:
            telegram_file = await document.get_file()
            with tempfile.TemporaryDirectory() as temp_dir:
                local_path = Path(temp_dir) / Path(filename).name
                await telegram_file.download_to_drive(custom_path=str(local_path))
                await asyncio.to_thread(
                    send_book_to_kindle, self.settings, registered.kindle_email, local_path, filename
                )
        except EmailSendError as error:
            logger.exception("SMTP error for user %s", user.id)
            await status_message.edit_text(
                f"Не удалось отправить книгу: {error}\nПроверьте настройки SMTP на сервере."
            )
            return
        except Exception:
            logger.exception("Unexpected error for user %s", user.id)
            await status_message.edit_text("Произошла ошибка при отправке. Попробуйте позже.")
            return

        await status_message.edit_text(
            f"Книга «{filename}» отправлена на {registered.kindle_email}.\n"
            "Она появится в библиотеке Kindle через несколько минут."
        )

    # --- сборка приложения ---

    def build_application(self) -> Application:
        async def post_init(application: Application) -> None:
            await self.recover_batches(application.bot)

        application = (
            Application.builder().token(self.settings.telegram_bot_token).post_init(post_init).build()
        )
        application.add_handler(WidgetQueryHandler(self.handle_widget_query))
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("status", self.status))
        application.add_handler(CommandHandler("register", self.register))
        application.add_handler(CommandHandler("cancel", self.cancel))
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        return application


def main() -> None:
    settings = load_settings()
    database = Database(settings.database_path)
    kindle_bot = KindleBot(settings, database)
    application = kindle_bot.build_application()
    logger.info("Бот запущен (виджеты: %s)", "включены" if settings.widgets_enabled else "выключены")
    application.run_polling(allowed_updates=Update.ALL_TYPES + WIDGET_UPDATE_TYPES)


if __name__ == "__main__":
    main()
