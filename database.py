from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    staff_role_id INTEGER,
    ticket_category_id INTEGER,
    ticket_log_channel_id INTEGER,
    mod_log_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    subject TEXT,
    claimed_by INTEGER,
    created_at INTEGER NOT NULL,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets (guild_id, owner_id, closed_at);

CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id);

CREATE TABLE IF NOT EXISTS polls (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_votes (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    option_idx INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER UNIQUE,
    host_id INTEGER NOT NULL,
    prize TEXT NOT NULL,
    description TEXT,
    image_name TEXT,
    winner_count INTEGER NOT NULL,
    required_role_id INTEGER,
    ends_at INTEGER NOT NULL,
    remaining INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    winners TEXT NOT NULL DEFAULT '[]',
    drawn TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways (status, ends_at);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
"""

CONFIG_COLUMNS = (
    "staff_role_id",
    "ticket_category_id",
    "ticket_log_channel_id",
    "mod_log_channel_id",
)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        last_id = cur.lastrowid
        await cur.close()
        return last_id

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchall()

    # --- configuration par serveur ------------------------------------------
    async def get_config(self, guild_id: int) -> dict:
        row = await self.fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            return {"guild_id": guild_id, **{col: None for col in CONFIG_COLUMNS}}
        return dict(row)

    async def set_config(self, guild_id: int, **values) -> None:
        for key in values:
            if key not in CONFIG_COLUMNS:
                raise ValueError(f"Unknown config key: {key}")
        await self.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        if values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            await self.execute(
                f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
                (*values.values(), guild_id),
            )
