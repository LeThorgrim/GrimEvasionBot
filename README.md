# GrimEvasionBot

A Discord bot that proxies messages containing trigger words: it deletes the original message and reposts it via webhook using the author's name and avatar. Each server (guild) has its own word list.

## TODO List

- List the banned words of the guild (page system ?)
- Fizzy word detection (maybe true or false per guild)
- Set per guild the required permission to manage the bot
- Implement an auto-deployed DB
- Set up a default list that can be added w a command (and infos w an other)

## Features

- Per-guild trigger word list, stored in `words.json`
- `/grimproxywordadd` slash command to add words (requires **Manage Server** permission)
- Reposts via webhook, preserving attachments
- Works in threads

## Setup

1. Create a bot application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent** under Bot settings.
3. Invite the bot with the `bot` and `applications.commands` scopes, granting:
   - Manage Webhooks
   - Manage Messages
   - View Channels
   - Send Messages
   - Attach Files
4. Clone this repo and install dependencies:
5. Create a `.env` file:

```dotenv
DISCORD_TOKEN=your_token_here
```

6. Run the bot:

```bash
python bot.py
```

## Usage

Run `/grimproxywordadd word:<your word>` in any server the bot is in. Any future message containing that word (case-insensitive, whole word match) gets deleted and reposted under the author's identity.

## Notes

- `words.json` is created automatically and stores each guild's word list. It's gitignored since it may contain server-specific or sensitive words.
- `words.json` as a flat file isn't safe for concurrent writes across multiple bot instances (use a real database if you scale beyond a single process).
- If the bot replies to a message that gets deleted afterwards, it is not updated.