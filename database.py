from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class User:
    telegram_id: int
    kindle_email: str
    username: str | None
    first_name: str | None
    sender_confirmed: bool = False


@dataclass(frozen=True)
class Registration:
    telegram_id: int
    message_id: int | None
    awaiting_email: bool
    test_status: str
    test_error: str


@dataclass(frozen=True)
class Inbox:
    id: int
    telegram_id: int
    message_id: int | None
    status: str
    convert_pdf: bool
    updated_at: float


@dataclass(frozen=True)
class InboxFile:
    id: int
    inbox_id: int
    file_id: str
    file_name: str
    file_size: int
    error: str


@dataclass(frozen=True)
class Batch:
    id: int
    telegram_id: int
    message_id: int | None
    kindle_email: str
    convert_pdf: bool
    started_at: float
    finished_at: float | None


@dataclass(frozen=True)
class BatchItem:
    id: int
    batch_id: int
    file_id: str
    file_name: str
    file_size: int
    stage: int
    error: str


# Этапы отправки файла, они же индексы подписей в widgets/progress.html
STAGE_QUEUED, STAGE_DOWNLOAD, STAGE_PREPARE, STAGE_SMTP, STAGE_DONE, STAGE_FAILED = range(6)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    kindle_email TEXT NOT NULL,
    username TEXT,
    first_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS registrations (
    telegram_id INTEGER PRIMARY KEY,
    message_id INTEGER,
    awaiting_email INTEGER NOT NULL DEFAULT 0,
    test_status TEXT NOT NULL DEFAULT '',
    test_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    convert_pdf INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS inboxes_user ON inboxes (telegram_id, status);
CREATE TABLE IF NOT EXISTS inbox_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS inbox_files_inbox ON inbox_files (inbox_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    message_id INTEGER,
    kindle_email TEXT NOT NULL,
    convert_pdf INTEGER NOT NULL DEFAULT 1,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    stage INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS batch_items_batch ON batch_items (batch_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:") if str(path) == ":memory:" else None
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._memory or sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            if self._memory is None:
                connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
            if "sender_confirmed" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN sender_confirmed INTEGER NOT NULL DEFAULT 0"
                )

    # --- users ---

    def get_user(self, telegram_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT telegram_id, kindle_email, username, first_name, sender_confirmed "
                "FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return User(
            telegram_id=row["telegram_id"],
            kindle_email=row["kindle_email"],
            username=row["username"],
            first_name=row["first_name"],
            sender_confirmed=bool(row["sender_confirmed"]),
        )

    def upsert_user(
        self,
        telegram_id: int,
        kindle_email: str,
        username: str | None,
        first_name: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, kindle_email, username, first_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    kindle_email = excluded.kindle_email,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    sender_confirmed = CASE
                        WHEN users.kindle_email = excluded.kindle_email THEN users.sender_confirmed
                        ELSE 0 END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_id, kindle_email, username, first_name),
            )

    def set_sender_confirmed(self, telegram_id: int, value: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET sender_confirmed = ? WHERE telegram_id = ?",
                (int(value), telegram_id),
            )

    # --- registrations ---

    def get_registration(self, telegram_id: int) -> Registration:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM registrations WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        if row is None:
            return Registration(telegram_id, None, False, "", "")
        return Registration(
            telegram_id=row["telegram_id"],
            message_id=row["message_id"],
            awaiting_email=bool(row["awaiting_email"]),
            test_status=row["test_status"],
            test_error=row["test_error"],
        )

    def update_registration(self, telegram_id: int, **fields: object) -> Registration:
        allowed = {"message_id", "awaiting_email", "test_status", "test_error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Неизвестные поля: {unknown}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO registrations (telegram_id) VALUES (?)", (telegram_id,)
            )
            for key, value in fields.items():
                if isinstance(value, bool):
                    value = int(value)
                connection.execute(
                    f"UPDATE registrations SET {key} = ? WHERE telegram_id = ?",
                    (value, telegram_id),
                )
        return self.get_registration(telegram_id)

    # --- inboxes ---

    @staticmethod
    def _inbox(row: sqlite3.Row) -> Inbox:
        return Inbox(
            id=row["id"],
            telegram_id=row["telegram_id"],
            message_id=row["message_id"],
            status=row["status"],
            convert_pdf=bool(row["convert_pdf"]),
            updated_at=row["updated_at"],
        )

    def create_inbox(self, telegram_id: int, now: float, convert_pdf: bool = True) -> Inbox:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO inboxes (telegram_id, status, convert_pdf, updated_at) VALUES (?, 'open', ?, ?)",
                (telegram_id, int(convert_pdf), now),
            )
            inbox_id = cursor.lastrowid
        inbox = self.get_inbox(inbox_id)
        assert inbox is not None
        return inbox

    def get_inbox(self, inbox_id: int) -> Inbox | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM inboxes WHERE id = ?", (inbox_id,)).fetchone()
        return self._inbox(row) if row else None

    def get_open_inbox(self, telegram_id: int) -> Inbox | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM inboxes WHERE telegram_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                (telegram_id,),
            ).fetchone()
        return self._inbox(row) if row else None

    def update_inbox(self, inbox_id: int, now: float, **fields: object) -> None:
        allowed = {"message_id", "status", "convert_pdf"}
        if set(fields) - allowed:
            raise ValueError(f"Неизвестные поля: {set(fields) - allowed}")
        with self._connect() as connection:
            for key, value in fields.items():
                if isinstance(value, bool):
                    value = int(value)
                connection.execute(f"UPDATE inboxes SET {key} = ? WHERE id = ?", (value, inbox_id))
            connection.execute("UPDATE inboxes SET updated_at = ? WHERE id = ?", (now, inbox_id))

    def claim_inbox(self, inbox_id: int, new_status: str, now: float) -> bool:
        """Атомарно переводит открытый список в new_status. False, если он уже не открыт."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE inboxes SET status = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                (new_status, now, inbox_id),
            )
            return cursor.rowcount == 1

    def add_inbox_file(
        self, inbox_id: int, file_id: str, file_name: str, file_size: int, error: str, now: float
    ) -> InboxFile:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO inbox_files (inbox_id, file_id, file_name, file_size, error) VALUES (?, ?, ?, ?, ?)",
                (inbox_id, file_id, file_name, file_size, error),
            )
            connection.execute("UPDATE inboxes SET updated_at = ? WHERE id = ?", (now, inbox_id))
            file_row_id = cursor.lastrowid
        return InboxFile(file_row_id, inbox_id, file_id, file_name, file_size, error)

    def list_inbox_files(self, inbox_id: int) -> list[InboxFile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inbox_files WHERE inbox_id = ? ORDER BY id", (inbox_id,)
            ).fetchall()
        return [
            InboxFile(r["id"], r["inbox_id"], r["file_id"], r["file_name"], r["file_size"], r["error"])
            for r in rows
        ]

    def remove_inbox_file(self, inbox_id: int, file_row_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM inbox_files WHERE inbox_id = ? AND id = ?", (inbox_id, file_row_id)
            )
            return cursor.rowcount == 1

    def move_inbox_files(self, from_inbox: int, to_inbox: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE inbox_files SET inbox_id = ? WHERE inbox_id = ?", (to_inbox, from_inbox)
            )

    # --- batches ---

    def create_batch(
        self,
        telegram_id: int,
        kindle_email: str,
        convert_pdf: bool,
        files: list[tuple[str, str, int]],
        now: float,
    ) -> Batch:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO batches (telegram_id, kindle_email, convert_pdf, started_at) VALUES (?, ?, ?, ?)",
                (telegram_id, kindle_email, int(convert_pdf), now),
            )
            batch_id = cursor.lastrowid
            connection.executemany(
                "INSERT INTO batch_items (batch_id, file_id, file_name, file_size) VALUES (?, ?, ?, ?)",
                [(batch_id, file_id, name, size) for file_id, name, size in files],
            )
        batch = self.get_batch(batch_id)
        assert batch is not None
        return batch

    def get_batch(self, batch_id: int) -> Batch | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        return Batch(
            id=row["id"],
            telegram_id=row["telegram_id"],
            message_id=row["message_id"],
            kindle_email=row["kindle_email"],
            convert_pdf=bool(row["convert_pdf"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def update_batch(self, batch_id: int, **fields: object) -> None:
        allowed = {"message_id", "started_at", "finished_at"}
        if set(fields) - allowed:
            raise ValueError(f"Неизвестные поля: {set(fields) - allowed}")
        with self._connect() as connection:
            for key, value in fields.items():
                connection.execute(f"UPDATE batches SET {key} = ? WHERE id = ?", (value, batch_id))

    def claim_finished_batch(self, batch_id: int, now: float) -> bool:
        """Атомарно перезапускает завершённую партию. False, если она ещё идёт."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batches SET finished_at = NULL, started_at = ? WHERE id = ? AND finished_at IS NOT NULL",
                (now, batch_id),
            )
            return cursor.rowcount == 1

    def list_batch_items(self, batch_id: int) -> list[BatchItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY id", (batch_id,)
            ).fetchall()
        return [
            BatchItem(r["id"], r["batch_id"], r["file_id"], r["file_name"], r["file_size"], r["stage"], r["error"])
            for r in rows
        ]

    def set_item_stage(self, item_id: int, stage: int, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_items SET stage = ?, error = ? WHERE id = ?", (stage, error, item_id)
            )

    def reset_failed_items(self, batch_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batch_items SET stage = ?, error = '' WHERE batch_id = ? AND stage = ?",
                (STAGE_QUEUED, batch_id, STAGE_FAILED),
            )
            return cursor.rowcount

    def fail_unfinished_batches(self, now: float) -> list[int]:
        """После перезапуска бота незавершённые отправки помечаются ошибкой."""
        with self._connect() as connection:
            ids = [
                r["id"] for r in connection.execute("SELECT id FROM batches WHERE finished_at IS NULL")
            ]
            for batch_id in ids:
                connection.execute(
                    "UPDATE batch_items SET stage = ?, error = 'Бот перезапускался, повторите' "
                    "WHERE batch_id = ? AND stage < ?",
                    (STAGE_FAILED, batch_id, STAGE_DONE),
                )
                connection.execute("UPDATE batches SET finished_at = ? WHERE id = ?", (now, batch_id))
        return ids
