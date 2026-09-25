"""SQLite persistence for the friend galgame plugin.

Two libraries are shared by every friend:

* characters -- each character owns several sprites, and every sprite carries a
  list of emotion tags such as 高兴 or 害羞;
* backgrounds -- plain background images.

A bound friend points at one character and one background, so the same character
can be reused across friends.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import time

import aiosqlite


def _tags_to_text(tags: list[str]) -> str:
    """Serialize emotion tags for storage.

    Args:
        tags: Tag list in display order.

    Returns:
        JSON text holding the tags.
    """
    cleaned: list[str] = []
    for tag in tags:
        text = str(tag).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)


def _text_to_tags(raw: str | None) -> list[str]:
    """Parse stored emotion tags.

    Args:
        raw: JSON text previously written by :func:`_tags_to_text`.

    Returns:
        Tag list, empty when the value is missing or unreadable.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class GalgameStore:
    """Persist sessions, libraries, bindings, scene messages, and options."""

    def __init__(self, database_path: Path) -> None:
        """Store the database location.

        Args:
            database_path: Path of the SQLite file backing this plugin.
        """
        self.database_path = database_path

    async def initialize(self) -> None:
        """Create the schema, upgrade older databases, and migrate legacy binds."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    umo TEXT PRIMARY KEY,
                    friend_id TEXT NOT NULL DEFAULT '',
                    friend_name TEXT NOT NULL DEFAULT '',
                    last_text TEXT NOT NULL DEFAULT '',
                    last_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    umo TEXT PRIMARY KEY,
                    friend_name TEXT NOT NULL DEFAULT '',
                    sprite TEXT NOT NULL DEFAULT '',
                    background TEXT NOT NULL DEFAULT '',
                    mood TEXT NOT NULL DEFAULT '',
                    affection INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_umo
                    ON messages(umo, id);
                CREATE TABLE IF NOT EXISTS options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    batch INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_options_umo
                    ON options(umo, batch);
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sprites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL,
                    file TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sprites_character
                    ON sprites(character_id);
                CREATE TABLE IF NOT EXISTS backgrounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                """
            )
            for column, ddl in (
                ("character_id", "INTEGER NOT NULL DEFAULT 0"),
                ("background_id", "INTEGER NOT NULL DEFAULT 0"),
                ("tag", "TEXT NOT NULL DEFAULT ''"),
                ("relation", "TEXT NOT NULL DEFAULT ''"),
            ):
                cursor = await connection.execute("PRAGMA table_info(bindings)")
                if column not in {row[1] for row in await cursor.fetchall()}:
                    await connection.execute(
                        f"ALTER TABLE bindings ADD COLUMN {column} {ddl}"
                    )
            cursor = await connection.execute("PRAGMA table_info(options)")
            option_columns = {row[1] for row in await cursor.fetchall()}
            for column, ddl in (
                ("angle", "TEXT NOT NULL DEFAULT ''"),
                ("affection", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in option_columns:
                    await connection.execute(
                        f"ALTER TABLE options ADD COLUMN {column} {ddl}"
                    )
            await connection.commit()
            await self._migrate_legacy_assets(connection)

    async def _migrate_legacy_assets(self, connection: aiosqlite.Connection) -> None:
        """Turn per-friend sprite/background files into library entries.

        Older versions stored a bare file name on each binding. Those files become
        a character (named after the friend) and a background so nothing is lost.

        Args:
            connection: Open database connection used by :meth:`initialize`.
        """
        cursor = await connection.execute(
            "SELECT umo, friend_name, sprite, background FROM bindings "
            "WHERE (character_id = 0 AND sprite != '') OR (background_id = 0 AND background != '')"
        )
        rows = list(await cursor.fetchall())
        now = int(time())
        for umo, friend_name, sprite, background in rows:
            character_id = 0
            background_id = 0
            if sprite:
                cursor = await connection.execute(
                    "INSERT INTO characters (name, created_at) VALUES (?, ?)",
                    (friend_name or umo, now),
                )
                character_id = int(cursor.lastrowid or 0)
                await connection.execute(
                    "INSERT INTO sprites (character_id, file, tags, is_default, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (character_id, sprite, _tags_to_text(["默认"]), now),
                )
            if background:
                cursor = await connection.execute(
                    "INSERT INTO backgrounds (name, file, created_at) VALUES (?, ?, ?)",
                    (Path(background).stem, background, now),
                )
                background_id = int(cursor.lastrowid or 0)
            await connection.execute(
                "UPDATE bindings SET character_id = ?, background_id = ? WHERE umo = ?",
                (character_id, background_id, umo),
            )
        if rows:
            await connection.commit()

    # ---------------------------------------------------------------- sessions

    async def record_session(
        self,
        umo: str,
        friend_id: str,
        friend_name: str,
        text: str,
    ) -> None:
        """Remember a private session so the page can offer it for binding.

        Args:
            umo: Unified message origin of the private session.
            friend_id: Sender ID reported by the platform.
            friend_name: Sender nickname reported by the platform.
            text: Latest message text, used as a preview.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                """
                INSERT INTO sessions (umo, friend_id, friend_name, last_text, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    friend_id = excluded.friend_id,
                    friend_name = excluded.friend_name,
                    last_text = excluded.last_text,
                    last_seen = excluded.last_seen
                """,
                (umo, friend_id, friend_name, text, int(time())),
            )
            await connection.commit()

    async def list_sessions(self, limit: int) -> list[aiosqlite.Row]:
        """List recently active private sessions, newest first.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            Session rows ordered by last activity.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM sessions ORDER BY last_seen DESC, umo ASC LIMIT ?",
                (max(1, limit),),
            )
            return list(await cursor.fetchall())

    # -------------------------------------------------------------- libraries

    async def list_characters(self) -> list[dict]:
        """List every character together with its sprites.

        Returns:
            Character dicts ordered by name, each with a ``sprites`` list.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT id, name FROM characters ORDER BY name COLLATE NOCASE"
            )
            characters = [
                {"id": row["id"], "name": row["name"], "sprites": []}
                for row in await cursor.fetchall()
            ]
            by_id = {item["id"]: item for item in characters}
            cursor = await connection.execute(
                "SELECT id, character_id, file, tags, is_default FROM sprites "
                "ORDER BY is_default DESC, id ASC"
            )
            for row in await cursor.fetchall():
                item = by_id.get(row["character_id"])
                if item is None:
                    continue
                item["sprites"].append(
                    {
                        "id": row["id"],
                        "file": row["file"],
                        "tags": _text_to_tags(row["tags"]),
                        "is_default": bool(row["is_default"]),
                    }
                )
            return characters

    async def get_character(self, character_id: int) -> dict | None:
        """Read one character with its sprites.

        Args:
            character_id: Character identifier.

        Returns:
            Character dict, or ``None`` when it does not exist.
        """
        for item in await self.list_characters():
            if item["id"] == character_id:
                return item
        return None

    async def create_character(self, name: str) -> int:
        """Create an empty character.

        Args:
            name: Display name of the character.

        Returns:
            Identifier of the new character.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "INSERT INTO characters (name, created_at) VALUES (?, ?)",
                (name, int(time())),
            )
            await connection.commit()
            return int(cursor.lastrowid or 0)

    async def rename_character(self, character_id: int, name: str) -> None:
        """Rename a character.

        Args:
            character_id: Character identifier.
            name: New display name.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE characters SET name = ? WHERE id = ?", (name, character_id)
            )
            await connection.commit()

    async def delete_character(self, character_id: int) -> None:
        """Delete a character, its sprites, and unlink bound friends.

        Args:
            character_id: Character identifier.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "DELETE FROM sprites WHERE character_id = ?", (character_id,)
            )
            await connection.execute(
                "DELETE FROM characters WHERE id = ?", (character_id,)
            )
            await connection.execute(
                "UPDATE bindings SET character_id = 0 WHERE character_id = ?",
                (character_id,),
            )
            await connection.commit()

    async def list_backgrounds(self) -> list[dict]:
        """List every background in the shared library.

        Returns:
            Background dicts ordered by name.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT id, name, file FROM backgrounds ORDER BY name COLLATE NOCASE"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_background(self, background_id: int) -> dict | None:
        """Read one background.

        Args:
            background_id: Background identifier.

        Returns:
            Background dict, or ``None`` when it does not exist.
        """
        for item in await self.list_backgrounds():
            if item["id"] == background_id:
                return item
        return None

    async def add_background(self, name: str, file: str) -> int:
        """Add a background to the shared library.

        Args:
            name: Display name.
            file: Asset file name.

        Returns:
            Identifier of the new background.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "INSERT INTO backgrounds (name, file, created_at) VALUES (?, ?, ?)",
                (name, file, int(time())),
            )
            await connection.commit()
            return int(cursor.lastrowid or 0)

    async def delete_background(self, background_id: int) -> None:
        """Delete a background and unlink bound friends.

        Args:
            background_id: Background identifier.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "DELETE FROM backgrounds WHERE id = ?", (background_id,)
            )
            await connection.execute(
                "UPDATE bindings SET background_id = 0 WHERE background_id = ?",
                (background_id,),
            )
            await connection.commit()

    # ---------------------------------------------------------------- sprites

    async def add_sprite(self, character_id: int, file: str, tags: list[str]) -> int:
        """Attach a sprite image with emotion tags to a character.

        Args:
            character_id: Owning character.
            file: Asset file name inside the page assets directory.
            tags: Emotion labels this sprite answers to.

        Returns:
            Identifier of the new sprite.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM sprites WHERE character_id = ?", (character_id,)
            )
            is_first = (await cursor.fetchone())[0] == 0
            cursor = await connection.execute(
                "INSERT INTO sprites (character_id, file, tags, is_default, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    character_id,
                    file,
                    _tags_to_text(tags),
                    1 if is_first else 0,
                    int(time()),
                ),
            )
            await connection.commit()
            return int(cursor.lastrowid or 0)

    async def update_sprite(
        self,
        sprite_id: int,
        tags: list[str] | None = None,
        is_default: bool | None = None,
    ) -> None:
        """Update a sprite's tags and/or default flag.

        Args:
            sprite_id: Sprite identifier.
            tags: Replacement tag list, ignored when ``None``.
            is_default: Mark as the fallback sprite, ignored when ``None``.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            if tags is not None:
                await connection.execute(
                    "UPDATE sprites SET tags = ? WHERE id = ?",
                    (_tags_to_text(tags), sprite_id),
                )
            if is_default:
                cursor = await connection.execute(
                    "SELECT character_id FROM sprites WHERE id = ?", (sprite_id,)
                )
                row = await cursor.fetchone()
                if row:
                    await connection.execute(
                        "UPDATE sprites SET is_default = 0 WHERE character_id = ?",
                        (row[0],),
                    )
                    await connection.execute(
                        "UPDATE sprites SET is_default = 1 WHERE id = ?", (sprite_id,)
                    )
            await connection.commit()

    async def delete_sprite(self, sprite_id: int) -> None:
        """Delete one sprite, promoting another sprite when it was the default.

        Args:
            sprite_id: Sprite identifier.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "SELECT character_id, is_default FROM sprites WHERE id = ?",
                (sprite_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            character_id, was_default = row[0], row[1]
            await connection.execute("DELETE FROM sprites WHERE id = ?", (sprite_id,))
            if was_default:
                await connection.execute(
                    "UPDATE sprites SET is_default = 1 WHERE id = ("
                    "SELECT id FROM sprites WHERE character_id = ? ORDER BY id LIMIT 1)",
                    (character_id,),
                )
            await connection.commit()

    # --------------------------------------------------------------- bindings

    async def upsert_binding(
        self,
        umo: str,
        friend_name: str,
        character_id: int,
        background_id: int,
    ) -> None:
        """Create or update the character bound to a friend.

        Args:
            umo: Unified message origin of the private session.
            friend_name: Display name shown in the dialogue box.
            character_id: Character from the shared library, 0 for none.
            background_id: Background from the shared library, 0 for none.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                """
                INSERT INTO bindings
                    (umo, friend_name, sprite, background, mood, affection,
                     enabled, updated_at, character_id, background_id, tag)
                VALUES (?, ?, '', '', '', 0, 1, ?, ?, ?, '')
                ON CONFLICT(umo) DO UPDATE SET
                    friend_name = excluded.friend_name,
                    character_id = excluded.character_id,
                    background_id = excluded.background_id,
                    updated_at = excluded.updated_at
                """,
                (umo, friend_name, int(time()), character_id, background_id),
            )
            await connection.commit()

    async def list_bindings(self) -> list[aiosqlite.Row]:
        """List every bound friend.

        Returns:
            Binding rows ordered by most recent update.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM bindings ORDER BY updated_at DESC, umo ASC"
            )
            return list(await cursor.fetchall())

    async def get_binding(self, umo: str) -> aiosqlite.Row | None:
        """Read the binding of one friend.

        Args:
            umo: Unified message origin of the private session.

        Returns:
            The binding row, or ``None`` when the session is not bound.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM bindings WHERE umo = ?", (umo,)
            )
            return await cursor.fetchone()

    async def delete_binding(self, umo: str) -> None:
        """Remove a binding while keeping the scene history.

        Args:
            umo: Unified message origin of the private session.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute("DELETE FROM bindings WHERE umo = ?", (umo,))
            await connection.commit()

    async def set_enabled(self, umo: str, enabled: bool) -> None:
        """Enable or pause the scene for one friend.

        Args:
            umo: Unified message origin of the private session.
            enabled: Whether incoming messages should reach the scene.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE bindings SET enabled = ?, updated_at = ? WHERE umo = ?",
                (1 if enabled else 0, int(time()), umo),
            )
            await connection.commit()

    async def update_tag(self, umo: str, tag: str) -> None:
        """Store the friend's current portrait tag.

        Args:
            umo: Unified message origin of the private session.
            tag: Emotion tag chosen for the current sprite.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE bindings SET tag = ?, mood = ?, updated_at = ? WHERE umo = ?",
                (tag, tag, int(time()), umo),
            )
            await connection.commit()

    async def update_affection(self, umo: str, affection: int) -> None:
        """Store the affection value after a chosen line.

        Args:
            umo: Unified message origin of the private session.
            affection: Absolute affection value, already clamped by the caller.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE bindings SET affection = ?, updated_at = ? WHERE umo = ?",
                (affection, int(time()), umo),
            )
            await connection.commit()

    async def update_relation(self, umo: str, relation: str) -> None:
        """Store how the user and this friend relate to each other.

        Args:
            umo: Unified message origin of the private session.
            relation: Free-form relationship description.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE bindings SET relation = ?, updated_at = ? WHERE umo = ?",
                (relation, int(time()), umo),
            )
            await connection.commit()

    # --------------------------------------------------------------- settings

    async def get_setting(self, key: str, default: str = "") -> str:
        """Read one global setting shared by every friend.

        Args:
            key: Setting name.
            default: Value returned when the setting was never saved.

        Returns:
            Stored string value.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return str(row[0]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """Write one global setting shared by every friend.

        Args:
            key: Setting name.
            value: Value to store.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await connection.commit()

    # --------------------------------------------------------------- messages

    async def add_message(self, umo: str, role: str, text: str, kind: str) -> None:
        """Append one line to the scene log.

        Args:
            umo: Unified message origin of the private session.
            role: ``friend`` for the other side, ``me`` for the user.
            text: Line content.
            kind: Message kind, for example ``text`` or ``Image``.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "INSERT INTO messages (umo, role, text, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (umo, role, text, kind, int(time())),
            )
            await connection.commit()

    async def history(self, umo: str, limit: int) -> list[aiosqlite.Row]:
        """Read the most recent scene lines in chronological order.

        Args:
            umo: Unified message origin of the private session.
            limit: Maximum number of lines to read.

        Returns:
            Message rows ordered oldest to newest.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT * FROM (
                    SELECT id, role, text, kind, created_at FROM messages
                    WHERE umo = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (umo, max(1, limit)),
            )
            return list(await cursor.fetchall())

    async def add_options(
        self,
        umo: str,
        batch: int,
        items: list[dict],
    ) -> list[int]:
        """Store a fresh batch of reply candidates.

        Args:
            umo: Unified message origin of the private session.
            batch: Batch identifier shared by the candidates.
            items: Candidate dicts with ``text``, ``angle``, and ``affection``.

        Returns:
            Identifiers of the inserted candidates, in the given order.
        """
        ids: list[int] = []
        created_at = int(time())
        async with aiosqlite.connect(self.database_path) as connection:
            for item in items:
                cursor = await connection.execute(
                    "INSERT INTO options (umo, batch, text, used, created_at, angle, "
                    "affection) VALUES (?, ?, ?, 0, ?, ?, ?)",
                    (
                        umo,
                        batch,
                        item["text"],
                        created_at,
                        item.get("angle", ""),
                        int(item.get("affection", 0)),
                    ),
                )
                ids.append(int(cursor.lastrowid or 0))
            await connection.commit()
        return ids

    async def latest_options(self, umo: str) -> list[aiosqlite.Row]:
        """Read the newest candidate batch of a session.

        Args:
            umo: Unified message origin of the private session.

        Returns:
            Candidate rows ordered by insertion.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT id, text, used, angle, affection, created_at FROM options
                WHERE umo = ? AND batch = (
                    SELECT MAX(batch) FROM options WHERE umo = ?
                ) ORDER BY id ASC
                """,
                (umo, umo),
            )
            return list(await cursor.fetchall())

    async def get_option(self, umo: str, option_id: int) -> aiosqlite.Row | None:
        """Read one candidate so its affection effect can be applied.

        Args:
            umo: Unified message origin of the private session.
            option_id: Identifier of the candidate.

        Returns:
            Candidate row, or ``None`` when it does not exist.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT id, text, used, angle, affection FROM options "
                "WHERE umo = ? AND id = ?",
                (umo, option_id),
            )
            return await cursor.fetchone()

    async def mark_option_used(self, umo: str, option_id: int) -> None:
        """Mark one candidate as sent so the page can dim it.

        Args:
            umo: Unified message origin of the private session.
            option_id: Identifier of the sent candidate.
        """
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE options SET used = 1 WHERE umo = ? AND id = ?",
                (umo, option_id),
            )
            await connection.commit()
