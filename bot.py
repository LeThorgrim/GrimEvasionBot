import os
import re
import json
import asyncio
from collections import OrderedDict

import discord
from discord import app_commands
from dotenv import load_dotenv
from rapidfuzz.distance import Levenshtein

import db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found: check your .env file")

WORD_LISTS_DIR = "wordLists"  # folder containing default word list .json files
DELETE_DELAY = 0.01  # seconds to wait before deleting the original message (local display for users is bugged when too low)
PREVIEW_LENGTH = 80  # max characters shown from the replied-to message
WORDS_PER_PAGE = 20  # max words shown per page in /gu words and /gu presets
PROXY_AUTHORS_MAX = 5000  # max remembered proxied messages (keeps memory bounded)
MESSAGE_LIMIT_MAX = 500  # highest value accepted by /gu limit (each cleanup re-reads up to this many messages)
LIMIT_CLEANUP_DELAY = 5.0  # seconds to wait after a new message before cleaning a limited channel (groups bursts)

# Fuzzy typo tolerance (edit distance) depends on the NORMALIZED word length. Shorter words only
# match exactly once normalized (leetspeak, repeated letters, separators), because one typo on a
# 4-5 letter word turns far too many everyday words into false positives ("that" ~ "twat", "pardo" ~ "paedo").
FUZZY_ONE_EDIT_MIN_LENGTH = 6   # from this length: 1 edit tolerated
FUZZY_TWO_EDITS_MIN_LENGTH = 9  # from this length: 2 edits tolerated
FUZZY_MIN_NORMALIZED_LENGTH = 2  # collapsing repeated letters never shrinks a word below this length

# Stops at "|" so a spoilered link "||https://...||" keeps its closing bars when replaced by <URL>
LINK_PATTERN = re.compile(r"(https?://|www\.)[^\s|]+", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"[a-z0-9@$!]+")  # word-like clusters, leetspeak chars included

# Catches evasion by separator-stuffing: "m.o.t", "m-o-t", "m_o_t", "m o t".
# Each piece must be a COMPLETE short (<=3 char) token — the lookarounds ensure it isn't
# immediately preceded/followed by another word character, so longer words (e.g. "check",
# "this") can never be sliced into fragments and accidentally chained together.
_CHAIN_PIECE = r"(?<![a-z0-9@$!])[a-z0-9@$!]{1,3}(?![a-z0-9@$!])"
SEPARATOR_CHAIN_PATTERN = re.compile(
    rf"{_CHAIN_PIECE}(?:[ \-_.]+{_CHAIN_PIECE}){{1,}}", re.IGNORECASE
)
SEPARATOR_STRIP_PATTERN = re.compile(r"[ \-_.]+")
SETTINGS_PARAMETERS = ["doLinksTrigger", "doMediasTrigger", "doFuzzyDetection", "allowExemptList"]  # DB column names
# Short names shown in /gu config -> DB column names
SETTINGS_ALIASES = {
    "links": "doLinksTrigger",
    "media": "doMediasTrigger",
    "fuzzy": "doFuzzyDetection",
    "exempt": "allowExemptList",
}
SETTINGS_DESCRIPTIONS = {
    "links": "trigger on messages containing links",
    "media": "trigger on messages with attachments",
    "fuzzy": "catch typos, leetspeak and spaced-out words",
    "exempt": "apply the exempt list",
}

# Leetspeak -> letter substitutions applied before fuzzy comparison
LEETSPEAK_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i",
})

guild_patterns: dict[str, re.Pattern] = {}       # cache: guild_id -> compiled regex (exact match)
guild_normalized_words: dict[str, list[str]] = {}  # cache: guild_id -> normalized trigger words (for fuzzy match)
guild_settings: dict[str, dict] = {}              # cache: guild_id -> settings dict
guild_exempt: dict[str, set[str]] = {}            # cache: guild_id -> user ids (as str) on the exempt list
channel_limits: dict[str, dict] = {}              # cache: channel_id -> {"guild_id", "enabled", "max"}


def build_pattern(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE)


def normalize_word(word: str) -> str:
    """Lowercase, de-leetspeak, strip non-alphanumerics, and collapse repeated letters."""
    word = word.lower().translate(LEETSPEAK_MAP)
    word = re.sub(r"[^a-z0-9]", "", word)
    collapsed = re.sub(r"(.)\1+", r"\1", word)  # "moooot" -> "mot", "mott" -> "mot"
    # Collapsing must not shrink a word to a single letter: "kkk" / "xxx" would become "k" / "x" and
    # then match every lone "k" or "x" typed in chat. In that case keep the uncollapsed form.
    return collapsed if len(collapsed) >= FUZZY_MIN_NORMALIZED_LENGTH else word


def fuzzy_tolerance(length: int) -> int:
    """Max edit distance allowed for a normalized word of this length.
    0 = only an exact match after normalization counts (too short, typo tolerance is too risky)."""
    if length >= FUZZY_TWO_EDITS_MIN_LENGTH:
        return 2
    if length >= FUZZY_ONE_EDIT_MIN_LENGTH:
        return 1
    return 0


def fuzzy_match(token: str, normalized_triggers: list[str]) -> bool:
    norm_token = normalize_word(token)
    if not norm_token:
        return False

    for trigger_norm in normalized_triggers:
        if not trigger_norm:
            continue
        if norm_token == trigger_norm:
            # Identical once leetspeak/separators/repeated letters are normalized away
            # (e.g. "moooot", "m0t", "mott" vs "mot") — always counts, even for short words.
            return True
        allowed = min(fuzzy_tolerance(len(norm_token)), fuzzy_tolerance(len(trigger_norm)))
        if allowed == 0:
            continue  # too short for genuine typo tolerance beyond exact normalized match
        if abs(len(trigger_norm) - len(norm_token)) > allowed:
            continue  # cheap length filter before the actual distance computation
        if Levenshtein.distance(norm_token, trigger_norm) <= allowed:
            return True
    return False


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def extract_separator_chains(text: str) -> list[str]:
    """Find runs of short tokens chained by spaces/./-/_ (e.g. 'm.o.t', 'm_o_t', 'm o t')
    and return each chain with the separators stripped out, as one candidate string per chain."""
    chains = []
    for match in SEPARATOR_CHAIN_PATTERN.finditer(text.lower()):
        merged = SEPARATOR_STRIP_PATTERN.sub("", match.group(0))
        chains.append(merged)
    return chains


async def refresh_pattern(guild_id: str) -> None:
    words = await db.get_words(guild_id)
    guild_patterns[guild_id] = build_pattern(words)
    guild_normalized_words[guild_id] = [normalize_word(w) for w in words]


async def refresh_settings(guild_id: str) -> None:
    guild_settings[guild_id] = await db.get_settings(guild_id)


async def refresh_exempt(guild_id: str) -> None:
    guild_exempt[guild_id] = set(await db.get_exempt_users(guild_id))


def get_cached_settings(guild_id: str) -> dict:
    return guild_settings.get(guild_id, dict(db.DEFAULT_SETTINGS))


def list_default_word_lists() -> list[str]:
    """Return the names (without .json) of default word list files available."""
    if not os.path.isdir(WORD_LISTS_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(WORD_LISTS_DIR)
        if f.endswith(".json") and os.path.isfile(os.path.join(WORD_LISTS_DIR, f))
    )


def load_default_word_list(name: str) -> list[str] | None:
    """Load a default word list by name (without .json). Returns None if not found/invalid."""
    path = os.path.join(WORD_LISTS_DIR, f"{name}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, list) or not all(isinstance(w, str) for w in data):
        return None
    return [w.strip().lower() for w in data if w.strip()]


intents = discord.Intents.default()
intents.message_content = True


class ProxyClient(discord.Client):
    def __init__(self):
        super().__init__(
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name="/gu help"),
            status=discord.Status.online,
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init_db()
        await self.tree.sync()


client = ProxyClient()

webhooks: dict[int, discord.Webhook] = {}  # cache: channel id -> webhook

# proxied (webhook) message id -> id of the REAL author who wrote it.
# Discord only exposes the webhook as the author of these messages, so we remember
# who was behind each one to be able to mention the right person in replies.
# In-memory only: entries are lost on restart (replies then fall back to a plain name).
proxy_authors: "OrderedDict[int, int]" = OrderedDict()


def remember_proxy_author(message_id: int, author_id: int) -> None:
    proxy_authors[message_id] = author_id
    while len(proxy_authors) > PROXY_AUTHORS_MAX:
        proxy_authors.popitem(last=False)  # drop the oldest entry


def strip_reply_prefix(content: str) -> str:
    """Remove the '-# ↱ Replying to ...' header our own proxy adds, so a reply to a
    proxied reply previews the actual text and not the previous header."""
    if not content.startswith("-# ↱"):
        return content
    lines = content.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith("-# "):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


def spoiler_left_open(text: str) -> bool:
    """True if a || spoiler is still open at the end of text.
    Ignores escaped pipes (\\||) and pipes inside `inline code`, which Discord doesn't treat as spoilers."""
    i, is_open, n = 0, False, len(text)
    while i < n:
        c = text[i]
        if c == "\\":  # escaped character: skip it
            i += 2
            continue
        if c == "`":  # inline code span: skip to its matching closing backticks
            j = i
            while j < n and text[j] == "`":
                j += 1
            end = text.find(text[i:j], j)
            i = end + (j - i) if end != -1 else j
            continue
        if text.startswith("||", i):
            is_open = not is_open
            i += 2
            continue
        i += 1
    return is_open


def truncate_preview(text: str, limit: int) -> str:
    """Truncate a reply preview without ever leaving a || spoiler open
    (an unclosed spoiler would render its content in plain sight)."""
    if len(text) > limit:
        # rstrip avoids leaving a lone "|" (half of a "||") or a dangling "\" escape
        text = text[:limit - 3].rstrip("|\\") + "..."
    if spoiler_left_open(text):
        text += "||"
    return text


async def get_webhook(channel: discord.abc.GuildChannel) -> discord.Webhook:
    if channel.id in webhooks:
        return webhooks[channel.id]
    for hook in await channel.webhooks():
        if hook.user == client.user:  # only webhooks created by this bot
            webhooks[channel.id] = hook
            return hook
    hook = await channel.create_webhook(name="proxy")
    webhooks[channel.id] = hook
    return hook


async def build_reply_prefix(msg: discord.Message) -> str:
    """Return a small subtext preview line if msg is a reply, else an empty string."""
    if not msg.reference or not msg.reference.message_id:
        return ""

    try:
        replied = msg.reference.resolved
        if replied is None:
            replied = await msg.channel.fetch_message(msg.reference.message_id)
    except (discord.NotFound, discord.HTTPException):
        return "-# ↱ *Replying to a deleted message*\n\n"

    if isinstance(replied, discord.DeletedReferencedMessage):
        return "-# ↱ *Replying to a deleted message*\n\n"

    # Webhook messages: the "author" is the webhook, not the real person.
    # Look up the real author we remembered; otherwise fall back to a plain name (no ping).
    if replied.webhook_id is not None:
        real_author_id = proxy_authors.get(replied.id)
        if real_author_id:
            author_label = f"<@{real_author_id}>"
        else:
            author_label = f"**{replied.author.display_name}**"
        raw_content = strip_reply_prefix(replied.content)
    else:
        author_label = f"<@{replied.author.id}>"
        raw_content = replied.content

    preview = raw_content.replace("\n", " ").strip()
    preview = LINK_PATTERN.sub("<URL>", preview)
    preview = truncate_preview(preview, PREVIEW_LENGTH)
    if not preview:
        if any(a.is_spoiler() for a in replied.attachments):
            preview = "*[spoiler attachment]*"
        else:
            preview = "*[attachment/embed]*"

    # "-# " renders as small subtext in Discord; <@id> renders as a clickable mention
    return f"-# ↱ Replying to {author_label}: {preview} • [Jump to message]({replied.jump_url})\n-# ───────────────────\n"


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    for guild_id in await db.get_all_guild_ids():
        await refresh_pattern(guild_id)
    for guild_id in await db.get_all_setting_guild_ids():
        await refresh_settings(guild_id)
    for guild_id in await db.get_all_exempt_guild_ids():
        await refresh_exempt(guild_id)
    for channel_id, guild_id, enabled, max_messages in await db.get_all_channel_limits():
        channel_limits[channel_id] = {"guild_id": guild_id, "enabled": enabled, "max": max_messages}
        if enabled:
            # Messages may have piled up while the bot was offline
            channel = client.get_channel(int(channel_id))
            if channel is not None:
                schedule_limit_cleanup(channel)


# ---------------------------------------------------------------------------
# /gu command group
# ---------------------------------------------------------------------------
# Every command is a direct subcommand: "/gu <action>", e.g. "/gu add", "/gu presets".
# Discord can't run a group by itself, so "/gu help" is the entry point.

gu_group = app_commands.Group(name="gu", description="GrimUtilities bot commands", guild_only=True)
client.tree.add_command(gu_group)


@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        text = "You need the Manage Server permission to use this."
    else:
        print(f"Command error: {error}")
        text = "Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def has_manage_guild(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def parse_words(raw: str) -> list[str]:
    """Split a comma-separated input into unique, lowercased, non-empty words (order kept)."""
    seen: dict[str, None] = {}
    for part in raw.split(","):
        word = part.strip().lower()
        if word:
            seen[word] = None
    return list(seen)


def format_words(words: list[str]) -> str:
    return ", ".join(f"`{w}`" for w in words)


# ---------------------------------------------------------------------------
# /gu help
# ---------------------------------------------------------------------------

@gu_group.command(name="help", description="Show what GrimUtilities does and list its commands")
async def gu_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="GrimUtilities",
        description=(
            "GrimUtilities watches messages for trigger words. When one matches, "
            "the original message is deleted and reposted through a webhook "
            "using your name and avatar, so it looks like it came from you "
            "while still being tracked by the bot. It can also limit how many "
            "messages a channel keeps."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Trigger words",
        value=(
            "`/gu words [page]` — show this server's trigger words\n"
            "`/gu add words` — add one or more words (comma-separated) 🔒\n"
            "`/gu remove words` — remove one or more words (comma-separated) 🔒\n"
            "`/gu clear` — remove ALL words, after a confirmation 🔒"
        ),
        inline=False,
    )
    embed.add_field(
        name="Presets (default word lists)",
        value=(
            "`/gu presets` — list the available presets\n"
            "`/gu presets name [page]` — preview a preset\n"
            "`/gu import name` — add a preset's words to this server 🔒"
        ),
        inline=False,
    )
    embed.add_field(
        name="Settings",
        value=(
            "`/gu config` — show this server's settings\n"
            "`/gu config setting value` — change one 🔒\n"
            "Settings: " + ", ".join(f"`{alias}` ({SETTINGS_DESCRIPTIONS[alias]})" for alias in SETTINGS_ALIASES)
        ),
        inline=False,
    )
    embed.add_field(
        name="Channels",
        value=(
            "`/gu limit [channel]` — show this channel's message limit\n"
            f"`/gu limit count [enabled] [channel]` — keep only the `count` most recent messages (max {MESSAGE_LIMIT_MAX}) 🔒\n"
            "`/gu limit enabled` — turn the limit on/off, keeping its count 🔒\n"
            "Pinned messages are never deleted and don't count."
        ),
        inline=False,
    )
    embed.add_field(
        name="Personal",
        value=(
            "`/gu exempt [value]` — toggle yourself on/off the exempt list (or force it with `value`). "
            "Only applies while the `exempt` setting is enabled."
        ),
        inline=False,
    )
    embed.set_footer(text="🔒 = requires Manage Server • Replies to a message keep a small preview and link.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /gu words
# ---------------------------------------------------------------------------

def build_list_embed(guild_name: str, words: list[str], page: int, total_pages: int) -> discord.Embed:
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE
    page_words = words[start:end]

    description = format_words(page_words) if page_words else "*No trigger words yet. Add some with `/gu add`.*"

    embed = discord.Embed(
        title=f"Trigger words — {guild_name}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(words)} word(s) total")
    return embed


class WordListView(discord.ui.View):
    def __init__(self, guild_id: str, guild_name: str, page: int, total_pages: int, requester_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.page = page
        self.requester_id = requester_id
        self.previous_button.disabled = page <= 0
        self.next_button.disabled = page >= total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        words = await db.get_words(self.guild_id)
        total_pages = max(1, (len(words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
        self.page = max(0, min(self.page, total_pages - 1))
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total_pages - 1
        embed = build_list_embed(self.guild_name, words, self.page, total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@gu_group.command(name="words", description="Show this server's trigger words")
@app_commands.describe(page="Page number to jump to (starts at 1)")
async def gu_words(interaction: discord.Interaction, page: int = 1):
    guild_id = str(interaction.guild_id)
    words = await db.get_words(guild_id)
    total_pages = max(1, (len(words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
    page_index = max(0, min(page - 1, total_pages - 1))

    embed = build_list_embed(interaction.guild.name, words, page_index, total_pages)
    view = WordListView(guild_id, interaction.guild.name, page_index, total_pages, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# /gu add
# ---------------------------------------------------------------------------

@gu_group.command(name="add", description="Add one or more trigger words (comma-separated)")
@app_commands.describe(words="Word(s) to add, separated by commas: word1, word2")
@app_commands.checks.has_permissions(manage_guild=True)
async def gu_add(interaction: discord.Interaction, words: str):
    guild_id = str(interaction.guild_id)
    to_add = parse_words(words)

    if not to_add:
        await interaction.response.send_message("Please give at least one word.", ephemeral=True)
        return

    existing = set(await db.get_words(guild_id))
    new_words = [w for w in to_add if w not in existing]
    already = [w for w in to_add if w in existing]

    if new_words:
        await db.add_words(guild_id, new_words)
        await refresh_pattern(guild_id)

    lines = []
    if new_words:
        lines.append(f"✅ Added {format_words(new_words)}.")
    if already:
        lines.append(f"Already in the list: {format_words(already)}.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /gu remove
# ---------------------------------------------------------------------------

async def guild_word_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # Completes the LAST comma-separated piece, keeping what was typed before it:
    # typing "foo, ba" suggests "foo, bar", "foo, baz"...
    head, sep, last = current.rpartition(",")
    prefix = f"{head}, " if sep else ""
    already_typed = set(parse_words(head))
    needle = last.strip().lower()

    words = await db.get_words(str(interaction.guild_id))
    choices = []
    for w in words:
        if w in already_typed or needle not in w:
            continue
        value = prefix + w
        if len(value) > 100:  # Discord limit for choice names/values
            continue
        choices.append(app_commands.Choice(name=value, value=value))
        if len(choices) == 25:
            break
    return choices


@gu_group.command(name="remove", description="Remove one or more trigger words (comma-separated)")
@app_commands.describe(words="Word(s) to remove, separated by commas: word1, word2")
@app_commands.autocomplete(words=guild_word_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def gu_remove(interaction: discord.Interaction, words: str):
    guild_id = str(interaction.guild_id)
    to_remove = parse_words(words)

    if not to_remove:
        await interaction.response.send_message("Please give at least one word.", ephemeral=True)
        return

    removed, missing = [], []
    for w in to_remove:
        (removed if await db.remove_word(guild_id, w) else missing).append(w)

    if removed:
        await refresh_pattern(guild_id)

    lines = []
    if removed:
        lines.append(f"🗑️ Removed {format_words(removed)}.")
    if missing:
        lines.append(f"Not in the list: {format_words(missing)}.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /gu clear
# ---------------------------------------------------------------------------

class ClearConfirmView(discord.ui.View):
    def __init__(self, guild_id: str, requester_id: int, interaction: discord.Interaction):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.interaction = interaction  # original interaction, used to edit the prompt on timeout

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Yes, clear everything", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        removed = await db.clear_words(self.guild_id)
        await refresh_pattern(self.guild_id)
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=f"🗑️ Cleared **{removed}** trigger word(s).", view=self
        )

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. No words were removed.", view=self
        )

    async def on_timeout(self):
        self._disable_all()
        try:
            await self.interaction.edit_original_response(
                content="⌛ Confirmation timed out. No words were removed.", view=self
            )
        except discord.HTTPException:
            pass


@gu_group.command(name="clear", description="Remove ALL trigger words from this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def gu_clear(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    words = await db.get_words(guild_id)

    if not words:
        await interaction.response.send_message("There are no trigger words to clear.", ephemeral=True)
        return

    view = ClearConfirmView(guild_id, interaction.user.id, interaction)
    await interaction.response.send_message(
        f"⚠️ Are you sure you want to remove **all {len(words)}** trigger word(s) from this server? "
        "This cannot be undone.",
        view=view,
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /gu presets
# ---------------------------------------------------------------------------

async def default_list_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    names = list_default_word_lists()
    filtered = [n for n in names if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


def build_default_list_embed(list_name: str, words: list[str], page: int, total_pages: int) -> discord.Embed:
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE
    page_words = words[start:end]

    description = format_words(page_words) if page_words else "*This preset is empty.*"

    embed = discord.Embed(
        title=f"Preset — {list_name}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"Page {page + 1}/{total_pages} • {len(words)} word(s) • Use /gu import name:{list_name} to import it."
    )
    return embed


class DefaultListView(discord.ui.View):
    def __init__(self, list_name: str, words: list[str], page: int, requester_id: int):
        super().__init__(timeout=120)
        self.list_name = list_name
        self.words = words
        self.page = page
        self.requester_id = requester_id
        self._update_button_state()

    def _total_pages(self) -> int:
        return max(1, (len(self.words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)

    def _update_button_state(self):
        total_pages = self._total_pages()
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        total_pages = self._total_pages()
        self.page = max(0, min(self.page, total_pages - 1))
        self._update_button_state()
        embed = build_default_list_embed(self.list_name, self.words, self.page, total_pages)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@gu_group.command(name="presets", description="List the available presets, or preview one by name")
@app_commands.describe(
    name="Preset to preview (leave empty to list all presets)",
    page="Page number to jump to when previewing (starts at 1)",
)
@app_commands.autocomplete(name=default_list_autocomplete)
async def gu_presets(interaction: discord.Interaction, name: str | None = None, page: int = 1):
    if name is None:
        names = list_default_word_lists()
        if not names:
            await interaction.response.send_message(
                f"No presets found in the `{WORD_LISTS_DIR}` folder.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Available presets",
            description="\n".join(f"• `{n}`" for n in names),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Use /gu presets name:<preset> to preview one, or /gu import name:<preset> to import it.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    words = load_default_word_list(name)
    if words is None:
        await interaction.response.send_message(
            f"No valid preset named `{name}` found in `{WORD_LISTS_DIR}/`.", ephemeral=True
        )
        return

    total_pages = max(1, (len(words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
    page_index = max(0, min(page - 1, total_pages - 1))

    embed = build_default_list_embed(name, words, page_index, total_pages)
    view = DefaultListView(name, words, page_index, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# /gu import
# ---------------------------------------------------------------------------

@gu_group.command(name="import", description="Add a preset's words to this server's trigger list")
@app_commands.describe(name="Preset to import")
@app_commands.autocomplete(name=default_list_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def gu_import(interaction: discord.Interaction, name: str):
    words_to_add = load_default_word_list(name)

    if words_to_add is None:
        await interaction.response.send_message(
            f"No valid preset named `{name}` found in `{WORD_LISTS_DIR}/`.", ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    added, skipped = await db.add_words(guild_id, words_to_add)
    await refresh_pattern(guild_id)

    message = f"Imported `{name}`: added {added} word(s)."
    if skipped:
        message += f" Skipped {skipped} already present."

    await interaction.response.send_message(message, ephemeral=True)


# ---------------------------------------------------------------------------
# /gu config
# ---------------------------------------------------------------------------

@gu_group.command(name="config", description="Show this server's settings, or change one")
@app_commands.describe(
    setting="Setting to change (leave empty to show all settings)",
    value="Enable (True) or disable (False)",
)
@app_commands.choices(
    setting=[app_commands.Choice(name=f"{alias} — {SETTINGS_DESCRIPTIONS[alias]}", value=alias) for alias in SETTINGS_ALIASES]
)
async def gu_config(
    interaction: discord.Interaction,
    setting: app_commands.Choice[str] | None = None,
    value: bool | None = None,
):
    guild_id = str(interaction.guild_id)

    # Show mode: anyone can look at the settings.
    if setting is None:
        if value is not None:
            await interaction.response.send_message(
                "Pick a `setting` to change along with `value`.", ephemeral=True
            )
            return

        settings = await db.get_settings(guild_id)
        embed = discord.Embed(title=f"Settings — {interaction.guild.name}", color=discord.Color.blurple())
        for alias, column in SETTINGS_ALIASES.items():
            state = "✅ Enabled" if settings[column] else "❌ Disabled"
            embed.add_field(name=alias, value=f"{state}\n*{SETTINGS_DESCRIPTIONS[alias]}*", inline=True)
        limited = [
            f"<#{cid}> — keeps {lim['max']}"
            for cid, lim in channel_limits.items()
            if lim["guild_id"] == guild_id and lim["enabled"]
        ]
        embed.add_field(
            name="Message limits",
            value="\n".join(limited) if limited else "*No channel has a message limit.*",
            inline=False,
        )
        embed.set_footer(text="Use /gu config setting:<name> value:<true/false> to change one.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    alias = setting.value
    column = SETTINGS_ALIASES[alias]

    if value is None:
        settings = await db.get_settings(guild_id)
        state = "enabled" if settings[column] else "disabled"
        await interaction.response.send_message(
            f"`{alias}` is currently **{state}**. Add `value` to change it.", ephemeral=True
        )
        return

    # Change mode: requires Manage Server.
    if not has_manage_guild(interaction):
        raise app_commands.MissingPermissions(["manage_guild"])

    await db.set_setting(guild_id, column, value)
    await refresh_settings(guild_id)

    state = "enabled" if value else "disabled"
    await interaction.response.send_message(f"`{alias}` is now **{state}** for this server.", ephemeral=True)


# ---------------------------------------------------------------------------
# /gu exempt
# ---------------------------------------------------------------------------

@gu_group.command(name="exempt", description="Toggle yourself on/off this server's exempt list")
@app_commands.describe(value="Optional: True to be exempt, False to stop being exempt (empty = toggle)")
async def gu_exempt(interaction: discord.Interaction, value: bool | None = None):
    # Always usable by anyone: the reply states whether the exempt list is actually applied.
    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

    if value is None:
        value = user_id not in set(await db.get_exempt_users(guild_id))

    if value:
        changed = await db.add_exempt_user(guild_id, user_id)
        headline = (
            "You've been **added** to the exempt list."
            if changed else "You're **already** on the exempt list."
        )
    else:
        changed = await db.remove_exempt_user(guild_id, user_id)
        headline = (
            "You've been **removed** from the exempt list."
            if changed else "You weren't on the exempt list."
        )

    await refresh_exempt(guild_id)

    enabled = get_cached_settings(guild_id)["allowExemptList"]

    if enabled and value:
        status = "✅ The `exempt` setting is **enabled** on this server: your messages will **not** be proxied, even when they match a trigger."
    elif enabled:
        status = "✅ The `exempt` setting is **enabled** on this server: your messages **will** be proxied when they match a trigger."
    elif value:
        status = (
            "⚠️ The `exempt` setting is currently **disabled** on this server: your messages **will still** be proxied "
            "when they match a trigger. You'll be exempt as soon as someone with **Manage Server** enables it."
        )
    else:
        status = (
            "ℹ️ The `exempt` setting is **disabled** on this server: the exempt list isn't applied, "
            "so your messages are proxied when they match a trigger either way."
        )

    await interaction.response.send_message(f"{headline}\n{status}", ephemeral=True)


# ---------------------------------------------------------------------------
# Channel message limits
# ---------------------------------------------------------------------------
# A limited channel only keeps its N most recent messages: everything older is deleted.
# Pinned messages are never deleted and don't count toward N.

limit_cleanup_pending: set[int] = set()            # channel ids with a cleanup already scheduled
limit_cleanup_locks: dict[int, asyncio.Lock] = {}  # channel id -> lock (one cleanup at a time per channel)


async def enforce_message_limit(channel: discord.abc.Messageable, keep: int) -> int:
    """Delete every non-pinned message older than the `keep` most recent ones. Returns how many were deleted."""
    boundary = None
    kept = 0
    async for message in channel.history(limit=None):  # newest first
        if message.pinned:
            continue
        kept += 1
        if kept == keep:
            boundary = message
            break

    if boundary is None:
        return 0  # not over the limit

    # purge() bulk-deletes messages younger than 14 days and falls back to one-by-one
    # deletion for older ones (Discord refuses to bulk-delete those).
    deleted = await channel.purge(
        limit=None,
        before=boundary,
        check=lambda m: not m.pinned,
        reason=f"Channel message limit ({keep})",
    )
    return len(deleted)


def schedule_limit_cleanup(channel: discord.abc.Messageable) -> None:
    """Run a cleanup of this channel a few seconds from now. Calls made in the meantime are merged."""
    if channel.id in limit_cleanup_pending:
        return
    limit_cleanup_pending.add(channel.id)
    asyncio.create_task(_run_limit_cleanup(channel))


async def _run_limit_cleanup(channel: discord.abc.Messageable) -> None:
    await asyncio.sleep(LIMIT_CLEANUP_DELAY)
    # Released BEFORE cleaning: a message arriving during the cleanup schedules a new pass.
    limit_cleanup_pending.discard(channel.id)

    lock = limit_cleanup_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        limit = channel_limits.get(str(channel.id))
        if not limit or not limit["enabled"]:
            return  # disabled while we were waiting
        try:
            deleted = await enforce_message_limit(channel, limit["max"])
        except discord.Forbidden:
            print(f"Missing permission to clean #{channel} (needs Read Message History and Manage Messages)")
            return
        except discord.HTTPException as e:
            # e.g. a message in the batch was deleted meanwhile (the proxy does that): try again later
            print(f"Message limit cleanup failed in #{channel}: {e}")
            schedule_limit_cleanup(channel)
            return
        if deleted:
            print(f"Message limit: deleted {deleted} message(s) in #{channel}")


async def set_channel_limit_and_cache(guild_id: str, channel_id: str, enabled: bool, max_messages: int) -> None:
    await db.set_channel_limit(guild_id, channel_id, enabled, max_messages)
    channel_limits[channel_id] = {"guild_id": guild_id, "enabled": enabled, "max": max_messages}


@client.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if channel_limits.pop(str(channel.id), None) is not None:
        await db.delete_channel_limit(str(channel.id))


@client.event
async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent):
    if channel_limits.pop(str(payload.thread_id), None) is not None:
        await db.delete_channel_limit(str(payload.thread_id))


class LimitConfirmView(discord.ui.View):
    """Asks for confirmation before turning on (or lowering) a limit, since deleted messages are gone for good."""

    def __init__(self, channel, count: int, requester_id: int, interaction: discord.Interaction):
        super().__init__(timeout=30)
        self.channel = channel
        self.count = count
        self.requester_id = requester_id
        self.interaction = interaction  # original interaction, used to edit the prompt on timeout

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Yes, enable it", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_channel_limit_and_cache(str(interaction.guild_id), str(self.channel.id), True, self.count)
        schedule_limit_cleanup(self.channel)
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"✅ {self.channel.mention} now keeps only its **{self.count}** most recent message(s). "
                "Older messages are being deleted (this can take a while for messages older than 14 days)."
            ),
            view=self,
        )

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)

    async def on_timeout(self):
        self._disable_all()
        try:
            await self.interaction.edit_original_response(
                content="⌛ Confirmation timed out. Nothing was changed.", view=self
            )
        except discord.HTTPException:
            pass


@gu_group.command(name="limit", description="Keep only the N most recent messages in a channel")
@app_commands.describe(
    count=f"How many recent messages to keep (1-{MESSAGE_LIMIT_MAX}). Setting it turns the limit on.",
    enabled="Turn the limit on or off (keeps the stored count)",
    channel="Channel to configure (defaults to this one)",
)
async def gu_limit(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 1, MESSAGE_LIMIT_MAX] | None = None,
    enabled: bool | None = None,
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = None,
):
    channel = channel or interaction.channel
    guild_id = str(interaction.guild_id)
    channel_id = str(channel.id)
    current = channel_limits.get(channel_id)

    # Show mode: anyone can look.
    if count is None and enabled is None:
        if current is None:
            text = f"{channel.mention} has no message limit."
        else:
            state = "**enabled**" if current["enabled"] else "**disabled**"
            text = f"{channel.mention} keeps its **{current['max']}** most recent message(s) — limit {state}."
        await interaction.response.send_message(text, ephemeral=True)
        return

    # Change mode: requires Manage Server.
    if not has_manage_guild(interaction):
        raise app_commands.MissingPermissions(["manage_guild"])

    new_count = count if count is not None else (current["max"] if current else None)
    new_enabled = enabled if enabled is not None else True  # giving a count implies turning it on

    if new_count is None:
        await interaction.response.send_message(
            f"{channel.mention} has no stored count yet: give one with `count`.", ephemeral=True
        )
        return

    if not new_enabled:
        await set_channel_limit_and_cache(guild_id, channel_id, False, new_count)
        await interaction.response.send_message(
            f"Message limit **disabled** for {channel.mention} (count {new_count} kept for later).", ephemeral=True
        )
        return

    # Turning it on: the bot must be able to read the history and delete messages there.
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.read_message_history and perms.manage_messages):
        await interaction.response.send_message(
            f"I need **Read Message History** and **Manage Messages** in {channel.mention} to enforce a limit.",
            ephemeral=True,
        )
        return

    was_enabled = bool(current and current["enabled"])
    if was_enabled and new_count >= current["max"]:
        # Raising (or keeping) an active limit deletes nothing: no confirmation needed.
        await set_channel_limit_and_cache(guild_id, channel_id, True, new_count)
        await interaction.response.send_message(
            f"✅ {channel.mention} now keeps its **{new_count}** most recent message(s).", ephemeral=True
        )
        return

    view = LimitConfirmView(channel, new_count, interaction.user.id, interaction)
    await interaction.response.send_message(
        f"⚠️ {channel.mention} will only keep its **{new_count}** most recent message(s): every older message "
        "(except pinned ones) will be **deleted now**, and again each time new messages come in. "
        "This cannot be undone. Continue?",
        view=view,
        ephemeral=True,
    )


def allowed_mentions_for(msg: discord.Message) -> discord.AllowedMentions:
    """Build an AllowedMentions mirroring what the author could actually have pinged:
      - user mentions: only the users the author mentioned in their OWN text (our reply-prefix
        <@id> isn't in msg.mentions, so a reply still shows the label without pinging);
      - @everyone/@here: only with the Mention Everyone permission in this channel;
      - role mentions: all mentioned roles if they have Mention Everyone, otherwise only the
        mentioned roles that are themselves mentionable — exactly Discord's own rule."""
    perms = msg.channel.permissions_for(msg.author)
    can_mention_everyone = perms.mention_everyone
    roles = msg.role_mentions if can_mention_everyone else [r for r in msg.role_mentions if r.mentionable]
    return discord.AllowedMentions(
        everyone=can_mention_everyone,
        users=msg.mentions,
        roles=roles,
        replied_user=False,
    )


@client.event
async def on_message(msg: discord.Message):
    if not msg.guild:
        return

    # Message limits count EVERY message (bots and webhooks included), so this runs before the bot filter
    limit = channel_limits.get(str(msg.channel.id))
    if limit and limit["enabled"]:
        schedule_limit_cleanup(msg.channel)

    # Ignore bots/webhooks (prevents infinite loops)
    if msg.author.bot:
        return

    guild_id = str(msg.guild.id)
    settings = get_cached_settings(guild_id)

    # Exempt members bypass the proxy entirely, but only if the guild enabled the exempt list.
    # Checked first: it's a cheap set lookup and avoids all the detection work below.
    if settings["allowExemptList"] and str(msg.author.id) in guild_exempt.get(guild_id, ()):
        return

    pattern = guild_patterns.get(guild_id)

    word_trigger = bool(pattern and pattern.search(msg.content))
    link_trigger = settings["doLinksTrigger"] and bool(LINK_PATTERN.search(msg.content))
    media_trigger = settings["doMediasTrigger"] and bool(msg.attachments)

    # Fuzzy detection only runs if the exact match above found nothing, to keep the common case cheap
    fuzzy_trigger = False
    if not word_trigger and settings["doFuzzyDetection"]:
        normalized_triggers = guild_normalized_words.get(guild_id, [])
        if normalized_triggers:
            per_token_hit = any(fuzzy_match(token, normalized_triggers) for token in tokenize(msg.content))
            chain_hit = any(
                fuzzy_match(chain, normalized_triggers) for chain in extract_separator_chains(msg.content)
            )
            fuzzy_trigger = per_token_hit or chain_hit

    if not (word_trigger or link_trigger or media_trigger or fuzzy_trigger):
        return

    # Reproduce Discord's native behavior about embed links: without the permission,
    # the author's links wouldn't have been previewed, so the repost must not preview them either.
    # Computed on the ORIGINAL channel (thread included) before we swap to the parent below.
    can_embed = msg.channel.permissions_for(msg.author).embed_links

    # Mirror the author's real mention rights, so the repost pings exactly what their own
    # message would have. Computed on the ORIGINAL channel, before the thread->parent swap.
    allowed_mentions = allowed_mentions_for(msg)

    channel = msg.channel
    thread = discord.utils.MISSING
    if isinstance(channel, discord.Thread):  # webhooks belong to the parent channel
        thread = channel
        channel = channel.parent

    try:
        hook = await get_webhook(channel)
    except discord.HTTPException as e:
        print(f"Failed to get/create webhook in #{channel}: {e}")
        return

    reply_prefix = await build_reply_prefix(msg)
    content = reply_prefix + msg.content
    # to_file() defaults to spoiler=False and strips the SPOILER_ prefix: keep the original state
    files = [await a.to_file(spoiler=a.is_spoiler()) for a in msg.attachments]

    sent = None
    try:
        sent = await hook.send(
            content=content,
            username=msg.author.display_name,
            avatar_url=msg.author.display_avatar.url,
            files=files,
            thread=thread,
            allowed_mentions=allowed_mentions,
            suppress_embeds=not can_embed,
            wait=True,  # needed to get the sent message back (its id is stored below)
        )
    except discord.NotFound:
        # Webhook was deleted manually on Discord's side, clear cache and retry once
        print(f"Cached webhook for #{channel} is invalid, recreating")
        webhooks.pop(channel.id, None)
        try:
            hook = await get_webhook(channel)
            sent = await hook.send(
                content=content,
                username=msg.author.display_name,
                avatar_url=msg.author.display_avatar.url,
                files=files,
                thread=thread,
                allowed_mentions=allowed_mentions,
                suppress_embeds=not can_embed,
                wait=True,
            )
        except discord.HTTPException as e:
            print(f"Retry failed for message {msg.id}: {e}")
            return
    except discord.HTTPException as e:
        print(f"Failed to send webhook message for {msg.id}: {e}")
        return

    # Remember who really wrote this proxied message, for correct mentions in later replies
    if sent is not None:
        remember_proxy_author(sent.id, msg.author.id)

    await asyncio.sleep(DELETE_DELAY)  # small buffer to avoid client-side render race

    try:
        await msg.delete()
    except discord.NotFound:
        pass  # already deleted
    except discord.Forbidden:
        print(f"Missing permission to delete message {msg.id} in #{msg.channel}")
    except discord.HTTPException as e:
        print(f"Failed to delete message {msg.id}: {e}")


client.run(TOKEN)
