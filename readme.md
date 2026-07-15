# Hardcore Poll Bot

A Discord bot that tracks World of Warcraft Hardcore characters using the Blizzard API.

## Quick Start

1. Install the required Python packages:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Configure the required environment variables:

   * `DISCORD_BOT_TOKEN`
   * `DISCORD_GUILD_ID`
   * `HC_ALLOWED_CHANNEL_ID`
   * `HC_ANNOUNCE_CHANNEL_ID`
   * `BNET_CLIENT_ID`
   * `BNET_CLIENT_SECRET`

3. Run the bot:

   ```bash
   python hc_bot_tracker.py
   ```

Replace `hc_bot_tracker.py` with the name of the bot's Python file.

---

## Discord Setup

### 1. Create a Discord Application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**.
3. Enter a name, such as `Crit Commanders HC Tracker`.
4. Open the newly created application.

### 2. Create the Bot and Get Its Token

1. Select **Bot** from the menu on the left.
2. Click **Add Bot** if the application does not already have one.
3. Under **Token**, click **Reset Token** or **View Token**.
4. Copy the token.

Set the token as an environment variable:

```bat
set "DISCORD_BOT_TOKEN=PASTE_BOT_TOKEN_HERE"
```

> Keep your bot token private. Anyone with this token can control your bot.

### 3. Invite the Bot to Your Discord Server

In the Discord Developer Portal:

1. Open your application.
2. Go to **OAuth2 → Installation**.
3. Add the following scopes:

   * `bot`
   * `applications.commands`
4. Grant the following bot permissions:

   * Send Messages
   * Use Application Commands
   * Read Message History, if needed
5. Copy the generated installation link.
6. Open the link in a browser and add the bot to your Discord server.

---

## Get the Discord Server ID

The Discord server ID is also called the guild ID.

### Browser Method

When Discord is open in a browser, the server ID is the first number in the server URL.

### Developer Mode Method

First, enable Developer Mode:

**Discord Settings → Advanced → Developer Mode → On**

Then:

1. Right-click the server icon.
2. Select **Copy Server ID** or **Copy ID**.

Set the environment variable:

```bat
set "DISCORD_GUILD_ID=PASTE_SERVER_ID_HERE"
```

---

## Get the Discord Channel IDs

Right-click the channel where users should register their Hardcore characters, then select **Copy Channel ID**.

Set the registration channel:

```bat
set "HC_ALLOWED_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"
```

Set the channel where the bot should post announcements:

```bat
set "HC_ANNOUNCE_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"
```

The same channel ID can be used for both settings.

---

## Blizzard API Setup

### 1. Create a Blizzard Developer Account

Open the [Battle.net Developer Portal](https://community.developer.battle.net/) and sign in with your Battle.net account.

### 2. Create an API Client

Create a new API client to receive:

* Client ID
* Client Secret

Set them as environment variables:

```bat
set "BNET_CLIENT_ID=PASTE_CLIENT_ID_HERE"
set "BNET_CLIENT_SECRET=PASTE_CLIENT_SECRET_HERE"
```

Keep the client secret private.

---

## Complete Windows Command Prompt Example

```bat
set "DISCORD_BOT_TOKEN=PASTE_BOT_TOKEN_HERE"
set "DISCORD_GUILD_ID=PASTE_SERVER_ID_HERE"
set "HC_ALLOWED_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"
set "HC_ANNOUNCE_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"
set "BNET_CLIENT_ID=PASTE_CLIENT_ID_HERE"
set "BNET_CLIENT_SECRET=PASTE_CLIENT_SECRET_HERE"

python -m pip install -r requirements.txt
python your_bot_script.py
```

Environment variables set with `set` only remain available in the current Command Prompt window. You must set them again after opening a new window unless you configure them permanently.
