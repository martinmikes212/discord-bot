import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import time
from datetime import datetime, timedelta, timezone

# =====================
import os

TOKEN = os.getenv("TOKEN")

# =====================
# NASTAVENÍ
# =====================
ALLOWED_ROLES = {"VELITEL ADMINU", "MAJITEL"}

AUTO_ROLE_NAME = "LEVEL-1"

LOG_CHANNEL_NAMES = ["role log", "role-log"]
HISTORY_CHANNEL_NAMES = ["role history", "role-history"]

# tempmute (soft mute): {user_id: expire_unix}
TEMP_MUTES: dict[int, float] = {}
# antispam upozornění: (user_id, channel_id) -> last_warn_time
LAST_WARN: dict[tuple[int, int], float] = {}

# =====================
# INTENTS
# =====================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # nutné pro tempmute mazání zpráv

bot = commands.Bot(command_prefix="!", intents=intents)

# =====================
# HELPER FUNKCE
# =====================

def has_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in ALLOWED_ROLES for r in member.roles)

def find_text_channel(guild: discord.Guild, names: list[str]) -> discord.TextChannel | None:
    for ch in guild.text_channels:
        if ch.name in names:
            return ch
    return None

async def send_to_logs(guild: discord.Guild, text: str):
    log_ch = find_text_channel(guild, LOG_CHANNEL_NAMES)
    hist_ch = find_text_channel(guild, HISTORY_CHANNEL_NAMES)

    # když kanál neexistuje, nic se nestane (ale bot nespadne)
    if log_ch:
        await log_ch.send(text)
    if hist_ch:
        await hist_ch.send(text)

def format_time_left(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    minutes_total = seconds // 60
    hours = minutes_total // 60
    minutes = minutes_total % 60

    parts = []
    if hours > 0:
        if hours == 1:
            parts.append("1 hodina")
        elif 2 <= hours <= 4:
            parts.append(f"{hours} hodiny")
        else:
            parts.append(f"{hours} hodin")

    if minutes > 0 or not parts:
        if minutes == 1:
            parts.append("1 minuta")
        elif 2 <= minutes <= 4:
            parts.append(f"{minutes} minuty")
        else:
            parts.append(f"{minutes} minut")

    return " ".join(parts)

def abuse_block(interaction: discord.Interaction, actor: discord.Member, target: discord.Member) -> str | None:
    # ochrany proti abuse
    if interaction.guild is None:
        return "Tohle funguje jen na serveru."
    if target.id == actor.id:
        return "Nemůžeš to použít sám na sebe."
    if target.bot:
        return "Nemůžeš to použít na bota."
    if target.id == interaction.guild.owner_id:
        return "Nemůžeš to použít na vlastníka serveru."
    if actor.id != interaction.guild.owner_id and actor.top_role <= target.top_role:
        return "Nemůžeš to použít na hráče se stejnou nebo vyšší rolí než máš ty."
    # bot hierarchie
    me = interaction.guild.me  # type: ignore
    if me and me.top_role <= target.top_role:
        return "Bot je níž nebo stejně vysoko než cílový hráč, nemůžu mu měnit role / moderovat."
    return None

def role_editable(role: discord.Role) -> bool:
    return (not role.is_default()) and (not role.managed)

def can_assign_role(actor: discord.Member, role: discord.Role) -> bool:
    # aktér nesmí přidat/odebrat roli, která je stejně vysoko nebo výš než jeho top role (kromě ownera)
    if actor.id == actor.guild.owner_id:
        return True
    return actor.top_role > role

def bot_can_assign_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me  # type: ignore
    if me is None:
        return False
    return me.top_role > role

# =====================
# READY (SYNC SLASH)
# =====================

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Přihlášen jako {bot.user} | Slash commands synced: {len(synced)}")
    except Exception as e:
        print("❌ Sync fail:", e)

# =====================
# AUTO ROLE (LEVEL-1)
# =====================

@bot.event
async def on_member_join(member: discord.Member):
    role = discord.utils.get(member.guild.roles, name=AUTO_ROLE_NAME)
    if role is None:
        print(f"⚠️ AutoRole: role '{AUTO_ROLE_NAME}' neexistuje.")
        return
    try:
        await member.add_roles(role, reason="AutoRole on join")
        print(f"✅ AutoRole: {member} dostal {AUTO_ROLE_NAME}")
    except Exception as e:
        print("❌ AutoRole error:", e)

# =====================
# TEMPMUTE (mazání zpráv)
# =====================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    exp = TEMP_MUTES.get(message.author.id)
    if exp is not None:
        now = time.time()

        # expirovalo
        if now >= exp:
            TEMP_MUTES.pop(message.author.id, None)
        else:
            # smaž zprávu
            try:
                await message.delete()
            except Exception:
                # nemá Manage Messages, nebo error
                return

            # varování max 1x za 5 sekund v kanálu
            key = (message.author.id, message.channel.id)
            last = LAST_WARN.get(key, 0)
            if now - last >= 5:
                LAST_WARN[key] = now
                left = int(exp - now)
                await message.channel.send(
                    f"{message.author.mention} ještě nemůžeš psát po dobu {format_time_left(left)}."
                )

    # pokud někdy přidáš prefix příkazy, nech to tu:
    await bot.process_commands(message)

# =====================
# PROMOTE / DEMOTE (s logy)
# =====================

@bot.tree.command(name="promote", description="Povýší hráče na vybranou roli (rank).")
@app_commands.describe(user="Koho povýšit", role="Na jaký rank/roli")
async def promote(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Tohle funguje jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor):
        return await interaction.followup.send("Nemáš oprávnění (VELITEL ADMINU / MAJITEL).", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    if not role_editable(role):
        return await interaction.followup.send("Tuhle roli nelze upravovat (managed/@everyone).", ephemeral=True)

    if not can_assign_role(actor, role):
        return await interaction.followup.send("Nemůžeš přidělit roli, která je stejně vysoko nebo výš než tvoje top role.", ephemeral=True)

    if not bot_can_assign_role(interaction.guild, role):
        return await interaction.followup.send("Bot je níž nebo stejně vysoko než tato role. Zvedni roli bota nad ni.", ephemeral=True)

    if role in user.roles:
        return await interaction.followup.send(f"{user.mention} už má roli {role.mention}.", ephemeral=True)

    try:
        await user.add_roles(role, reason=f"Promote by {actor} ({actor.id})")
    except discord.Forbidden:
        return await interaction.followup.send("Nemám oprávnění přidat tuto roli.", ephemeral=True)

    msg = f"Hráč {user.mention} byl povýšen na rank {role.mention} adminem {actor.mention}."
    await send_to_logs(interaction.guild, msg)

    await interaction.followup.send("✅ Hotovo. Zapsáno do logu i historie.", ephemeral=True)


@bot.tree.command(name="demote", description="Degraduje hráče z vybrané role (ranku).")
@app_commands.describe(user="Koho degradovat", role="Jakou roli odebrat")
async def demote(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Tohle funguje jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor):
        return await interaction.followup.send("Nemáš oprávnění (VELITEL ADMINU / MAJITEL).", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    if not role_editable(role):
        return await interaction.followup.send("Tuhle roli nelze upravovat (managed/@everyone).", ephemeral=True)

    if not can_assign_role(actor, role):
        return await interaction.followup.send("Nemůžeš odebrat roli, která je stejně vysoko nebo výš než tvoje top role.", ephemeral=True)

    if not bot_can_assign_role(interaction.guild, role):
        return await interaction.followup.send("Bot je níž nebo stejně vysoko než tato role. Zvedni roli bota nad ni.", ephemeral=True)

    if role not in user.roles:
        return await interaction.followup.send(f"{user.mention} nemá roli {role.mention}.", ephemeral=True)

    try:
        await user.remove_roles(role, reason=f"Demote by {actor} ({actor.id})")
    except discord.Forbidden:
        return await interaction.followup.send("Nemám oprávnění odebrat tuto roli.", ephemeral=True)

    msg = f"Hráč {user.mention} byl degradován z ranku {role.mention} adminem {actor.mention}."
    await send_to_logs(interaction.guild, msg)

    await interaction.followup.send("✅ Hotovo. Zapsáno do logu i historie.", ephemeral=True)

# =====================
# MODERACE: kick / ban / tempban / mute / tempmute / unmute
# =====================

@bot.tree.command(name="kick", description="Kickne hráče ze serveru.")
@app_commands.describe(user="Koho kicknout", reason="Důvod")
async def kick_cmd(interaction: discord.Interaction, user: discord.Member, reason: str = "Bez důvodu"):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor) and not actor.guild_permissions.kick_members:
        return await interaction.followup.send("Nemáš oprávnění na /kick.", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    try:
        await user.kick(reason=f"{reason} | by {actor} ({actor.id})")
        await interaction.followup.send(f"✅ {user.mention} byl kicknut. Důvod: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Nemám oprávnění kicknout tohoto hráče.", ephemeral=True)


@bot.tree.command(name="ban", description="Zabanuje hráče.")
@app_commands.describe(user="Koho zabanovat", reason="Důvod")
async def ban_cmd(interaction: discord.Interaction, user: discord.Member, reason: str = "Bez důvodu"):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor) and not actor.guild_permissions.ban_members:
        return await interaction.followup.send("Nemáš oprávnění na /ban.", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    try:
        await user.ban(reason=f"{reason} | by {actor} ({actor.id})")
        await interaction.followup.send(f"✅ {user.mention} byl zabanován. Důvod: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Nemám oprávnění banovat tohoto hráče.", ephemeral=True)


@bot.tree.command(name="tempban", description="Dočasný ban (po čase se unbanuje).")
@app_commands.describe(user="Koho tempban", minutes="Na kolik minut", reason="Důvod")
async def tempban_cmd(interaction: discord.Interaction, user: discord.Member, minutes: int = 10, reason: str = "Bez důvodu"):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor) and not actor.guild_permissions.ban_members:
        return await interaction.followup.send("Nemáš oprávnění na /tempban.", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    if minutes < 1:
        minutes = 1

    try:
        await user.ban(reason=f"{reason} | TEMPBAN {minutes}m | by {actor} ({actor.id})")
    except discord.Forbidden:
        return await interaction.followup.send("❌ Nemám oprávnění tempbanovat tohoto hráče.", ephemeral=True)

    await interaction.followup.send(
        f"✅ {user.mention} byl zabanován na {minutes} minut. Důvod: {reason}",
        ephemeral=True
    )

    await asyncio.sleep(minutes * 60)

    # unban podle ID
    try:
        await interaction.guild.unban(discord.Object(id=user.id), reason="Tempban vypršel")
    except Exception:
        pass


@bot.tree.command(name="mute", description="Mute přes Discord Timeout.")
@app_commands.describe(user="Koho mute", minutes="Na kolik minut", reason="Důvod")
async def mute_cmd(interaction: discord.Interaction, user: discord.Member, minutes: int = 10, reason: str = "Bez důvodu"):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor) and not actor.guild_permissions.moderate_members:
        return await interaction.followup.send("Nemáš oprávnění na /mute.", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    if minutes < 1:
        minutes = 1

    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    try:
        await user.timeout(until, reason=f"{reason} | by {actor} ({actor.id})")
        await interaction.followup.send(
            f"✅ {user.mention} byl muted (timeout) na {minutes} minut. Důvod: {reason}",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ Nemám oprávnění na timeout (Moderate Members).", ephemeral=True)


@bot.tree.command(name="tempmute", description="Soft tempmute: maže zprávy + píše čas (default 1h 4m).")
@app_commands.describe(user="Koho tempmute", hours="Hodiny (default 1)", minutes="Minuty (default 4)")
async def tempmute_cmd(interaction: discord.Interaction, user: discord.Member, hours: int = 1, minutes: int = 4):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    # na tempmute musí umět mazat zprávy (nebo být MAJITEL/VELITEL)
    if not has_permission(actor) and not actor.guild_permissions.manage_messages:
        return await interaction.followup.send("Nemáš oprávnění na /tempmute.", ephemeral=True)

    err = abuse_block(interaction, actor, user)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    if hours < 0:
        hours = 0
    if minutes < 0:
        minutes = 0

    total = hours * 3600 + minutes * 60
    if total < 60:
        total = 60

    TEMP_MUTES[user.id] = time.time() + total

    await interaction.followup.send(
        f"✅ {user.mention} je tempmute na {format_time_left(total)}. (Zprávy se mu budou mazat.)",
        ephemeral=True
    )


@bot.tree.command(name="unmute", description="Zruší mute (timeout) i tempmute.")
@app_commands.describe(user="Komu zrušit mute")
async def unmute_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        return await interaction.followup.send("Jen na serveru.", ephemeral=True)

    actor = interaction.user
    if not isinstance(actor, discord.Member):
        return await interaction.followup.send("Nepodařilo se načíst tvoje role.", ephemeral=True)

    if not has_permission(actor) and not (actor.guild_permissions.moderate_members or actor.guild_permissions.manage_messages):
        return await interaction.followup.send("Nemáš oprávnění na /unmute.", ephemeral=True)

    # zruš soft tempmute
    TEMP_MUTES.pop(user.id, None)

    # zruš timeout
    try:
        await user.timeout(None, reason=f"Unmute by {actor} ({actor.id})")
    except Exception:
        pass

    await interaction.followup.send(f"✅ Unmute pro {user.mention}.", ephemeral=True)

# =====================
# START
# =====================
bot.run(TOKEN)