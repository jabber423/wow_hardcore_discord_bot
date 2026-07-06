# Hardcore poll bot

## Get the bot token

Go to the Discord Developer Portal.
Click New Application.
Name it something like Crit Commanders HC Tracker.
Open the application.
Go to Bot on the left.
Click Add Bot if there isn’t one already.
Under Token, click Reset Token or View Token, then copy it.

That token is what you set here:

set "DISCORD_BOT_TOKEN=PASTE_BOT_TOKEN_HERE"


## Invite the bot to your server

In the Developer Portal:

Go to your application.
Go to OAuth2 / Installation.
Add scopes:
bot
applications.commands
Bot permissions:
Send Messages
Use Slash Commands
Read Message History is optional here
Copy the generated install/invite link.
Open it in a new browser window and add the bot to your Discord server.


## Get the guild ID / server ID

Open Discord via the browser, it's the first number in the url

-Or-

First enable Developer Mode:

Discord Settings → Advanced → Developer Mode → On

Then:

Right-click your server icon.
Click Copy Server ID.

That number is your Guild ID. Discord’s support docs say Server ID/Guild ID is copied by right-clicking the server icon and choosing Copy ID, with Developer Mode enabled.

Set it like this:

set "DISCORD_GUILD_ID=PASTE_SERVER_ID_HERE"


## Get the channel ID

Right-click the channel where people should register.
Click Copy Channel ID.

Then set both of these to that same channel for now:

set "HC_ALLOWED_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"
set "HC_ANNOUNCE_CHANNEL_ID=PASTE_CHANNEL_ID_HERE"