"""SQLite-backed storage for GrimEvasion's per-guild trigger word lists."""

import aiosqlite

DB_FILE = "grimevasion.db"

_connection: aiosqlite.Connection | None = None


DEFAULT_SETTINGS = {"doLinksTrigger": False, "doMediasTrigger": False, "doFuzzyDetection": False}

# Maps the public parameter name (used in commands) to its actual column name.
_SETTING_COLUMNS = {
    "doLinksTrigger": "do_links_trigger",
    "doMediasTrigger": "do_medias_trigger",
    "doFuzzyDetection": "do_fuzzy_detection",
}


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
    await _connection.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY,
            do_links_trigger INTEGER NOT NULL DEFAULT 0,
            do_medias_trigger INTEGER NOT NULL DEFAULT 0,
            do_fuzzy_detection INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await _connection.commit()
    await _migrate_missing_columns()


async def _migrate_missing_columns() -> None:
    """Add columns that may be missing from a guild_settings table created before they existed."""
    async with _connection.execute("PRAGMA table_info(guild_settings)") as cursor:
        existing_columns = {row[1] for row in await cursor.fetchall()}

    for column in _SETTING_COLUMNS.values():
        if column not in existing_columns:
            await _connection.execute(
                f"ALTER TABLE guild_settings ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
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


async def get_settings(guild_id: str) -> dict:
    """Return this guild's settings, falling back to defaults if none are stored yet."""
    async with _connection.execute(
        "SELECT do_links_trigger, do_medias_trigger, do_fuzzy_detection FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {
        "doLinksTrigger": bool(row[0]),
        "doMediasTrigger": bool(row[1]),
        "doFuzzyDetection": bool(row[2]),
    }


async def set_setting(guild_id: str, parameter: str, value: bool) -> None:
    """Set a single setting by its public parameter name (e.g. 'doLinksTrigger')."""
    if parameter not in _SETTING_COLUMNS:
        raise ValueError(f"Unknown setting parameter: {parameter}")
    column = _SETTING_COLUMNS[parameter]
    await _connection.execute(
        f"""
        INSERT INTO guild_settings (guild_id, {column}) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET {column} = excluded.{column}
        """,
        (guild_id, int(value)),
    )
    await _connection.commit()


async def get_all_setting_guild_ids() -> list[str]:
    """Return every guild_id that has a settings row stored."""
    async with _connection.execute("SELECT guild_id FROM guild_settings") as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]