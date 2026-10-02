# GrimEvasionBot

A Discord bot that proxies messages containing trigger words: it deletes the original message and reposts it via webhook using the author's name and avatar. Each server (guild) has its own word list.

## TODO List

- Fizzy word detection (maybe true or false per guild)

## Features

- Per-guild trigger word list, stored in a SQLite database (`grimevasion.db`) via `aiosqlite`
- All commands grouped under `/grimevasion`, with nested subcommands (`/grimevasion word add`, `/grimevasion lists show`, etc.)
- Default word lists, stored as `.json` files in `wordLists/`, that can be previewed and imported into any guild
- Reposts via webhook, preserving attachments
- Works in threads
- If the proxied message is a reply, shows a small clickable preview linking to the original

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

```bash
pip install -r requirements.txt
```

5. Create a `.env` file:

```dotenv
DISCORD_TOKEN=your_token_here
```

6. (Optional) Add default word lists as `.json` files in a `wordLists/` folder at the project root, formatted as:

```json
[
  "word1",
  "word2"
]
```

7. Run the bot:

```bash
python bot.py
```

## Usage

- `/grimevasion info` — shows what the bot does and lists all commands.
- `/grimevasion word add word:<your word>` — add a trigger word to this server's list (requires **Manage Server**). Any future message containing that word (case-insensitive, whole word match) gets deleted and reposted under the author's identity.
- `/grimevasion word remove word:<your word>` — remove a trigger word from this server's list (requires **Manage Server**), with autocomplete suggesting existing words.
- `/grimevasion list page:<number>(optional)` — browse this server's trigger words, 20 per page, with Previous/Next buttons.
- `/grimevasion lists list` — list the default word lists available in `wordLists/`.
- `/grimevasion lists show name:<list name> page:<number>(optional)` — preview the content of a specific default list, paginated.
- `/grimevasion lists add name:<list name>` — import a default list's words into this server's trigger list (requires **Manage Server**), skipping words already present.

## Notes

- Trigger words are stored in `grimevasion.db` (SQLite), created automatically on first run via `db.py`. It's gitignored since it may contain server-specific or sensitive words.
- SQLite handles concurrent writes safely for a single bot process. If you ever shard across multiple processes/machines sharing the same data, move to a networked database instead.
- If the bot replies to a message that gets deleted afterwards, the bot answer is not updated.