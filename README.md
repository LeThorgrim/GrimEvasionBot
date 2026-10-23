# GrimEvasionBot

A Discord bot that proxies messages containing trigger words: it deletes the original message and reposts it via webhook using the author's name and avatar. Each server (guild) has its own word list and settings.

## Features

- Per-guild trigger word list and settings, stored in a SQLite database (`grimevasion.db`) via `aiosqlite`
- All commands grouped under a short `/ge` prefix, one level deep (`/ge add`, `/ge presets`, `/ge config`, etc.)
- Presets (default word lists), stored as `.json` files in `wordLists/`, that can be previewed and imported into any guild
- Per-guild toggles to also trigger on messages containing links (`links`) or attachments (`media`), independent of the word list
- Per-guild fuzzy detection (`fuzzy`), using `rapidfuzz`, that catches typos, leetspeak (`d0t`), doubled/repeated letters (`doooot`), and separator-stuffed spelling (`d.o.t`, `d-o-t`, `d o t`) even when the exact word isn't typed
- Per-guild exempt list (`exempt` setting): when a server enables it, members can opt themselves out of the proxy with `/ge exempt`
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

```
pip install -r requirements.txt
```

5. Create a `.env` file:

```
DISCORD_TOKEN=your_token_here
```

6. (Optional) Add default word lists as `.json` files in a `wordLists/` folder at the project root, formatted as:

```
[
  "word1",
  "word2"
]
```

7. Run the bot:

```
python bot.py
```

## Usage

🔒 = requires **Manage Server**.

**Help**
- `/ge help` — shows what the bot does and lists all commands.

**Trigger words**
- `/ge words page:<number>(optional)` — browse this server's trigger words, 20 per page, with Previous/Next buttons.
- `/ge add words:<word1, word2, ...>` 🔒 — add one or more trigger words, separated by commas. Any future message containing one of them (case-insensitive, whole word match) gets deleted and reposted under the author's identity. Words already present are reported and skipped.
- `/ge remove words:<word1, word2, ...>` 🔒 — remove one or more trigger words, separated by commas. Autocomplete suggests existing words for the piece you're currently typing.
- `/ge clear` 🔒 — remove **all** trigger words from this server. The bot first asks for confirmation with Yes/No buttons, and only the person who ran the command can answer; without an answer after 30 seconds, nothing is removed.

**Presets (default word lists)**
- `/ge presets` — list the presets available in `wordLists/`.
- `/ge presets name:<preset> page:<number>(optional)` — preview the content of a preset, paginated.
- `/ge import name:<preset>` 🔒 — import a preset's words into this server's trigger list, skipping words already present.

**Settings**
- `/ge config` — show this server's current settings.
- `/ge config setting:<links|media|fuzzy|exempt> value:<true/false>` 🔒 — enable or disable a setting. Without `value`, it just shows that setting's current state.

| Setting | Stored as | Effect |
|---|---|---|
| `links` | `doLinksTrigger` | also trigger on messages containing links |
| `media` | `doMediasTrigger` | also trigger on messages with attachments |
| `fuzzy` | `doFuzzyDetection` | catch typos, leetspeak and spaced-out words |
| `exempt` | `allowExemptList` | apply the exempt list |

**Personal**
- `/ge exempt value:<true/false>(optional)` — without `value`, toggles you on/off this server's exempt list; with it, forces the state. Anyone can use it, and the reply tells you whether the exempt list is currently applied on this server (see the `exempt` setting below).

## Notes

- Trigger words, per-guild settings and exempt users are stored in `grimevasion.db` (SQLite), created automatically on first run via `db.py`. It's gitignored since it may contain server-specific or sensitive data. New settings columns are added automatically to existing databases at startup.
- SQLite handles concurrent writes safely for a single bot process. If you ever shard across multiple processes/machines sharing the same data, move to a networked database instead.
- The `links`, `media`, `fuzzy` and `exempt` settings are all disabled by default; a message only needs to match one active condition (word, link, attachment, or fuzzy match) to be proxied.
- `fuzzy` covers both per-word fuzzy matching (typos, leetspeak, repeated letters) and separator-stuffed spelling (letters chained by spaces, `.`, `-`, or `_`, e.g. `d o t`) — both checks only run when the exact word match found nothing, to keep the common case cheap.
- Fuzzy matching normalizes words (lowercase, de-leetspeak, collapse repeated letters) before comparing. Genuine typo tolerance only applies to longer words (1 edit from 6 letters, 2 edits from 9 letters, tunable with the `FUZZY_*` constants at the top of `bot.py`); shorter words must match exactly once normalized, which avoids false positives such as `that` ~ `twat`. The separator-chain check only chains together complete short tokens (≤3 characters) — it can't fragment or merge longer words, so an ordinary sentence won't accidentally form a false match.
- The `exempt` setting controls whether the exempt list is applied. When it is **enabled**, messages from exempt members are ignored by the proxy entirely (not deleted, not reposted), even if they match a trigger. When it is **disabled**, `/ge exempt` still works and the list is still saved, but it has no effect (the command's reply says so); it starts applying as soon as the setting is enabled.
- If the bot replies to a message that gets deleted afterwards, the bot answer is not updated.
- When someone replies to a message the bot proxied, the preview mentions the real author rather than the webhook. This mapping is kept in memory only (last 5000 proxied messages), so after a restart the preview shows the name without a mention.