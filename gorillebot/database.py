import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite


class MessageDatabase:
    def __init__(self, db_path: str, logger) -> None:
        self.db_path = db_path
        self.logger = logger
        self.connection: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.db_path)
        self.connection.row_factory = aiosqlite.Row

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def init_db(self) -> None:
        if not self.connection:
            raise RuntimeError("La base de données n'est pas connectée")
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
                first_name TEXT, chat_id INTEGER, message_text TEXT, is_spam INTEGER,
                spam_reasons TEXT, regex_score INTEGER DEFAULT 0, ai_confidence REAL,
                ai_reason TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP, action_taken TEXT
            );
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
                ban_reason TEXT, banned_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT UNIQUE,
                messages_analyzed INTEGER DEFAULT 0, spam_detected INTEGER DEFAULT 0,
                users_banned INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE TABLE IF NOT EXISTS reviews (
                review_message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL, target_username TEXT, content TEXT,
                ai_reason TEXT, ai_confidence REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                deadline TEXT NOT NULL, resolved INTEGER DEFAULT 0, resolution TEXT
            );
            CREATE TABLE IF NOT EXISTS review_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, review_message_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL, vote TEXT NOT NULL,
                voted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(review_message_id, voter_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_active ON reviews(resolved, deadline);
            """
        )
        await self._ensure_message_columns()
        await self.connection.commit()

    async def _ensure_message_columns(self) -> None:
        cursor = await self.connection.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        for name, definition in {
            "regex_score": "INTEGER DEFAULT 0",
            "ai_confidence": "REAL",
            "ai_reason": "TEXT",
        }.items():
            if name not in columns:
                await self.connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    async def _increment_daily(self, messages: int = 0, spam: int = 0, banned: int = 0) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self.connection.execute(
            "INSERT INTO daily_stats (date, messages_analyzed, spam_detected, users_banned) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET messages_analyzed = messages_analyzed + excluded.messages_analyzed, "
            "spam_detected = spam_detected + excluded.spam_detected, users_banned = users_banned + excluded.users_banned",
            (today, messages, spam, banned),
        )

    async def log_message(self, user_id: int, username: str, first_name: str, chat_id: int,
                          message_text: str, is_spam: bool, spam_reasons: List[str],
                          action_taken: str = "none", regex_score: int = 0,
                          ai_confidence: Optional[float] = None, ai_reason: Optional[str] = None) -> None:
        await self.connection.execute(
            "INSERT INTO messages (user_id, username, first_name, chat_id, message_text, is_spam, spam_reasons, regex_score, ai_confidence, ai_reason, action_taken) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, chat_id, message_text, int(is_spam), json.dumps(spam_reasons), regex_score, ai_confidence, ai_reason, action_taken),
        )
        await self._increment_daily(messages=1, spam=int(is_spam))
        await self.connection.commit()

    async def log_ban(self, user_id: int, username: str, ban_reason: str) -> None:
        await self.connection.execute("INSERT INTO bans (user_id, username, ban_reason) VALUES (?, ?, ?)", (user_id, username, ban_reason))
        await self._increment_daily(banned=1)
        await self.connection.commit()

    async def get_total_bans(self) -> int:
        cursor = await self.connection.execute("SELECT COUNT(*) FROM bans")
        row = await cursor.fetchone()
        return int(row[0])

    async def get_user_history(self, user_id: int) -> list:
        cursor = await self.connection.execute("SELECT * FROM messages WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_stats(self, days: int = 7) -> Dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        cursor = await self.connection.execute(
            "SELECT COALESCE(SUM(messages_analyzed), 0), COALESCE(SUM(spam_detected), 0), COALESCE(SUM(users_banned), 0) FROM daily_stats WHERE date >= ?",
            (since,),
        )
        row = await cursor.fetchone()
        return {"messages_analyzed": int(row[0]), "spam_detected": int(row[1]), "users_banned": int(row[2])}

    async def create_review(self, review_message_id: int, chat_id: int, target_user_id: int,
                            target_username: str, content: str, ai_reason: str,
                            ai_confidence: float, deadline: datetime) -> None:
        await self.connection.execute(
            "INSERT INTO reviews (review_message_id, chat_id, target_user_id, target_username, content, ai_reason, ai_confidence, deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (review_message_id, chat_id, target_user_id, target_username, content, ai_reason, ai_confidence, deadline.isoformat()),
        )
        await self.connection.commit()

    async def get_active_reviews(self) -> list:
        cursor = await self.connection.execute("SELECT * FROM reviews WHERE resolved = 0")
        reviews = []
        for row in await cursor.fetchall():
            item = dict(row)
            votes_cursor = await self.connection.execute(
                "SELECT voter_id, vote FROM review_votes WHERE review_message_id = ?",
                (item["review_message_id"],),
            )
            item["votes"] = [dict(vote) for vote in await votes_cursor.fetchall()]
            reviews.append(item)
        return reviews

    async def add_review_vote(self, review_message_id: int, voter_id: int, vote: str) -> bool:
        cursor = await self.connection.execute(
            "INSERT OR IGNORE INTO review_votes (review_message_id, voter_id, vote) VALUES (?, ?, ?)",
            (review_message_id, voter_id, vote),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def resolve_review(self, review_message_id: int, resolution: str) -> None:
        await self.connection.execute(
            "UPDATE reviews SET resolved = 1, resolution = ? WHERE review_message_id = ?",
            (resolution, review_message_id),
        )
        await self.connection.commit()