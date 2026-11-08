from __future__ import annotations

import sqlite3
import secrets
import argon2
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

argon2_hash = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "users.db"


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                is_admin INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_login TEXT
            )
        """)


class UserManager:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        _init_db()

    def _hash_password(self, password: str) -> str:
        return argon2_hash.hash(password)

    def _verify_password(self, password: str, password_hash: str) -> bool:
        try:
            argon2_hash.verify(password_hash, password)
            return True
        except argon2.exceptions.VerifyMismatchError:
            return False

    def create_user(self, username: str, password: str, display_name: str = "", is_admin: bool = False) -> int:
        if not re.match(r'^[\w\-_]{3,32}$', username):
            raise ValueError("Username must be 3-32 chars, alphanumeric, underscore or hyphen")
        if len(password) < 4:
            raise ValueError("Password must be at least 4 characters")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?, ?, ?, ?)",
                (username, self._hash_password(password), display_name or username, 1 if is_admin else 0)
            )
            return cursor.lastrowid

    def verify_user(self, username: str, password: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, username, password_hash, display_name, is_admin FROM users WHERE LOWER(username) = LOWER(?) AND is_active = 1",
                (username,)
            ).fetchone()
        if row and self._verify_password(password, row["password_hash"]):
            now = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, row["id"]))
            return dict(row)
        return None

    def get_user(self, user_id: int) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, include_inactive: bool = False) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT id, username, display_name, is_admin, is_active, created_at, last_login FROM users"
            if not include_inactive:
                query += " WHERE is_active = 1"
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, **fields) -> bool:
        allowed = ["display_name", "is_admin", "is_active"]
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                (*updates.values(), user_id)
            )
        return True

    def change_password(self, user_id: int, new_password: str) -> bool:
        if len(new_password) < 4:
            raise ValueError("Password must be at least 4 characters")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (self._hash_password(new_password), user_id))
        return True

    def delete_user(self, user_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True

    def admin_exists(self) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            return bool(conn.execute("SELECT 1 FROM users WHERE is_admin = 1 AND is_active = 1").fetchone())

    def create_admin(self, username: str, password: str) -> int:
        return self.create_user(username, password, username, is_admin=True)

    def get_user_dir(self, user_id: int) -> Path:
        return DATA_DIR / str(user_id)

    def ensure_user_dir(self, user_id: int) -> Path:
        user_dir = self.get_user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir