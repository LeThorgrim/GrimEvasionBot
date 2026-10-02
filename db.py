"""SQLite-backed storage for GrimEvasion's per-guild trigger word lists."""

import aiosqlite

DB_FILE = "grimevasion.db"

_connection: aiosqlite.Connection | None = None


async def init_db() -> None:
    """Open the database connection and create tables if needed. Call once at startup."""
    global _connection
    _connection = await aiosqlite.connect(DB_FILE)
    await _connection.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_words (
            guild_id TEXT NOT NULL,
            word TEXT NOT NULL,
            PRIMARY KEY (guild_id, word)
        )
        """
    )
    await _connection.commit()


async def close_db() -> None:
    if _connection is not None:
        await _connection.close()


async def get_words(guild_id: str) -> list[str]:
    """Return all trigger words for a guild, sorted alphabetically."""
    async with _connection.execute(
        "SELECT word FROM guild_words WHERE guild_id = ? ORDER BY word", (guild_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_all_guild_ids() -> list[str]:
    """Return every guild_id that has at least one word stored."""
    async with _connection.execute("SELECT DISTINCT guild_id FROM guild_words") as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def add_word(guild_id: str, word: str) -> bool:
    """Add a single word. Returns False if it was already present."""
    try:
        await _connection.execute(
            "INSERT INTO guild_words (guild_id, word) VALUES (?, ?)", (guild_id, word)
        )
        await _connection.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # already exists (guild_id, word) pair


async def remove_word(guild_id: str, word: str) -> bool:
    """Remove a single word. Returns False if it wasn't present."""
    cursor = await _connection.execute(
        "DELETE FROM guild_words WHERE guild_id = ? AND word = ?", (guild_id, word)
    )
    await _connection.commit()
    return cursor.rowcount > 0


async def add_words(guild_id: str, words: list[str]) -> tuple[int, int]:
    """Add multiple words at once. Returns (added_count, skipped_count)."""
    added = 0
    for word in words:
        if await add_word(guild_id, word):
            added += 1
    skipped = len(words) - added
    return added, skipped