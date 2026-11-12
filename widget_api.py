"""Тонкая обёртка над методами HTML-виджетов Bot API 10.4.

python-telegram-bot пока не знает о виджетах, поэтому методы вызываются
через Bot.do_api_request, а апдейты widget_query достаются из api_kwargs.
Документация: https://core.telegram.org/bots/widgets
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from telegram import Bot, Update
from telegram.error import BadRequest, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

WIDGETS_DIR = Path(__file__).resolve().parent / "widgets"

MAX_STATE_BYTES = 1024
MAX_HTML_BYTES = 32768
MAX_HTML_COMPRESSED = 4096

WIDGET_UPDATE_TYPES = ["widget_query", "shared_widget_query"]


def load_widget_html(name: str) -> str:
    return (WIDGETS_DIR / f"{name}.html").read_text(encoding="utf-8").strip()


def dump_state(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def state_size(state: dict[str, Any]) -> int:
    return len(dump_state(state).encode("utf-8"))


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def fit_state(build: Callable[[int, int], dict[str, Any]], item_count: int) -> dict[str, Any]:
    """Подбирает длину имён и число элементов, чтобы состояние влезло в 1024 байта.

    build(name_limit, max_items) возвращает состояние.
    """
    for max_items in (item_count, 12, 8, 5, 3, 1):
        if max_items > item_count:
            continue
        for name_limit in (80, 48, 32, 22, 14):
            state = build(name_limit, max_items)
            if state_size(state) <= MAX_STATE_BYTES:
                return state
    state = build(8, 0)
    if state_size(state) > MAX_STATE_BYTES:
        raise ValueError("Состояние виджета не помещается в 1024 байта")
    return state


@dataclass(frozen=True)
class WidgetQuery:
    id: str
    user_id: int
    chat_id: int | None
    message_id: int | None
    data: str
    raw: dict[str, Any]


def extract_widget_query(update: Update) -> WidgetQuery | None:
    raw = (update.api_kwargs or {}).get("widget_query")
    if not isinstance(raw, dict):
        return None
    chat = raw.get("chat") or {}
    return WidgetQuery(
        id=str(raw.get("id", "")),
        user_id=int((raw.get("from") or {}).get("id", 0)),
        chat_id=chat.get("id"),
        message_id=raw.get("message_id"),
        data=str(raw.get("data", "")),
        raw=raw,
    )


def is_widget_update(update: object) -> bool:
    return isinstance(update, Update) and "widget_query" in (update.api_kwargs or {})


class WidgetAPI:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(
        self,
        chat_id: int,
        html: str,
        chat_data: dict[str, Any],
        caption: str,
        reply_to: int | None = None,
        allow_queries: bool = True,
    ) -> int:
        """Отправляет виджет и возвращает message_id."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "html": html,
            "chat_data": dump_state(chat_data),
            "caption": caption,
            "allow_queries": allow_queries,
        }
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = await self.bot.do_api_request("sendWidget", api_kwargs=params)
        return int(result["message_id"])

    async def edit(self, chat_id: int, message_id: int, chat_data: dict[str, Any]) -> bool:
        """Меняет chat_data виджета. Возвращает False, если сообщение исчезло."""
        params = {"chat_id": chat_id, "message_id": message_id, "chat_data": dump_state(chat_data)}
        for attempt in range(3):
            try:
                await self.bot.do_api_request("editMessageWidgetData", api_kwargs=params)
                return True
            except RetryAfter as error:
                delay = error.retry_after
                seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
                await asyncio.sleep(seconds + 0.1)
            except BadRequest as error:
                text = str(error).lower()
                if "not modified" in text:
                    return True
                if "not found" in text or "message_id_invalid" in text:
                    logger.info("Виджет %s/%s больше не существует", chat_id, message_id)
                    return False
                raise
        return False

    async def answer(self, query_id: str, data: dict[str, Any] | str | None = None) -> None:
        params: dict[str, Any] = {"widget_query_id": query_id}
        if data is not None:
            params["data"] = data if isinstance(data, str) else dump_state(data)
        try:
            await self.bot.do_api_request("answerWidgetQuery", api_kwargs=params)
        except TelegramError:
            logger.exception("Не удалось ответить на widget_query %s", query_id)


class ThrottledWidget:
    """Склеивает частые обновления виджета: не чаще одного edit в interval секунд.

    editMessageWidgetData считается отправленным сообщением в лимитах бота,
    поэтому прогресс не стоит слать на каждый чих.
    """

    def __init__(self, api: WidgetAPI, chat_id: int, message_id: int, interval: float = 1.0) -> None:
        self.api = api
        self.chat_id = chat_id
        self.message_id = message_id
        self.interval = interval
        self._pending: dict[str, Any] | None = None
        self._last = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.alive = True

    async def push(self, state: dict[str, Any]) -> None:
        self._pending = state
        wait = self.interval - (time.monotonic() - self._last)
        if wait <= 0:
            await self._flush()
        elif self._task is None or self._task.done():
            self._task = asyncio.create_task(self._delayed(wait))

    async def _delayed(self, wait: float) -> None:
        await asyncio.sleep(wait)
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            state, self._pending = self._pending, None
            if state is None or not self.alive:
                return
            self._last = time.monotonic()
            try:
                self.alive = await self.api.edit(self.chat_id, self.message_id, state)
            except TelegramError:
                logger.exception("Не удалось обновить виджет %s", self.message_id)

    async def close(self, state: dict[str, Any]) -> None:
        """Финальное состояние отправляется всегда, без ожидания."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._pending = state
        await self._flush()
