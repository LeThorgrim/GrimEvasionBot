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
WORDS_PER_PAGE = 20  # max words shown per page in /grimevasion list
PROXY_AUTHORS_MAX = 5000  # max remembered proxied messages (keeps memory bounded)

# Fuzzy typo tolerance (edit distance) depends on the NORMALIZED word length. Shorter words only
# match exactly once normalized (leetspeak, repeated letters, separators), because one typo on a
# 4-5 letter word turns far too many everyday words into false positives ("that" ~ "twat", "pardo" ~ "paedo").
FUZZY_ONE_EDIT_MIN_LENGTH = 6   # from this length: 1 edit tolerated
FUZZY_TWO_EDITS_MIN_LENGTH = 9  # from this length: 2 edits tolerated
FUZZY_MIN_NORMALIZED_LENGTH = 2  # collapsing repeated letters never shrinks a word below this length

LINK_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
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
SETTINGS_PARAMETERS = ["doLinksTrigger", "doMediasTrigger", "doFuzzyDetection", "allowExemptList"]  # valid /grimevasion configure values

# Leetspeak -> letter substitutions applied before fuzzy comparison
LEETSPEAK_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i",
})

guild_patterns: dict[str, re.Pattern] = {}       # cache: guild_id -> compiled regex (exact match)
guild_normalized_words: dict[str, list[str]] = {}  # cache: guild_id -> normalized trigger words (for fuzzy match)
guild_settings: dict[str, dict] = {}              # cache: guild_id -> settings dict
guild_exempt: dict[str, set[str]] = {}            # cache: guild_id -> user ids (as str) on the exempt list


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
            activity=discord.Activity(type=discord.ActivityType.watching, name="/grimevasion info || ThorgrimCorp."),
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
    if len(preview) > PREVIEW_LENGTH:
        preview = preview[:PREVIEW_LENGTH - 3] + "..."
    if not preview:
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


# ---------------------------------------------------------------------------
# /grimevasion command group
# ---------------------------------------------------------------------------
# Top-level subcommands appear as "/grimevasion <subcommand>", e.g. "/grimevasion list"
# "word" and "lists" are subgroups: "/grimevasion word add", "/grimevasion lists show"

grimevasion_group = app_commands.Group(name="grimevasion", description="GrimEvasion proxy bot commands")
client.tree.add_command(grimevasion_group)

word_group = app_commands.Group(name="word", description="Manage this server's trigger words", parent=grimevasion_group)
lists_group = app_commands.Group(name="lists", description="Browse and import default word lists", parent=grimevasion_group)
list_group = app_commands.Group(name="list", description="View or clear this server's trigger words", parent=grimevasion_group)


# ---------------------------------------------------------------------------
# /grimevasion info
# ---------------------------------------------------------------------------

@grimevasion_group.command(name="info", description="Show info about GrimEvasion and its commands")
async def grim_evasion_info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="GrimEvasion",
        description=(
            "GrimEvasion watches messages for trigger words. When one matches, "
            "the original message is deleted and reposted through a webhook "
            "using your name and avatar, so it looks like it came from you "
            "while still being tracked by the bot."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="/grimevasion info",
        value="Shows this help message.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion word add `word`",
        value="Adds a word to this server's trigger list. Requires **Manage Server**.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion word remove `word`",
        value="Removes a word from this server's trigger list. Requires **Manage Server**.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion list show `page`",
        value="Lists the trigger words configured for this server, with pagination.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion list clear",
        value="Removes ALL trigger words from this server, after a confirmation. Requires **Manage Server**.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion lists list",
        value="Lists the default word lists available to import.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion lists show `name`",
        value="Previews the content of a specific default word list.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion lists add `name`",
        value="Imports a default word list into this server's trigger list. Requires **Manage Server**.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion configure `parameter` `value`",
        value="Enables or disables a server setting (doLinksTrigger, doMediasTrigger, doFuzzyDetection, allowExemptList). Requires **Manage Server**.",
        inline=False,
    )
    embed.add_field(
        name="/grimevasion exempt `value`",
        value=(
            "Adds yourself to (`true`) or removes yourself from (`false`) this server's exempt list. "
            "Exempt members are ignored by the proxy, but only while `allowExemptList` is enabled."
        ),
        inline=False,
    )
    embed.add_field(
        name="/grimevasion parameters",
        value="Shows this server's current settings.",
        inline=False,
    )
    embed.set_footer(text="Messages replying to another message keep a small preview and link.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion list
# ---------------------------------------------------------------------------

def build_list_embed(guild_name: str, words: list[str], page: int, total_pages: int) -> discord.Embed:
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE
    page_words = words[start:end]

    description = ", ".join(f"`{w}`" for w in page_words) if page_words else "*No trigger words yet.*"

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


@list_group.command(name="show", description="Show this server's trigger words")
@app_commands.describe(page="Page number to jump to (starts at 1)")
async def grim_evasion_list(interaction: discord.Interaction, page: int = 1):
    guild_id = str(interaction.guild_id)
    words = await db.get_words(guild_id)
    total_pages = max(1, (len(words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
    page_index = max(0, min(page - 1, total_pages - 1))

    embed = build_list_embed(interaction.guild.name, words, page_index, total_pages)
    view = WordListView(guild_id, interaction.guild.name, page_index, total_pages, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion list clear
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


@list_group.command(name="clear", description="Remove ALL trigger words from this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_evasion_list_clear(interaction: discord.Interaction):
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


@grim_evasion_list_clear.error
async def grim_evasion_list_clear_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion word add
# ---------------------------------------------------------------------------

@word_group.command(name="add", description="Add a word to this server's proxy trigger list")
@app_commands.describe(word="The word to add")
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_evasion_word_add(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    word = word.strip().lower()

    if not word:
        await interaction.response.send_message("Word cannot be empty.", ephemeral=True)
        return

    added = await db.add_word(guild_id, word)
    if not added:
        await interaction.response.send_message(f"`{word}` is already in the list.", ephemeral=True)
        return

    await refresh_pattern(guild_id)

    await interaction.response.send_message(f"Added `{word}` to the proxy trigger list.", ephemeral=True)


@grim_evasion_word_add.error
async def grim_evasion_word_add_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion word remove
# ---------------------------------------------------------------------------

async def guild_word_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    guild_id = str(interaction.guild_id)
    words = await db.get_words(guild_id)
    filtered = [w for w in words if current.lower() in w.lower()]
    return [app_commands.Choice(name=w, value=w) for w in filtered[:25]]


@word_group.command(name="remove", description="Remove a word from this server's proxy trigger list")
@app_commands.describe(word="The word to remove")
@app_commands.autocomplete(word=guild_word_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_evasion_word_remove(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    word = word.strip().lower()

    removed = await db.remove_word(guild_id, word)
    if not removed:
        await interaction.response.send_message(f"`{word}` is not in the list.", ephemeral=True)
        return

    await refresh_pattern(guild_id)

    await interaction.response.send_message(f"Removed `{word}` from the proxy trigger list.", ephemeral=True)


@grim_evasion_word_remove.error
async def grim_evasion_word_remove_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


async def default_list_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    names = list_default_word_lists()
    filtered = [n for n in names if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


# ---------------------------------------------------------------------------
# /grimevasion lists list
# ---------------------------------------------------------------------------

@lists_group.command(name="list", description="List the default word lists available to import")
async def grim_evasion_lists_list(interaction: discord.Interaction):
    names = list_default_word_lists()

    if not names:
        await interaction.response.send_message(
            f"No default word lists found in the `{WORD_LISTS_DIR}` folder.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Available default word lists",
        description="\n".join(f"• `{name}`" for name in names),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Use /grimevasion lists show name:<list name> to preview its content, or lists add to import it.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion lists show
# ---------------------------------------------------------------------------

def build_default_list_embed(list_name: str, words: list[str], page: int, total_pages: int) -> discord.Embed:
    start = page * WORDS_PER_PAGE
    end = start + WORDS_PER_PAGE
    page_words = words[start:end]

    description = ", ".join(f"`{w}`" for w in page_words) if page_words else "*This list is empty.*"

    embed = discord.Embed(
        title=f"Default list — {list_name}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"Page {page + 1}/{total_pages} • {len(words)} word(s) • Use /grimevasion lists add name:{list_name} to import it."
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


@lists_group.command(name="show", description="Show the content of a specific default word list")
@app_commands.describe(name="Name of the default list to preview (without .json)", page="Page number to jump to (starts at 1)")
@app_commands.autocomplete(name=default_list_autocomplete)
async def grim_evasion_lists_show(interaction: discord.Interaction, name: str, page: int = 1):
    words = load_default_word_list(name)

    if words is None:
        await interaction.response.send_message(
            f"No valid default list named `{name}` found in `{WORD_LISTS_DIR}/`.", ephemeral=True
        )
        return

    total_pages = max(1, (len(words) + WORDS_PER_PAGE - 1) // WORDS_PER_PAGE)
    page_index = max(0, min(page - 1, total_pages - 1))

    embed = build_default_list_embed(name, words, page_index, total_pages)
    view = DefaultListView(name, words, page_index, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion lists add
# ---------------------------------------------------------------------------

@lists_group.command(name="add", description="Import a default word list into this server")
@app_commands.describe(name="Name of the default list to import (without .json)")
@app_commands.autocomplete(name=default_list_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_evasion_lists_add(interaction: discord.Interaction, name: str):
    words_to_add = load_default_word_list(name)

    if words_to_add is None:
        await interaction.response.send_message(
            f"No valid default list named `{name}` found in `{WORD_LISTS_DIR}/`.", ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    added, skipped = await db.add_words(guild_id, words_to_add)
    await refresh_pattern(guild_id)

    message = f"Imported `{name}`: added {added} word(s)."
    if skipped:
        message += f" Skipped {skipped} already present."

    await interaction.response.send_message(message, ephemeral=True)


@grim_evasion_lists_add.error
async def grim_evasion_lists_add_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion configure
# ---------------------------------------------------------------------------

async def settings_parameter_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    filtered = [p for p in SETTINGS_PARAMETERS if current.lower() in p.lower()]
    return [app_commands.Choice(name=p, value=p) for p in filtered]


@grimevasion_group.command(name="configure", description="Enable or disable an automatic trigger setting for this server")
@app_commands.describe(parameter="The setting to change", value="Enable (True) or disable (False)")
@app_commands.autocomplete(parameter=settings_parameter_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_evasion_configure(interaction: discord.Interaction, parameter: str, value: bool):
    if parameter not in SETTINGS_PARAMETERS:
        await interaction.response.send_message(
            f"Unknown parameter `{parameter}`. Valid options: {', '.join(SETTINGS_PARAMETERS)}",
            ephemeral=True,
        )
        return

    guild_id = str(interaction.guild_id)
    await db.set_setting(guild_id, parameter, value)
    await refresh_settings(guild_id)

    state = "enabled" if value else "disabled"
    await interaction.response.send_message(f"`{parameter}` is now **{state}** for this server.", ephemeral=True)


@grim_evasion_configure.error
async def grim_evasion_configure_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion parameters
# ---------------------------------------------------------------------------

@grimevasion_group.command(name="parameters", description="Show this server's GrimEvasion settings")
async def grim_evasion_parameters(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    settings = await db.get_settings(guild_id)

    embed = discord.Embed(
        title=f"Settings — {interaction.guild.name}",
        color=discord.Color.blurple(),
    )
    for param in SETTINGS_PARAMETERS:
        state = "✅ Enabled" if settings[param] else "❌ Disabled"
        embed.add_field(name=param, value=state, inline=True)
    embed.set_footer(text="Use /grimevasion configure parameter:<name> value:<true/false> to change these.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /grimevasion exempt
# ---------------------------------------------------------------------------

@grimevasion_group.command(name="exempt", description="Add or remove yourself from this server's exempt list")
@app_commands.describe(value="True to add yourself to the exempt list, False to remove yourself")
async def grim_evasion_exempt(interaction: discord.Interaction, value: bool):
    # Always usable by anyone: the reply states whether the exempt list is actually applied.
    if interaction.guild_id is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

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
        status = "✅ `allowExemptList` is **enabled** on this server: your messages will **not** be proxied, even when they match a trigger."
    elif enabled:
        status = "✅ `allowExemptList` is **enabled** on this server: your messages **will** be proxied when they match a trigger."
    elif value:
        status = (
            "⚠️ `allowExemptList` is currently **disabled** on this server: your messages **will still** be proxied "
            "when they match a trigger. You'll be exempt as soon as someone with **Manage Server** enables it."
        )
    else:
        status = (
            "ℹ️ `allowExemptList` is **disabled** on this server: the exempt list isn't applied, "
            "so your messages are proxied when they match a trigger either way."
        )

    await interaction.response.send_message(f"{headline}\n{status}", ephemeral=True)


@client.event
async def on_message(msg: discord.Message):
    # Ignore bots/webhooks (prevents infinite loops) and DMs
    if msg.author.bot or not msg.guild:
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
    files = [await a.to_file() for a in msg.attachments]

    sent = None
    try:
        sent = await hook.send(
            content=content,
            username=msg.author.display_name,
            avatar_url=msg.author.display_avatar.url,
            files=files,
            thread=thread,
            allowed_mentions=discord.AllowedMentions.none(),
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
                allowed_mentions=discord.AllowedMentions.none(),
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