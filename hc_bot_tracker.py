"""
Crit Commanders Hardcore Classic Challenge Tracker

Runs a Discord slash-command bot and a background Blizzard API poller.

Install:
    pip install -U discord.py requests

Required env vars, Windows cmd.exe:
    set "DISCORD_BOT_TOKEN=your_discord_bot_token"
    set "BNET_CLIENT_ID=your_blizzard_client_id"
    set "BNET_CLIENT_SECRET=your_blizzard_client_secret"

Recommended env vars:
    set "DISCORD_GUILD_ID=your_discord_server_id"
    set "HC_ALLOWED_CHANNEL_ID=channel_where_commands_are_allowed"
    set "HC_ANNOUNCE_CHANNEL_ID=channel_where_announcements_should_post"
    set "HC_POLL_INTERVAL_SECONDS=300"

Run:
    python hc_bot_tracker.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import discord
import requests
from discord import app_commands


DB_PATH = os.getenv("HC_DB_PATH", "hc_players.db")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
HC_ALLOWED_CHANNEL_ID = os.getenv("HC_ALLOWED_CHANNEL_ID", "1523399292289679480")
HC_ANNOUNCE_CHANNEL_ID = os.getenv("HC_ANNOUNCE_CHANNEL_ID") or HC_ALLOWED_CHANNEL_ID

BNET_CLIENT_ID = os.getenv("BNET_CLIENT_ID")
BNET_CLIENT_SECRET = os.getenv("BNET_CLIENT_SECRET")

REGION = os.getenv("BNET_REGION", "us")
LOCALE = os.getenv("BNET_LOCALE", "en_US")
DEFAULT_REALM = os.getenv("HC_DEFAULT_REALM", "defias pillager")

POLL_INTERVAL_SECONDS = int(os.getenv("HC_POLL_INTERVAL_SECONDS", "300"))
DELAY_BETWEEN_PLAYERS_SECONDS = float(
    os.getenv("HC_DELAY_BETWEEN_PLAYERS_SECONDS", "1.0")
)

# Character/profile endpoints must use profile-* namespaces.
PROFILE_NAMESPACES = [
    # "profile-classicann-us",  # Anniversary / Dreamscythe TBC Anniversary
    "profile-classic1x-us",  # Classic Era / Hardcore Doomhowl, Defias Pillager
    # "profile-classic-us",     # Classic progression MoP
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def int_or_none(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def bool_to_db(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def safe_json_loads(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


def shorten(text: Optional[str], limit: int = 500) -> Optional[str]:
    if text is None:
        return None
    return text[:limit]


# =============================================================================
# SQLite
# =============================================================================


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing_columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()
    }
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl};")


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hc_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                discord_user_id TEXT NOT NULL,
                discord_display_name TEXT NOT NULL,

                character_name TEXT NOT NULL,
                realm TEXT NOT NULL,

                active INTEGER NOT NULL DEFAULT 1,

                last_level INTEGER,
                last_race TEXT,
                last_class TEXT,
                last_gender TEXT,
                last_is_ghost INTEGER,
                last_is_self_found INTEGER,
                last_namespace TEXT,
                last_api_status INTEGER,
                last_error TEXT,
                last_checked TEXT,
                last_professions_json TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # Safe migrations if you already created an older hc_players.db.
        ensure_column(conn, "hc_players", "last_level", "last_level INTEGER")
        ensure_column(conn, "hc_players", "last_race", "last_race TEXT")
        ensure_column(conn, "hc_players", "last_class", "last_class TEXT")
        ensure_column(conn, "hc_players", "last_gender", "last_gender TEXT")
        ensure_column(conn, "hc_players", "last_is_ghost", "last_is_ghost INTEGER")
        ensure_column(
            conn,
            "hc_players",
            "last_is_self_found",
            "last_is_self_found INTEGER",
        )
        ensure_column(conn, "hc_players", "last_namespace", "last_namespace TEXT")
        ensure_column(conn, "hc_players", "last_api_status", "last_api_status INTEGER")
        ensure_column(conn, "hc_players", "last_error", "last_error TEXT")
        ensure_column(conn, "hc_players", "last_checked", "last_checked TEXT")
        ensure_column(
            conn, "hc_players", "last_professions_json", "last_professions_json TEXT"
        )

        # Clean up any active duplicates created by an older version of the bot.
        # The newest active row is kept; older duplicate names are deactivated.
        active_rows = conn.execute(
            """
            SELECT id, character_name
            FROM hc_players
            WHERE active = 1
            ORDER BY updated_at DESC, id DESC;
            """
        ).fetchall()

        seen_character_names: set[str] = set()
        for row in active_rows:
            character_key = normalize(row["character_name"])
            if character_key in seen_character_names:
                conn.execute(
                    """
                    UPDATE hc_players
                    SET active = 0,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (utc_now(), row["id"]),
                )
            else:
                seen_character_names.add(character_key)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_hc_active ON hc_players(active);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_user ON hc_players(discord_user_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_char ON hc_players(character_name, realm);"
        )
        # Enforce one active row per normalized character name, regardless of realm.
        # Drop the older index definition first so this migration is repeatable.
        conn.execute("DROP INDEX IF EXISTS ux_hc_active_character_name;")
        conn.execute(
            """
            CREATE UNIQUE INDEX ux_hc_active_character_name
            ON hc_players(LOWER(REPLACE(TRIM(character_name), ' ', '-')))
            WHERE active = 1;
            """
        )


def load_active_players() -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE active = 1
            ORDER BY realm, character_name;
            """
        ).fetchall()


def find_active_players_by_user(discord_user_id: str) -> list[sqlite3.Row]:
    """Return every active Hardcore character registered to one Discord user."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE discord_user_id = ?
              AND active = 1
            ORDER BY created_at, realm, character_name;
            """,
            (discord_user_id,),
        ).fetchall()


def find_active_character(
    discord_user_id: str,
    character_name: str,
    realm: str,
) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE discord_user_id = ?
              AND character_name = ?
              AND realm = ?
              AND active = 1
            LIMIT 1;
            """,
            (discord_user_id, normalize(character_name), normalize(realm)),
        ).fetchone()


def register_player(
    discord_user_id: str,
    discord_display_name: str,
    character_name: str,
    realm: str,
) -> tuple[bool, str]:
    character_name = normalize(character_name)
    realm = normalize(realm)
    now = utc_now()

    with get_db() as conn:
        conn.execute(
            """
            UPDATE hc_players
            SET discord_display_name = ?,
                updated_at = ?
            WHERE discord_user_id = ?;
            """,
            (discord_display_name, now, discord_user_id),
        )

        # Character names are unique among active tracked characters.
        # The realm is intentionally not part of this check.
        existing_active = conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE LOWER(REPLACE(TRIM(character_name), ' ', '-')) = ?
              AND active = 1
            LIMIT 1;
            """,
            (character_name,),
        ).fetchone()

        if existing_active:
            return (
                False,
                f"`{character_name}` is already being tracked for "
                f"**{existing_active['discord_display_name']}**.",
            )

        existing_character = conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE discord_user_id = ?
              AND character_name = ? COLLATE NOCASE
              AND realm = ? COLLATE NOCASE
            ORDER BY updated_at DESC, id DESC
            LIMIT 1;
            """,
            (discord_user_id, character_name, realm),
        ).fetchone()

        if existing_character:
            # Re-enable the same character if it had previously been unregistered.
            # Do not erase its last-known level/death history.
            conn.execute(
                """
                UPDATE hc_players
                SET discord_display_name = ?,
                    active = 1,
                    updated_at = ?
                WHERE id = ?;
                """,
                (discord_display_name, now, existing_character["id"]),
            )
        else:
            # One Discord user may own many rows/characters.
            try:
                conn.execute(
                    """
                    INSERT INTO hc_players (
                        discord_user_id,
                        discord_display_name,
                        character_name,
                        realm,
                        active,
                        last_level,
                        last_race,
                        last_class,
                        last_gender,
                        last_is_ghost,
                        last_is_self_found,
                        last_namespace,
                        last_api_status,
                        last_error,
                        last_checked,
                        last_professions_json,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        ?, ?, ?, ?, 1,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                        ?, ?
                    );
                    """,
                    (
                        discord_user_id,
                        discord_display_name,
                        character_name,
                        realm,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Protect against two registrations happening at nearly the same time.
                return False, f"`{character_name}` is already being tracked."

    return (
        True,
        f"✅ Registered **{discord_display_name}** as **{character_name}** on **{realm}**.",
    )


def unregister_player(
    discord_user_id: str,
    character_name: str,
    realm: str,
) -> bool:
    """Stop tracking one character without removing the user's other characters."""
    with get_db() as conn:
        result = conn.execute(
            """
            UPDATE hc_players
            SET active = 0,
                updated_at = ?
            WHERE discord_user_id = ?
              AND character_name = ?
              AND realm = ?
              AND active = 1;
            """,
            (
                utc_now(),
                discord_user_id,
                normalize(character_name),
                normalize(realm),
            ),
        )
    return result.rowcount > 0


def update_player_success(
    player_id: int,
    profile: dict[str, Any],
    namespace: str,
) -> None:
    level = profile.get("level")
    race = profile.get("race", {}).get("name", "Unknown")
    char_class = profile.get("character_class", {}).get("name", "Unknown")
    gender = profile.get("gender", {}).get("name", "Unknown")
    is_ghost = profile.get("is_ghost", None)
    is_self_found = profile.get("is_self_found", None)
    api_name = normalize(profile.get("name", ""))

    professions_json = None

    with get_db() as conn:
        conn.execute(
            """
            UPDATE hc_players
            SET character_name = COALESCE(NULLIF(?, ''), character_name),
                last_level = ?,
                last_race = ?,
                last_class = ?,
                last_gender = ?,
                last_is_ghost = ?,
                last_is_self_found = ?,
                last_namespace = ?,
                last_api_status = 200,
                last_error = NULL,
                last_checked = ?,
                last_professions_json = COALESCE(?, last_professions_json),
                updated_at = ?
            WHERE id = ?;
            """,
            (
                api_name,
                level,
                race,
                char_class,
                gender,
                bool_to_db(is_ghost),
                bool_to_db(is_self_found),
                namespace,
                utc_now(),
                professions_json,
                utc_now(),
                player_id,
            ),
        )


def update_player_failure(
    player_id: int, status_code: int, error: Optional[str]
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE hc_players
            SET last_level = CASE
                    WHEN ? = 404 THEN NULL
                    ELSE last_level
                END,
                last_api_status = ?,
                last_error = ?,
                last_checked = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (
                status_code,
                status_code,
                shorten(error, 500),
                utc_now(),
                utc_now(),
                player_id,
            ),
        )


# =============================================================================
# Blizzard API
# =============================================================================


def get_access_token() -> str:
    if not BNET_CLIENT_ID or not BNET_CLIENT_SECRET:
        raise RuntimeError("Missing BNET_CLIENT_ID or BNET_CLIENT_SECRET.")

    response = requests.post(
        "https://oauth.battle.net/token",
        auth=(BNET_CLIENT_ID, BNET_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Token request failed: {response.status_code} {response.text[:500]}"
        )

    return response.json()["access_token"]


def blizzard_get(token: str, path: str, namespace: str) -> requests.Response:
    url = f"https://{REGION}.api.blizzard.com{path}"
    print(url)
    return requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"namespace": namespace, "locale": LOCALE},
        timeout=30,
    )


def get_character_profile(
    token: str, realm: str, character: str, namespace: str
) -> requests.Response:
    path = f"/profile/wow/character/{normalize(realm)}/{normalize(character)}"
    return blizzard_get(token, path, namespace)


def fetch_profile_any_namespace(
    token: str,
    realm: str,
    character: str,
) -> tuple[Optional[dict[str, Any]], Optional[str], int, Optional[str]]:
    for namespace in PROFILE_NAMESPACES:
        response = get_character_profile(token, realm, character, namespace)

        if response.status_code == 200:
            print(response.json())
            return response.json(), namespace, 200, None

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            return None, namespace, 429, f"Rate limited. Retry-After: {retry_after}"

        if response.status_code in (401, 403):
            return None, namespace, response.status_code, response.text[:500]

        # 404 means try next profile namespace.
        if response.status_code != 404:
            return None, namespace, response.status_code, response.text[:500]

    return None, None, 404, "Not found in any profile namespace."


# =============================================================================
# Discord message builders
# =============================================================================


def build_level_message(
    mention: str,
    display_name: str,
    character: str,
    realm: str,
    old_level: int,
    new_level: int,
    race: str,
    char_class: str,
) -> str:
    return (
        f"<@&1523790776130343146>\n"
        f"🎉 {mention} **leveled up!**\n"
        f"**{character}** on **{realm}** is now level **{new_level}**.\n"
        f"`{old_level} → {new_level}` | {race} {char_class}"
    )


def build_death_message(
    mention: str, display_name: str, character: str, realm: str, level: Optional[int]
) -> str:
    level_text = level if level is not None else "unknown"
    return (
        f"<@&1523790776130343146>\n"
        f"💀 **Hardcore death detected** 💀\n"
        f"**{mention}'s** character **{character}** on **{realm}** "
        f"is showing as ghost/dead.\n"
        f"Last known level: **{level_text}**\n"
        f"\n:saluting_face:"
    )


def build_first_seen_message(
    display_name: str,
    character: str,
    realm: str,
    level: Optional[int],
    race: str,
    char_class: str,
) -> str:
    level_text = level if level is not None else "unknown"
    return (
        f"📌 Now tracking **{display_name}** as **{character}** on **{realm}**.\n"
        f"Baseline: level **{level_text}** {race} {char_class}"
    )


# =============================================================================
# Poll cycle
# =============================================================================


def run_poll_cycle_sync(announce_first_seen: bool = False) -> list[str]:
    messages: list[str] = []
    players = load_active_players()

    if not players:
        print("Poller: no active players registered.")
        return messages

    print(f"Poller: checking {len(players)} registered players...")
    token = get_access_token()

    for player in players:
        player_id = player["id"]
        display_name = player["discord_display_name"]
        character = player["character_name"]
        realm = player["realm"]
        discord_user_id = player["discord_user_id"]
        mention = f"<@{discord_user_id}>"
        old_level = player["last_level"]
        old_is_ghost = player["last_is_ghost"]

        print(f"Checking {character} on {realm}...")

        profile, namespace, status_code, error = fetch_profile_any_namespace(
            token, realm, character
        )

        if profile is None:
            print(f"  API failed: {status_code} {error}")
            update_player_failure(player_id, status_code, error)
            time.sleep(DELAY_BETWEEN_PLAYERS_SECONDS)
            continue

        api_name = profile.get("name", character)
        new_level = profile.get("level")
        race = profile.get("race", {}).get("name", "Unknown")
        char_class = profile.get("character_class", {}).get("name", "Unknown")
        is_ghost = profile.get("is_ghost", None)

        print(
            f"  Found {api_name}: level {new_level} {race} {char_class} namespace={namespace}"
        )

        update_player_success(player_id, profile, namespace or "Unknown")

        if old_level is None:
            print("  Level baseline saved.")
            if announce_first_seen:
                messages.append(
                    build_first_seen_message(
                        display_name, api_name, realm, new_level, race, char_class
                    )
                )
        elif (
            isinstance(old_level, int)
            and isinstance(new_level, int)
            and new_level > old_level
        ):
            print(f"  LEVEL UP: {old_level} -> {new_level}")
            messages.append(
                build_level_message(
                    mention,
                    display_name,
                    api_name,
                    realm,
                    old_level,
                    new_level,
                    race,
                    char_class,
                )
            )
        else:
            print("  No level change.")

        if is_ghost is True and old_is_ghost != 1:
            print("  GHOST/DEAD detected.")
            messages.append(
                build_death_message(mention, display_name, api_name, realm, new_level)
            )

        time.sleep(DELAY_BETWEEN_PLAYERS_SECONDS)

    return messages


# =============================================================================
# Discord bot
# =============================================================================


class HCBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.poll_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        init_db()

        guild_id = int_or_none(DISCORD_GUILD_ID)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {guild_id}.")
        else:
            await self.tree.sync()
            print(
                "Slash commands synced globally. Global commands can take a while to appear."
            )

        if self.poll_task is None:
            self.poll_task = asyncio.create_task(poll_loop())


bot = HCBot()


def command_channel_allowed(interaction: discord.Interaction) -> bool:
    allowed_channel_id = int_or_none(HC_ALLOWED_CHANNEL_ID)
    if allowed_channel_id is None:
        return True
    return interaction.channel_id == allowed_channel_id


async def reject_wrong_channel(interaction: discord.Interaction) -> bool:
    if command_channel_allowed(interaction):
        return False

    await interaction.response.send_message(
        "Hardcore tracker commands are only allowed in the assigned challenge channel.",
        ephemeral=True,
    )
    return True


async def send_announcement(message: str) -> None:
    channel_id = int_or_none(HC_ANNOUNCE_CHANNEL_ID)

    if channel_id is None:
        print("No HC_ANNOUNCE_CHANNEL_ID or HC_ALLOWED_CHANNEL_ID set. Cannot post:")
        print(message)
        return

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)

        if not hasattr(channel, "send"):
            print("Announcement channel does not support sending messages.")
            return

        await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )

    except Exception as error:
        print(f"Failed to send announcement: {error}")


async def poll_loop() -> None:
    await bot.wait_until_ready()
    print(f"Poller starting. Interval: {POLL_INTERVAL_SECONDS} seconds.")
    await asyncio.sleep(10)

    while not bot.is_closed():
        try:
            messages = await asyncio.to_thread(run_poll_cycle_sync, False)
            for message in messages:
                await send_announcement(message)
        except Exception as error:
            print(f"Poller error: {error}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")
    print(f"DB: {DB_PATH}")
    print(f"Allowed channel: {HC_ALLOWED_CHANNEL_ID}")
    print(f"Announcement channel: {HC_ANNOUNCE_CHANNEL_ID}")


# =============================================================================
# Slash commands
# =============================================================================
@bot.tree.command(name="roleid", description="Get the ID for a Discord role.")
@app_commands.describe(role="Pick the role")
async def roleid(interaction: discord.Interaction, role: discord.Role) -> None:
    await interaction.response.send_message(
        f"Role: {role.mention}\n"
        f"Role name: `{role.name}`\n"
        f"Role ID: `{role.id}`\n\n"
        f"Windows cmd:\n"
        f'```bat\nset "HC_ROLE_ID={role.id}"\n```',
        ephemeral=True,
    )


@bot.tree.command(
    name="registerhc", description="Register your Hardcore Classic challenge character."
)
@app_commands.describe(
    character_name="Your Hardcore character name",
    realm="Realm name. Default is doomhowl.",
)
async def registerhc(
    interaction: discord.Interaction,
    character_name: str,
    realm: Optional[str] = DEFAULT_REALM,
) -> None:
    if await reject_wrong_channel(interaction):
        return

    character_name = normalize(character_name)
    realm = normalize(realm or DEFAULT_REALM)

    if not character_name:
        await interaction.response.send_message(
            "Character name cannot be blank.", ephemeral=True
        )
        return

    display_name = getattr(interaction.user, "display_name", interaction.user.name)
    success, message = register_player(
        discord_user_id=str(interaction.user.id),
        discord_display_name=display_name,
        character_name=character_name,
        realm=realm,
    )

    await interaction.response.send_message(
        message
        + (
            "\nThe tracker will start watching them on the next poll."
            if success
            else ""
        ),
        ephemeral=not success,
    )


@bot.tree.command(
    name="unregisterhc", description="Remove one Hardcore character from tracking."
)
@app_commands.describe(
    character_name="The Hardcore character to stop tracking",
    realm="Realm name",
)
async def unregisterhc(
    interaction: discord.Interaction,
    character_name: str,
    realm: Optional[str] = DEFAULT_REALM,
) -> None:
    if await reject_wrong_channel(interaction):
        return

    character_name = normalize(character_name)
    realm = normalize(realm or DEFAULT_REALM)
    removed = unregister_player(str(interaction.user.id), character_name, realm)

    if not removed:
        await interaction.response.send_message(
            f"You do not have an active character named **{character_name}** on **{realm}**.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🗑️ Stopped tracking **{character_name}** on **{realm}** for "
        f"**{interaction.user.display_name}**.",
        ephemeral=False,
    )


@bot.tree.command(
    name="myhc", description="Show all your registered Hardcore characters."
)
async def myhc(interaction: discord.Interaction) -> None:
    if await reject_wrong_channel(interaction):
        return

    rows = find_active_players_by_user(str(interaction.user.id))

    if not rows:
        await interaction.response.send_message(
            "You do not have any active Hardcore characters registered.",
            ephemeral=True,
        )
        return

    lines = [f"📌 **{interaction.user.display_name}'s Hardcore characters:**"]
    for row in rows:
        level = row["last_level"] if row["last_level"] is not None else "?"
        ghost_note = " 💀" if row["last_is_ghost"] == 1 else ""
        lines.append(
            f"• **{row['character_name']}** on **{row['realm']}** — "
            f"level **{level}**{ghost_note}"
        )

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="hclist", description="List Hardcore characters grouped by Discord member."
)
async def hclist(interaction: discord.Interaction) -> None:
    if await reject_wrong_channel(interaction):
        return

    rows = load_active_players()

    if not rows:
        await interaction.response.send_message(
            "No Hardcore characters are registered yet.", ephemeral=True
        )
        return

    # Group all character rows under their one Discord account.
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        user_id = row["discord_user_id"]
        group = grouped.setdefault(
            user_id,
            {
                "display_name": row["discord_display_name"],
                "characters": [],
            },
        )
        # Keep the newest stored Discord display name for every line in the group.
        group["display_name"] = row["discord_display_name"]
        group["characters"].append(row)

    lines: list[str] = []
    sorted_groups = sorted(
        grouped.values(),
        key=lambda group: str(group["display_name"]).lower(),
    )

    for group in sorted_groups:
        lines.append(f"**{group['display_name']}**")
        characters = sorted(
            group["characters"],
            key=lambda row: (row["realm"].lower(), row["character_name"].lower()),
        )

        for row in characters:
            level = row["last_level"] if row["last_level"] is not None else "?"
            race = row["last_race"] or "?"
            char_class = row["last_class"] or "?"
            gender = row["last_gender"] or "?"
            ghost = " 💀" if row["last_is_ghost"] == 1 else ""
            if row["last_is_self_found"] == 1:
                self_found = "✅"
            elif row["last_is_self_found"] == 0:
                self_found = "❌"
            else:
                self_found = "?"

            lines.append(
                f"  ↳ `{row['character_name']}` — "
                f"level **{level}** | {gender} {race} {char_class} | "
                f"Self-Found: {self_found}{ghost}"
            )

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1850] + "\n...list trimmed..."

    await interaction.response.send_message(message, ephemeral=False)


@bot.tree.command(
    name="pollhc", description="Manually run one Hardcore tracker poll now."
)
@app_commands.describe(
    announce_first_seen="If true, announce newly baselined characters too."
)
async def pollhc(
    interaction: discord.Interaction, announce_first_seen: Optional[bool] = False
) -> None:
    if await reject_wrong_channel(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    try:
        messages = await asyncio.to_thread(
            run_poll_cycle_sync, bool(announce_first_seen)
        )
        for message in messages:
            await send_announcement(message)

        await interaction.followup.send(
            f"Poll complete. Announcements sent: **{len(messages)}**.",
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(f"Poll failed: `{error}`", ephemeral=True)


@bot.tree.command(name="hcdebug", description="Show tracker debug/config info.")
async def hcdebug(interaction: discord.Interaction) -> None:
    if await reject_wrong_channel(interaction):
        return

    rows = load_active_players()

    await interaction.response.send_message(
        "```text\n"
        f"DB_PATH={DB_PATH}\n"
        f"REGION={REGION}\n"
        f"LOCALE={LOCALE}\n"
        f"DEFAULT_REALM={DEFAULT_REALM}\n"
        f"POLL_INTERVAL_SECONDS={POLL_INTERVAL_SECONDS}\n"
        f"DELAY_BETWEEN_PLAYERS_SECONDS={DELAY_BETWEEN_PLAYERS_SECONDS}\n"
        f"HC_ALLOWED_CHANNEL_ID={HC_ALLOWED_CHANNEL_ID}\n"
        f"HC_ANNOUNCE_CHANNEL_ID={HC_ANNOUNCE_CHANNEL_ID}\n"
        f"Registered active players={len(rows)}\n"
        "```",
        ephemeral=True,
    )


if __name__ == "__main__":
    missing = []

    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not BNET_CLIENT_ID:
        missing.append("BNET_CLIENT_ID")
    if not BNET_CLIENT_SECRET:
        missing.append("BNET_CLIENT_SECRET")

    if missing:
        print("Missing required environment variables:")
        for item in missing:
            print(f"- {item}")

        print()
        print("Windows cmd.exe example:")
        print('set "DISCORD_BOT_TOKEN=your_discord_bot_token"')
        print('set "BNET_CLIENT_ID=your_blizzard_client_id"')
        print('set "BNET_CLIENT_SECRET=your_blizzard_client_secret"')
        print('set "DISCORD_GUILD_ID=your_discord_server_id"')
        print('set "HC_ALLOWED_CHANNEL_ID=your_channel_id"')
        print('set "HC_ANNOUNCE_CHANNEL_ID=your_channel_id"')
        sys.exit(1)

    bot.run(DISCORD_BOT_TOKEN)
