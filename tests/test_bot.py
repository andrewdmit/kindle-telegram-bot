import asyncio
import json
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import BadRequest

import bot as bot_module
from bot import KindleBot, WidgetQueryHandler
from config import Settings
from database import STAGE_DONE, STAGE_FAILED, Database
from email_sender import EmailSendError
from widget_api import MAX_HTML_COMPRESSED, MAX_STATE_BYTES, extract_widget_query, load_widget_html

USER_ID = 111


def make_settings(**overrides):
    values = dict(
        telegram_bot_token="t",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="bot@example.com",
        smtp_password="p",
        smtp_from_email="bot@example.com",
        smtp_use_tls=True,
        database_path=Path(":memory:"),
        max_file_size=20 * 1024 * 1024,
        widgets_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


class FakeFile:
    def __init__(self, content: bytes):
        self.content = content

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(self.content)


class FakeBot:
    def __init__(self):
        self.calls = []
        self.next_message_id = 1000
        self.fail_methods = {}
        self.sent_texts = []

    async def do_api_request(self, endpoint, api_kwargs=None, return_type=None):
        self.calls.append((endpoint, api_kwargs))
        if endpoint in self.fail_methods:
            raise self.fail_methods[endpoint]
        if endpoint in ("sendWidget", "sendSharedWidget"):
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        return True

    async def get_file(self, file_id):
        if file_id == "missing":
            raise BadRequest("Wrong file_id specified")
        return FakeFile(b"book-" + file_id.encode())

    async def send_message(self, chat_id, text):
        self.sent_texts.append(text)

    def by(self, endpoint):
        return [kwargs for name, kwargs in self.calls if name == endpoint]

    def last_state(self, endpoint="editMessageWidgetData", message_id=None):
        for kwargs in reversed(self.by(endpoint)):
            if message_id is None or kwargs.get("message_id") == message_id:
                return json.loads(kwargs["chat_data"])
        raise AssertionError(f"no {endpoint}")


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(monkeypatch):
    delivered = []

    def fake_deliver(settings, message):
        if "broken" in message["Subject"]:
            raise EmailSendError("Почтовый сервер отклонил адрес Kindle")
        delivered.append(message)

    monkeypatch.setattr(bot_module, "deliver", fake_deliver)
    monkeypatch.setattr(bot_module, "send_book_to_kindle", lambda *a, **k: delivered.append(("test", a)))
    clock = Clock()
    app = KindleBot(make_settings(), Database(Path(":memory:")), now=clock)
    fake = FakeBot()
    context = SimpleNamespace(bot=fake)
    return SimpleNamespace(app=app, bot=fake, ctx=context, delivered=delivered, clock=clock)


def message_update(text=None, document=None):
    message = SimpleNamespace(
        text=text,
        document=document,
        chat_id=USER_ID,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, username="u", first_name="U"),
        message=message,
    )


def doc(file_id, name, size=1000):
    return SimpleNamespace(file_id=file_id, file_name=name, file_size=size)


def widget_update(data, message_id):
    return Update.de_json(
        {
            "update_id": 1,
            "widget_query": {
                "id": f"q-{data}",
                "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
                "chat": {"id": USER_ID, "type": "private"},
                "message_id": message_id,
                "chat_instance": "1",
                "type": {"type": "data"},
                "data": data,
            },
        },
        None,
    )


async def query(env, data, message_id=1):
    update = widget_update(data, message_id)
    assert WidgetQueryHandler(env.app.handle_widget_query).check_update(update)
    await env.app.handle_widget_query(update, env.ctx)
    answers = [k for k in env.bot.by("answerWidgetQuery") if k["widget_query_id"] == f"q-{data}"]
    assert answers, "каждый запрос должен получить ответ"
    return json.loads(answers[-1]["data"]) if "data" in answers[-1] else None


async def register(env, email="reader@kindle.com"):
    await env.app.start(message_update("/start"), env.ctx)
    await env.app.handle_text(message_update(email), env.ctx)
    reg_msg = env.bot.next_message_id
    await query(env, "reg:ok", reg_msg)
    return reg_msg


# --- виджеты ---


@pytest.mark.parametrize("name", ["registration", "inbox", "progress"])
def test_widget_html_fits_limits(name):
    html = load_widget_html(name).encode("utf-8")
    assert len(html) <= 32768
    assert len(zlib.compress(html, 9)) <= MAX_HTML_COMPRESSED
    assert b"\x00" not in html
    # всё это вне профиля виджетов: клиент молча вырежет, и дизайн развалится
    banned = (
        "<input", "<select", "<textarea", "<progress", "fetch(", "vh;", "vh)",
        "color-mix", "oklch", ":has(", "text-shadow", "clip-path", "mask:",
        "@font-face", "position:sticky", "overflow:auto", "overflow:scroll",
        "font-family:'", 'font-family:"', "<animate",
    )
    text = html.decode("utf-8")
    for token in banned:
        assert token not in text, token
    # opacity < 1 и размытые тени — «тяжёлые» элементы, их не больше 16: не используем вовсе
    assert "opacity:" not in text.replace("opacity:1", "")


def test_widget_query_is_parsed_from_api_kwargs():
    q = extract_widget_query(widget_update("in:send:5", 42))
    assert q.user_id == USER_ID and q.message_id == 42 and q.data == "in:send:5"


# --- регистрация ---


async def test_registration_flow(env):
    await env.app.start(message_update("/start"), env.ctx)
    sent = env.bot.by("sendWidget")[0]
    assert sent["allow_queries"] is True
    assert json.loads(sent["chat_data"])["s"] == 1
    reg_msg = env.bot.next_message_id

    bad = message_update("hello@gmail.com")
    await env.app.handle_text(bad, env.ctx)
    assert "не похоже" in bad.message.reply_text.call_args.args[0]

    await env.app.handle_text(message_update("Reader@Kindle.com"), env.ctx)
    state = env.bot.last_state(message_id=reg_msg)
    assert state["s"] == 2 and state["e"] == "reader@kindle.com" and state["f"] == "bot@example.com"

    answer = await query(env, "reg:ok", reg_msg)
    assert answer["s"] == 3
    assert env.app.db.get_user(USER_ID).sender_confirmed

    answer = await query(env, "reg:test", reg_msg)
    assert answer["t"] == "run"
    await env.app.wait_background()
    assert env.bot.last_state(message_id=reg_msg)["t"] == "ok"
    assert env.delivered and env.delivered[-1][0] == "test"

    answer = await query(env, "reg:edit", reg_msg)
    assert answer["s"] == 1
    await env.app.handle_text(message_update("other@kindle.com"), env.ctx)
    state = env.bot.last_state(message_id=reg_msg)
    # смена адреса сбрасывает подтверждение отправителя
    assert state["s"] == 2 and state["e"] == "other@kindle.com"


async def test_revisions_are_monotonic(env):
    reg_msg = await register(env)
    revs = [json.loads(k["chat_data"])["r"] for k in env.bot.by("editMessageWidgetData")]
    assert revs == sorted(revs) and len(set(revs)) == len(revs)
    assert reg_msg


async def test_unregistered_document_starts_registration(env):
    update = message_update(document=doc("f1", "book.epub"))
    await env.app.handle_document(update, env.ctx)
    assert env.bot.by("sendWidget")[0]["caption"].startswith("Регистрация")
    assert env.app.db.get_registration(USER_ID).awaiting_email


# --- приём файлов и отправка ---


async def test_inbox_collects_files_and_sends_batch(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("f1", "Война и мир.epub")), env.ctx)
    inbox_msg = env.bot.next_message_id
    await env.app.handle_document(message_update(document=doc("f2", "scan.pdf")), env.ctx)
    await env.app.handle_document(message_update(document=doc("f3", "virus.exe")), env.ctx)
    await env.app.handle_document(message_update(document=doc("f4", "huge.pdf", 50 * 1024 * 1024)), env.ctx)

    # все файлы попали в одну карточку
    assert len([k for k in env.bot.by("sendWidget") if k["caption"].startswith("Приём")]) == 1
    state = env.bot.last_state(message_id=inbox_msg)
    assert [f[1] for f in state["f"]] == ["Война и мир.epub", "scan.pdf", "virus.exe", "huge.pdf"]
    assert [f[4] for f in state["f"]] == ["", "", "fmt", "big"]
    assert state["st"] == "open" and state["cv"] == 1

    inbox_id = state["id"]
    answer = await query(env, f"in:cv:{inbox_id}", inbox_msg)
    assert answer["cv"] == 0

    answer = await query(env, f"in:send:{inbox_id}", inbox_msg)
    assert answer["st"] == "sent"
    progress_call = env.bot.by("sendWidget")[-1]
    assert progress_call["reply_parameters"]["message_id"] == inbox_msg
    progress_msg = env.bot.next_message_id

    await env.app.wait_background()
    final = env.bot.last_state(message_id=progress_msg)
    assert [f[1] for f in final["f"]] == [STAGE_DONE, STAGE_DONE]
    assert "t1" in final
    subjects = [m["Subject"] for m in env.delivered]
    # конвертация выключена — у PDF нет «Convert:» в теме
    assert subjects == ["Война и мир.epub", "scan.pdf"]

    # повторная отправка того же списка ничего не делает
    await query(env, f"in:send:{inbox_id}", inbox_msg)
    await env.app.wait_background()
    assert len(env.delivered) == 2


async def test_remove_and_clear(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("f1", "a.epub")), env.ctx)
    inbox_msg = env.bot.next_message_id
    await env.app.handle_document(message_update(document=doc("f2", "b.epub")), env.ctx)
    state = env.bot.last_state(message_id=inbox_msg)
    answer = await query(env, f"in:rm:{state['id']}:{state['f'][0][0]}", inbox_msg)
    assert [f[1] for f in answer["f"]] == ["b.epub"]
    answer = await query(env, f"in:clr:{state['id']}", inbox_msg)
    assert answer["st"] == "cleared"


async def test_stale_inbox_moves_files_to_new_card(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("f1", "a.epub")), env.ctx)
    first_msg = env.bot.next_message_id
    env.clock.t += bot_module.INBOX_REUSE_SECONDS + 1
    await env.app.handle_document(message_update(document=doc("f2", "b.epub")), env.ctx)
    second_msg = env.bot.next_message_id
    assert second_msg != first_msg
    assert env.bot.last_state(message_id=first_msg)["st"] == "moved"
    assert [f[1] for f in env.bot.last_state("sendWidget")["f"]] == ["a.epub", "b.epub"]


async def test_deleted_inbox_message_creates_new_card(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("f1", "a.epub")), env.ctx)
    env.bot.fail_methods["editMessageWidgetData"] = BadRequest("Message to edit not found")
    await env.app.handle_document(message_update(document=doc("f2", "b.epub")), env.ctx)
    del env.bot.fail_methods["editMessageWidgetData"]
    names = [f[1] for f in env.bot.last_state("sendWidget")["f"]]
    assert names == ["a.epub", "b.epub"]


async def test_failures_and_retry(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("missing", "gone.epub")), env.ctx)
    inbox_msg = env.bot.next_message_id
    await env.app.handle_document(message_update(document=doc("f2", "broken.epub")), env.ctx)
    await env.app.handle_document(message_update(document=doc("f3", "fine.epub")), env.ctx)
    inbox_id = env.bot.last_state(message_id=inbox_msg)["id"]
    await query(env, f"in:send:{inbox_id}", inbox_msg)
    progress_msg = env.bot.next_message_id
    await env.app.wait_background()

    final = env.bot.last_state(message_id=progress_msg)
    stages = [f[1] for f in final["f"]]
    assert stages == [STAGE_FAILED, STAGE_FAILED, STAGE_DONE]
    assert final["f"][0][2].startswith("Telegram:")
    assert "отклонил" in final["f"][1][2]

    batch_id = final["id"]
    answer = await query(env, f"pr:retry:{batch_id}", progress_msg)
    assert "t1" not in answer and [f[1] for f in answer["f"]][:2] == [0, 0]
    await env.app.wait_background()
    final = env.bot.last_state(message_id=progress_msg)
    assert [f[1] for f in final["f"]] == [STAGE_FAILED, STAGE_FAILED, STAGE_DONE]
    # успешный файл повторно не отправлялся
    assert [m["Subject"] for m in env.delivered if not isinstance(m, tuple)] == ["fine.epub"]


async def test_foreign_user_cannot_touch_inbox(env):
    await register(env)
    await env.app.handle_document(message_update(document=doc("f1", "a.epub")), env.ctx)
    inbox_id = env.bot.last_state("sendWidget")["id"]
    update = widget_update(f"in:send:{inbox_id}", 1)
    update.api_kwargs["widget_query"]["from"]["id"] = 999
    await env.app.handle_widget_query(update, env.ctx)
    assert "data" not in env.bot.by("answerWidgetQuery")[-1]
    assert env.app.db.get_inbox(inbox_id).status == "open"


async def test_state_fits_with_many_long_names(env):
    await register(env)
    for i in range(30):
        name = f"Очень длинное название книги номер {i} — полное собрание сочинений, том {i}.epub"
        await env.app.handle_document(message_update(document=doc(f"f{i}", name)), env.ctx)
    for name, kwargs in env.bot.calls:
        if "chat_data" in kwargs:
            assert len(kwargs["chat_data"].encode()) <= MAX_STATE_BYTES
    state = env.bot.last_state()
    assert len(state["f"]) + state.get("x", 0) == 30


async def test_recover_marks_interrupted_batches(env):
    await register(env)
    batch = env.app.db.create_batch(USER_ID, "reader@kindle.com", True, [("f1", "a.epub", 1)], env.clock())
    env.app.db.update_batch(batch.id, message_id=777)
    await env.app.recover_batches(env.bot)
    state = env.bot.last_state(message_id=777)
    assert state["f"][0][1] == STAGE_FAILED and "t1" in state


async def test_fallback_to_text_when_widgets_unavailable(env):
    await register(env)
    env.bot.fail_methods["sendWidget"] = BadRequest("Method not found")
    update = message_update(document=doc("f1", "a.epub"))
    update.message.document.get_file = AsyncMock(return_value=FakeFile(b"x"))
    status = SimpleNamespace(edit_text=AsyncMock())
    update.message.reply_text = AsyncMock(return_value=status)
    await env.app.handle_document(update, env.ctx)
    assert "отправлена" in status.edit_text.call_args.args[0]
    assert env.delivered[-1][0] == "test"  # legacy-путь вызывает send_book_to_kindle


async def test_widgets_disabled_uses_text_registration(env):
    app = KindleBot(make_settings(widgets_enabled=False), Database(Path(":memory:")))
    update = message_update("/start")
    await app.start(update, env.ctx)
    assert not env.bot.by("sendWidget")
    assert "Kindle email" in update.message.reply_text.call_args.args[0]
    await app.handle_text(message_update("me@kindle.com"), env.ctx)
    assert app.db.get_user(USER_ID).kindle_email == "me@kindle.com"
