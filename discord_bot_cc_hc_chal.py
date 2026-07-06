import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks


# =========================
# Config
# =========================

DB_PATH = os.getenv("HC_DB_PATH", "hc_players.db")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

ALLOWED_CHANNEL_ID = os.getenv("HC_ALLOWED_CHANNEL_ID", "musk-api-testing")
ANNOUNCE_CHANNEL_ID = os.getenv("HC_ANNOUNCE_CHANNEL_ID") or ALLOWED_CHANNEL_ID

BNET_CLIENT_ID = os.getenv("BNET_CLIENT_ID")
BNET_CLIENT_SECRET = os.getenv("BNET_CLIENT_SECRET")

REGION = "us"
LOCALE = "en_US"
DEFAULT_REALM = "doomhowl"

POLL_INTERVAL_SECONDS = int(os.getenv("HC_POLL_INTERVAL_SECONDS", "300"))

PROFILE_NAMESPACES = [
    "profile-classicann-us",
    "profile-classic1x-us",
    "profile-classic-us",
]


# =========================
# Helpers
# =========================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: str) -> str:
    return value.strip().lower()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hc_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                discord_user_id TEXT NOT NULL UNIQUE,
                discord_display_name TEXT NOT NULL,

                character_name TEXT NOT NULL,
                realm TEXT NOT NULL,

                active INTEGER NOT NULL DEFAULT 1,

                last_level INTEGER,
                last_is_ghost INTEGER,
                last_namespace TEXT,
                last_api_status INTEGER,
                last_error TEXT,
                last_checked TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def int_env(value: Optional[str]) -> Optional[int]:
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def channel_allowed(interaction: discord.Interaction) -> bool:
    allowed_channel = int_env(ALLOWED_CHANNEL_ID)

    if allowed_channel is None:
        return True

    return interaction.channel_id == allowed_channel


async def reject_wrong_channel(interaction: discord.Interaction) -> bool:
    if channel_allowed(interaction):
        return False

    await interaction.response.send_message(
        "Hardcore registration commands are only allowed in the assigned challenge channel.",
        ephemeral=True,
    )
    return True


# =========================
# Blizzard API
# =========================

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


def get_character_profile(
    token: str,
    realm: str,
    character: str,
    namespace: str,
) -> requests.Response:
    realm = normalize(realm)
    character = normalize(character)

    url = f"https://{REGION}.api.blizzard.com/profile/wow/character/{realm}/{character}"

    headers = {
        "Authorization": f"Bearer {token}",
    }

    params = {
        "namespace": namespace,
        "locale": LOCALE,
    }

    return requests.get(url, headers=headers, params=params, timeout=30)


def fetch_profile_any_namespace(
    token: str,
    realm: str,
    character: str,
) -> tuple[Optional[dict], Optional[str], int, Optional[str]]:
    for namespace in PROFILE_NAMESPACES:
        response = get_character_profile(
            token=token,
            realm=realm,
            character=character,
            namespace=namespace,
        )

        if response.status_code == 200:
            return response.json(), namespace, 200, None

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            return None, namespace, 429, f"Rate limited. Retry-After: {retry_after}"

        if response.status_code in (401, 403):
            return None, namespace, response.status_code, response.text[:500]

        # 404 means try the next profile namespace.
        if response.status_code != 404:
            return None, namespace, response.status_code, response.text[:500]

    return None, None, 404, "Not found in any profile namespace."


# =========================
# Poller DB logic
# =========================

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


def update_player_success(
    player_id: int,
    profile: dict,
    namespace: str,
) -> None:
    level = profile.get("level")
    is_ghost = profile.get("is_ghost", None)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE hc_players
            SET character_name = ?,
                last_level = ?,
                last_is_ghost = ?,
                last_namespace = ?,
                last_api_status = 200,
                last_error = NULL,
                last_checked = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (
                normalize(profile.get("name", "")),
                level,
                None if is_ghost is None else int(bool(is_ghost)),
                namespace,
                utc_now(),
                utc_now(),
                player_id,
            ),
        )


def update_player_failure(
    player_id: int,
    status_code: int,
    error: Optional[str],
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE hc_players
            SET last_api_status = ?,
                last_error = ?,
                last_checked = ?,
                updated_at = ?
            WHERE id = ?;
            """,
            (
                status_code,
                error,
                utc_now(),
                utc_now(),
                player_id,
            ),
        )


def build_level_message(
    display_name: str,
    character: str,
    realm: str,
    old_level: int,
    new_level: int,
    race: str,
    char_class: str,
) -> str:
    return (
        f"🎉 **{display_name}'s Hardcore character leveled up!**\n"
        f"**{character}** on **{realm}** is now level **{new_level}**.\n"
        f"`{old_level} → {new_level}` | {race} {char_class}"
    )


def build_death_message(
    display_name: str,
    character: str,
    realm: str,
    level: Optional[int],
) -> str:
    level_text = level if level is not None else "unknown"

    return (
        f"💀 **Possible Hardcore death detected.**\n"
        f"**{display_name}'s** character **{character}** on **{realm}** "
        f"is showing as ghost/dead in the API.\n"
        f"Last known level: **{level_text}**"
    )


def run_poll_cycle_sync() -> list[str]:
    """
    Runs in a background thread.
    Returns Discord messages to send.
    """
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

        old_level = player["last_level"]
        old_is_ghost = player["last_is_ghost"]

        print(f"Checking {character} on {realm}...")

        profile, namespace, status_code, error = fetch_profile_any_namespace(
            token=token,
            realm=realm,
            character=character,
        )

        if profile is None:
            print(f"  API failed: {status_code} {error}")
            update_player_failure(player_id, status_code, error)
            continue

        new_level = profile.get("level")
        api_name = profile.get("name", character)
        race = profile.get("race", {}).get("name", "Unknown")
        char_class = profile.get("character_class", {}).get("name", "Unknown")
        is_ghost = profile.get("is_ghost", None)

        update_player_success(player_id, profile, namespace or "Unknown")

        print(
            f"  Found {api_name}: level {new_level} "
            f"{race} {char_class} namespace={namespace}"
        )

        # First successful check becomes baseline.
        if old_level is None:
            print("  Baseline saved.")
            continue

        if isinstance(old_level, int) and isinstance(new_level, int) and new_level > old_level:
            messages.append(
                build_level_message(
                    display_name=display_name,
                    character=api_name,
                    realm=realm,
                    old_level=old_level,
                    new_level=new_level,
                    race=race,
                    char_class=char_class,
                )
            )

        if is_ghost is True and old_is_ghost != 1:
            messages.append(
                build_death_message(
                    display_name=display_name,
                    character=api_name,
                    realm=realm,
                    level=new_level,
                )
            )

    return messages


# =========================
# Discord Bot
# =========================

class HCBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()

        guild_id = int_env(DISCORD_GUILD_ID)

        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {guild_id}.")
        else:
            await self.tree.sync()
            print("Slash commands synced globally. This can take a while to appear.")

        if not poll_players_loop.is_running():
            poll_players_loop.start()


bot = HCBot()


async def send_announcement(message: str) -> None:
    channel_id = int_env(ANNOUNCE_CHANNEL_ID)

    if channel_id is None:
        print("No HC_ANNOUNCE_CHANNEL_ID or HC_ALLOWED_CHANNEL_ID set. Cannot post:")
        print(message)
        return

    channel = bot.get_channel(channel_id)

    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    if not hasattr(channel, "send"):
        print("Announcement channel does not support sending messages.")
        return

    await channel.send(message)


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_players_loop():
    try:
        messages = await asyncio.to_thread(run_poll_cycle_sync)

        for message in messages:
            await send_announcement(message)

    except Exception as error:
        print(f"Poller error: {error}")


@poll_players_loop.before_loop
async def before_poll_players_loop():
    await bot.wait_until_ready()
    print(f"Poller starting. Interval: {POLL_INTERVAL_SECONDS} seconds.")
    await asyncio.sleep(10)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"DB: {DB_PATH}")
    print(f"Allowed channel: {ALLOWED_CHANNEL_ID}")
    print(f"Announcement channel: {ANNOUNCE_CHANNEL_ID}")


# =========================
# Slash Commands
# =========================

@bot.tree.command(name="registerhc", description="Register your Hardcore Classic challenge character.")
@app_commands.describe(
    character_name="Your Hardcore character name",
    realm="Realm name. Default is doomhowl.",
)
async def registerhc(
    interaction: discord.Interaction,
    character_name: str,
    realm: Optional[str] = DEFAULT_REALM,
):
    if await reject_wrong_channel(interaction):
        return

    character_name = normalize(character_name)
    realm = normalize(realm or DEFAULT_REALM)

    if not character_name:
        await interaction.response.send_message("Character name cannot be blank.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    display_name = getattr(interaction.user, "display_name", interaction.user.name)

    with get_db() as conn:
        existing = conn.execute(
            """
            SELECT discord_user_id, discord_display_name
            FROM hc_players
            WHERE character_name = ?
              AND realm = ?
              AND active = 1
              AND discord_user_id != ?;
            """,
            (character_name, realm, user_id),
        ).fetchone()

        if existing:
            await interaction.response.send_message(
                f"`{character_name}` on `{realm}` is already registered by "
                f"**{existing['discord_display_name']}**.",
                ephemeral=True,
            )
            return

        now = utc_now()

        conn.execute(
            """
            INSERT INTO hc_players (
                discord_user_id,
                discord_display_name,
                character_name,
                realm,
                active,
                last_level,
                last_is_ghost,
                last_namespace,
                last_api_status,
                last_error,
                last_checked,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(discord_user_id)
            DO UPDATE SET
                discord_display_name = excluded.discord_display_name,
                character_name = excluded.character_name,
                realm = excluded.realm,
                active = 1,
                last_level = NULL,
                last_is_ghost = NULL,
                last_namespace = NULL,
                last_api_status = NULL,
                last_error = NULL,
                last_checked = NULL,
                updated_at = excluded.updated_at;
            """,
            (
                user_id,
                display_name,
                character_name,
                realm,
                now,
                now,
            ),
        )

    await interaction.response.send_message(
        f"✅ Registered **{display_name}** as **{character_name}** on **{realm}**.\n"
        f"The tracker will start watching them on the next poll.",
        ephemeral=False,
    )


@bot.tree.command(name="unregisterhc", description="Remove your Hardcore character from tracking.")
async def unregisterhc(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    user_id = str(interaction.user.id)

    with get_db() as conn:
        result = conn.execute(
            """
            UPDATE hc_players
            SET active = 0,
                updated_at = ?
            WHERE discord_user_id = ?
              AND active = 1;
            """,
            (utc_now(), user_id),
        )

    if result.rowcount == 0:
        await interaction.response.send_message(
            "You do not have an active Hardcore character registered.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🗑️ Removed **{interaction.user.display_name}** from Hardcore tracking.",
        ephemeral=False,
    )


@bot.tree.command(name="myhc", description="Show your registered Hardcore character.")
async def myhc(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    user_id = str(interaction.user.id)

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE discord_user_id = ?
              AND active = 1;
            """,
            (user_id,),
        ).fetchone()

    if not row:
        await interaction.response.send_message(
            "You do not have an active Hardcore character registered.",
            ephemeral=True,
        )
        return

    level = row["last_level"] if row["last_level"] is not None else "unknown"
    api_status = row["last_api_status"] if row["last_api_status"] is not None else "not checked yet"

    await interaction.response.send_message(
        f"📌 You are registered as **{row['character_name']}** on **{row['realm']}**.\n"
        f"Last known level: **{level}**\n"
        f"API status: `{api_status}`",
        ephemeral=True,
    )


@bot.tree.command(name="hclist", description="List registered Hardcore challenge characters.")
async def hclist(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM hc_players
            WHERE active = 1
            ORDER BY realm, character_name;
            """
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "No Hardcore characters are registered yet.",
            ephemeral=True,
        )
        return

    lines = []

    for row in rows:
        level = row["last_level"] if row["last_level"] is not None else "?"
        status = row["last_api_status"] if row["last_api_status"] is not None else "not checked"
        ghost = ""

        if row["last_is_ghost"] == 1:
            ghost = " 💀"

        lines.append(
            f"**{row['discord_display_name']}** — "
            f"`{row['character_name']}` on `{row['realm']}` "
            f"level **{level}**{ghost} "
            f"`API: {status}`"
        )

    message = "\n".join(lines)

    if len(message) > 1900:
        message = message[:1850] + "\n...list trimmed..."

    await interaction.response.send_message(message, ephemeral=False)


@bot.tree.command(name="pollhc", description="Manually run one Hardcore tracker poll now.")
async def pollhc(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    try:
        messages = await asyncio.to_thread(run_poll_cycle_sync)

        for message in messages:
            await send_announcement(message)

        await interaction.followup.send(
            f"Poll complete. Announcements sent: **{len(messages)}**.",
            ephemeral=True,
        )

    except Exception as error:
        await interaction.followup.send(
            f"Poll failed: `{error}`",
            ephemeral=True,
        )


# =========================
# Main
# =========================

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
        sys.exit(1)

    bot.run(DISCORD_BOT_TOKEN)