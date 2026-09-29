import os
import re
import json
import asyncio
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not found: check your .env file")

WORDS_FILE = "words.json"
DELETE_DELAY = 0.1  # seconds to wait before deleting the original message (local display for users is bugged when too low)
PREVIEW_LENGTH = 80  # max characters shown from the replied-to message


def load_words() -> dict:
    if not os.path.exists(WORDS_FILE):
        return {}
    with open(WORDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_words(data: dict) -> None:
    with open(WORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


guild_words = load_words()          # {guild_id (str): [word, ...]}
guild_patterns: dict[str, re.Pattern] = {}  # cache: guild_id -> compiled regex


def build_pattern(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE)


def refresh_pattern(guild_id: str) -> None:
    guild_patterns[guild_id] = build_pattern(guild_words.get(guild_id, []))


for gid in guild_words:
    refresh_pattern(gid)


intents = discord.Intents.default()
intents.message_content = True


class ProxyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
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


@client.tree.command(name="grimproxywordadd", description="Add a word to this server's proxy trigger list")
@app_commands.describe(word="The word to add")
@app_commands.checks.has_permissions(manage_guild=True)
async def grim_proxy_word_add(interaction: discord.Interaction, word: str):
    guild_id = str(interaction.guild_id)
    word = word.strip().lower()

    if not word:
        await interaction.response.send_message("Word cannot be empty.", ephemeral=True)
        return

    words = guild_words.setdefault(guild_id, [])
    if word in words:
        await interaction.response.send_message(f"`{word}` is already in the list.", ephemeral=True)
        return

    words.append(word)
    save_words(guild_words)
    refresh_pattern(guild_id)

    await interaction.response.send_message(f"Added `{word}` to the proxy trigger list.", ephemeral=True)


@grim_proxy_word_add.error
async def grim_proxy_word_add_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the Manage Server permission to use this.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message("Something went wrong.", ephemeral=True)


@client.event
async def on_message(msg: discord.Message):
    # Ignore bots/webhooks (prevents infinite loops) and DMs
    if msg.author.bot or not msg.guild:
        return

    pattern = guild_patterns.get(str(msg.guild.id))
    if not pattern or not pattern.search(msg.content):
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