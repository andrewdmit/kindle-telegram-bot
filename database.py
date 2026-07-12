from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class User:
    telegram_id: int
    kindle_email: str
    username: str | None
    first_name: str | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    kindle_email TEXT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get_user(self, telegram_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT telegram_id, kindle_email, username, first_name FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

        if row is None:
            return None

        return User(
            telegram_id=row["telegram_id"],
            kindle_email=row["kindle_email"],
            username=row["username"],
            first_name=row["first_name"],
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
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_id, kindle_email, username, first_name),
            )
