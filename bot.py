import os
import re
import json
import asyncio
import discord
from discord import app_commands
from dotenv import load_dotenv

import db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found: check your .env file")

WORD_LISTS_DIR = "wordLists"  # folder containing default word list .json files
DELETE_DELAY = 0.1  # seconds to wait before deleting the original message (local display for users is bugged when too low)
PREVIEW_LENGTH = 80  # max characters shown from the replied-to message
WORDS_PER_PAGE = 20  # max words shown per page in /grimevasion list

LINK_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
SETTINGS_PARAMETERS = ["doLinksTrigger", "doMediasTrigger"]  # valid /grimevasion configure parameter values

guild_patterns: dict[str, re.Pattern] = {}  # cache: guild_id -> compiled regex
guild_settings: dict[str, dict] = {}        # cache: guild_id -> {"doLinksTrigger": bool, "doMediasTrigger": bool}


def build_pattern(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE)


async def refresh_pattern(guild_id: str) -> None:
    words = await db.get_words(guild_id)
    guild_patterns[guild_id] = build_pattern(words)


async def refresh_settings(guild_id: str) -> None:
    guild_settings[guild_id] = await db.get_settings(guild_id)


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
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init_db()
        await self.tree.sync()


client = ProxyClient()

webhooks: dict[int, discord.Webhook] = {}  # cache: channel id -> webhook


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

    preview = replied.content.replace("\n", " ").strip()
    if len(preview) > PREVIEW_LENGTH:
        preview = preview[:PREVIEW_LENGTH - 3] + "..."
    if not preview:
        preview = "*[attachment/embed]*"

    # "-# " renders as small subtext in Discord; <@id> renders as a clickable mention
    return f"-# ↱ Replying to <@{replied.author.id}>: {preview} • [Jump to message]({replied.jump_url})\n-# ───────────────────\n"


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    for guild_id in await db.get_all_guild_ids():
        await refresh_pattern(guild_id)
    for guild_id in await db.get_all_setting_guild_ids():
        await refresh_settings(guild_id)


# ---------------------------------------------------------------------------
# /grimevasion command group
# ---------------------------------------------------------------------------
# Top-level subcommands appear as "/grimevasion <subcommand>", e.g. "/grimevasion list"
# "word" and "lists" are subgroups: "/grimevasion word add", "/grimevasion lists show"

grimevasion_group = app_commands.Group(name="grimevasion", description="GrimEvasion proxy bot commands")
client.tree.add_command(grimevasion_group)

word_group = app_commands.Group(name="word", description="Manage this server's trigger words", parent=grimevasion_group)
lists_group = app_commands.Group(name="lists", description="Browse and import default word lists", parent=grimevasion_group)


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
        name="/grimevasion list `page`",
        value="Lists the trigger words configured for this server, with pagination.",
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
        value="Enables or disables an automatic trigger setting (doLinksTrigger, doMediasTrigger). Requires **Manage Server**.",
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


@grimevasion_group.command(name="list", description="List this server's trigger words")
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


@client.event
async def on_message(msg: discord.Message):
    # Ignore bots/webhooks (prevents infinite loops) and DMs
    if msg.author.bot or not msg.guild:
        return

    guild_id = str(msg.guild.id)
    pattern = guild_patterns.get(guild_id)
    settings = get_cached_settings(guild_id)

    word_trigger = bool(pattern and pattern.search(msg.content))
    link_trigger = settings["doLinksTrigger"] and bool(LINK_PATTERN.search(msg.content))
    media_trigger = settings["doMediasTrigger"] and bool(msg.attachments)

    if not (word_trigger or link_trigger or media_trigger):
        return

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

    try:
        await hook.send(
            content=content,
            username=msg.author.display_name,
            avatar_url=msg.author.display_avatar.url,
            files=files,
            thread=thread,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.NotFound:
        # Webhook was deleted manually on Discord's side, clear cache and retry once
        print(f"Cached webhook for #{channel} is invalid, recreating")
        webhooks.pop(channel.id, None)
        try:
            hook = await get_webhook(channel)
            await hook.send(
                content=content,
                username=msg.author.display_name,
                avatar_url=msg.author.display_avatar.url,
                files=files,
                thread=thread,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            print(f"Retry failed for message {msg.id}: {e}")
            return
    except discord.HTTPException as e:
        print(f"Failed to send webhook message for {msg.id}: {e}")
        return

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