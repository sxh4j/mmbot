import discord
from discord.ext import commands
from discord import app_commands
import os
from dotenv import load_dotenv
import logging
from database import Database
from datetime import datetime
import asyncio
import re
import signal
import sys
from aiohttp import web

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID'))
MM_REQUEST_CHANNEL_ID = int(os.getenv('MM_REQUEST_CHANNEL_ID'))
PVP_REQUEST_CHANNEL_ID = int(os.getenv('PVP_REQUEST_CHANNEL_ID'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))
PROOF_CHANNEL_ID = int(os.getenv('PROOF_CHANNEL_ID'))
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID'))
PVP_TICKET_CATEGORY_ID = int(os.getenv('PVP_TICKET_CATEGORY_ID'))
PORT = int(os.getenv('PORT', 8080))

TIER_ROLES = {
    'trial': int(os.getenv('TRIAL_MIDDLEMAN_ROLE_ID')),
    'middleman': int(os.getenv('MIDDLEMAN_ROLE_ID')),
    'pro': int(os.getenv('PRO_MIDDLEMAN_ROLE_ID')),
    'head': int(os.getenv('HEAD_MIDDLEMAN_ROLE_ID')),
    'owner': int(os.getenv('OWNER_ROLE_ID'))
}

TIER_NAMES = {
    'trial': '<100m/s',
    'middleman': '100-250m/s',
    'pro': '250-500m/s',
    'head': '500+m/s',
    'owner': 'Owner'
}

TIER_LIMITS = {
    'trial': 'Trades under 100M',
    'middleman': 'Trades 100M-250M',
    'pro': 'Trades 250M-500M',
    'head': 'Trades over 500M',
    'owner': 'Owner to MM (fee)'
}

TIER_HIERARCHY = {
    'trial': 1,
    'middleman': 2,
    'pro': 3,
    'head': 4,
    'owner': 5
}

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = False
intents.typing = False

bot = commands.Bot(
    command_prefix='$',
    intents=intents,
    chunk_guilds_at_startup=False,
    max_messages=100
)

db = Database()
ticket_counter = {'mm': 0, 'pvp': 0}
URL_PATTERN = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
_role_cache = {}
_member_cache = {}

# ==================== UTILITY FUNCTIONS ====================

async def safe_discord_request(coro, max_retries=3, base_delay=2):
    """Safely execute a Discord API request with automatic retry"""
    for attempt in range(max_retries):
        try:
            return await coro
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(e.response.headers.get('Retry-After', base_delay * (2 ** attempt)))
                logger.warning(f"Rate limited. Retrying after {retry_after:.2f}s...")
                await asyncio.sleep(retry_after)
                if attempt == max_retries - 1:
                    raise
            elif e.status == 403:
                logger.error(f"Permission denied: {e}")
                raise
            elif e.status >= 500:
                wait_time = base_delay * (2 ** attempt)
                logger.warning(f"Server error {e.status}. Retrying after {wait_time}s...")
                await asyncio.sleep(wait_time)
                if attempt == max_retries - 1:
                    raise
            else:
                raise
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                wait_time = base_delay * (2 ** attempt)
                logger.warning(f"Request timeout. Retrying after {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise
    return None

async def safe_send_message(messageable, *args, **kwargs):
    return await safe_discord_request(messageable.send(*args, **kwargs))

async def safe_interaction_response(interaction, *args, **kwargs):
    try:
        if interaction.response.is_done():
            return await safe_discord_request(interaction.followup.send(*args, **kwargs))
        return await safe_discord_request(interaction.response.send_message(*args, **kwargs))
    except Exception as e:
        logger.error(f"Error sending interaction response: {e}")

async def safe_interaction_defer(interaction, **kwargs):
    try:
        if not interaction.response.is_done():
            return await safe_discord_request(interaction.response.defer(**kwargs))
    except Exception as e:
        logger.error(f"Error deferring interaction: {e}")

async def safe_interaction_followup(interaction, *args, **kwargs):
    return await safe_discord_request(interaction.followup.send(*args, **kwargs))

async def get_member_cached(guild, user_id):
    cache_key = f"{guild.id}_{user_id}"
    if cache_key in _member_cache:
        cache_time, member = _member_cache[cache_key]
        if datetime.utcnow().timestamp() - cache_time < 300:
            return member
    try:
        member = guild.get_member(user_id)
        if not member:
            member = await guild.fetch_member(user_id)
        _member_cache[cache_key] = (datetime.utcnow().timestamp(), member)
        return member
    except discord.NotFound:
        return None
    except Exception as e:
        logger.error(f"Error fetching member {user_id}: {e}")
        return None

def has_middleman_role(member: discord.Member) -> bool:
    cache_key = f"{member.id}_{member.guild.id}"
    cache_time = _role_cache.get(cache_key, {}).get('time', 0)
    if datetime.utcnow().timestamp() - cache_time < 30:
        return _role_cache[cache_key]['result']
    user_roles = [role.id for role in member.roles]
    result = any(role_id in user_roles for role_id in TIER_ROLES.values())
    _role_cache[cache_key] = {'result': result, 'time': datetime.utcnow().timestamp()}
    return result

def get_member_tier(member: discord.Member) -> str:
    """Returns the highest tier role a member has"""
    user_role_ids = [role.id for role in member.roles]
    highest_tier = None
    highest_rank = 0
    
    for tier, role_id in TIER_ROLES.items():
        if role_id in user_role_ids:
            if TIER_HIERARCHY[tier] > highest_rank:
                highest_rank = TIER_HIERARCHY[tier]
                highest_tier = tier
    
    return highest_tier

def can_access_tier(member: discord.Member, ticket_tier: str) -> bool:
    """Check if member's tier is >= ticket tier"""
    member_tier = get_member_tier(member)
    if not member_tier:
        return False
    return TIER_HIERARCHY[member_tier] >= TIER_HIERARCHY[ticket_tier]

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

# ==================== HEALTH CHECK SERVER ====================

async def health_check(request):
    try:
        db_healthy = await db.health_check()
        bot_ready = bot.is_ready()
        status = {
            'status': 'healthy' if (db_healthy and bot_ready) else 'degraded',
            'bot_ready': bot_ready,
            'database': 'connected' if db_healthy else 'disconnected',
            'guilds': len(bot.guilds),
            'uptime': str(datetime.utcnow() - bot.start_time) if hasattr(bot, 'start_time') else 'unknown'
        }
        return web.json_response(status)
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return web.json_response({'status': 'unhealthy', 'error': str(e)}, status=503)

async def start_health_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/ping', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Health check server started on port {PORT}")

def signal_handler(sig, frame):
    logger.info('Received shutdown signal, closing bot...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==================== BOT EVENTS ====================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    
    # Block URLs in ticket channels from non-middlemen
    if message.channel.category_id in [TICKET_CATEGORY_ID, PVP_TICKET_CATEGORY_ID]:
        if not has_middleman_role(message.author):
            if URL_PATTERN.search(message.content):
                try:
                    await message.delete()
                    await safe_send_message(
                        message.channel,
                        f"❌ {message.author.mention} Only middlemen can send links in this ticket.",
                        delete_after=5
                    )
                except discord.Forbidden:
                    logger.error(f"Missing permissions to delete message in {message.channel.name}")
                except Exception as e:
                    logger.error(f"Error handling URL in message: {e}")
    
    await bot.process_commands(message)

@bot.event
async def on_ready():
    bot.start_time = datetime.utcnow()
    logger.info(f'✅ Logged in as {bot.user}')
    logger.info(f'🆔 Bot ID: {bot.user.id}')
    logger.info(f'🌐 Connected to {len(bot.guilds)} guild(s)')
    
    # Connect to database
    await db.connect()
    await db.init_db()
    
    # Leave unauthorized servers
    for guild in bot.guilds:
        if guild.id != GUILD_ID:
            logger.warning(f"⚠️ Unauthorized server: {guild.name} ({guild.id})")
            await guild.leave()
            logger.info(f"👋 Left unauthorized server: {guild.name}")
    
    # Restore persistent views
    await restore_persistent_views()
    
    # Sync commands
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        logger.info(f"✅ Synced {len(synced)} command(s) to guild")
        for cmd in synced:
            logger.info(f"   - /{cmd.name}")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

async def restore_persistent_views():
    """Restore button views on existing setup messages"""
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return
        
        # Restore MM setup message
        mm_msg_data = await db.get_setup_message('mm')
        if mm_msg_data:
            try:
                channel = guild.get_channel(mm_msg_data['channel_id'])
                if channel:
                    message = await channel.fetch_message(mm_msg_data['message_id'])
                    view = CreateMMTicketView()
                    await message.edit(view=view)
                    logger.info("✅ Restored MM ticket button")
            except:
                logger.warning("Could not restore MM setup message")
        
        # Restore PvP setup message
        pvp_msg_data = await db.get_setup_message('pvp')
        if pvp_msg_data:
            try:
                channel = guild.get_channel(pvp_msg_data['channel_id'])
                if channel:
                    message = await channel.fetch_message(pvp_msg_data['message_id'])
                    view = CreatePvPTicketView()
                    await message.edit(view=view)
                    logger.info("✅ Restored PvP ticket button")
            except:
                logger.warning("Could not restore PvP setup message")
    except Exception as e:
        logger.error(f"Error restoring views: {e}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id != GUILD_ID:
        logger.warning(f"⚠️ Bot added to unauthorized server: {guild.name} ({guild.id})")
        await guild.leave()
        logger.info(f"👋 Left unauthorized server: {guild.name}")
    else:
        logger.info(f"✅ Bot joined authorized server: {guild.name}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        try:
            await safe_interaction_response(
                interaction,
                "⚠️ This command is outdated. Commands are being refreshed...",
                ephemeral=True
            )
        except:
            pass
    else:
        logger.error(f"Command tree error: {error}")
        try:
            await safe_interaction_response(
                interaction,
                f"❌ An error occurred: {str(error)[:100]}",
                ephemeral=True
            )
        except:
            pass
# ==================== UI COMPONENTS & MODALS ====================
# Add this after the bot events section in bot.py

# ==================== TIER SELECT VIEW ====================

class TierSelectView(discord.ui.View):
    def __init__(self, ticket_type='mm'):
        super().__init__(timeout=300)
        self.ticket_type = ticket_type
    
    @discord.ui.select(
        placeholder="Select tier based on your trade value",
        options=[
            discord.SelectOption(label="<100m/s", value="trial", description="Trades under 100M"),
            discord.SelectOption(label="100-250m/s", value="middleman", description="Trades 100M-250M"),
            discord.SelectOption(label="250-500m/s", value="pro", description="Trades 250M-500M"),
            discord.SelectOption(label="500+m/s", value="head", description="Trades over 500M"),
            discord.SelectOption(label="Owner", value="owner", description="Owner to MM (fee)")
        ]
    )
    async def tier_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        selected_tier = select.values[0]
        if self.ticket_type == 'mm':
            modal = MMDetailsModal(selected_tier)
        else:
            modal = PvPDetailsModal(selected_tier)
        await interaction.response.send_modal(modal)

# ==================== MM DETAILS MODAL ====================

class MMDetailsModal(discord.ui.Modal, title="Fill out the Format"):
    def __init__(self, tier):
        super().__init__()
        self.tier = tier
        if tier == 'owner':
            self.title = "Owner Tier (Fee Required)"
    
    trader = discord.ui.TextInput(
        label="Who's your Trader?",
        placeholder="EX: 9cv or 705256895711019041",
        required=True,
        max_length=100
    )
    giving = discord.ui.TextInput(
        label="What are you giving?",
        placeholder="EX: Frost Dragon (BE SPECIFIC)",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    receiving = discord.ui.TextInput(
        label="What is the other trader giving?",
        placeholder="EX: 4k Robux (BE SPECIFIC)",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Check for duplicate ticket
        has_duplicate = await db.check_duplicate_mm_ticket(
            interaction.user.id,
            str(self.trader.value),
            self.tier
        )
        if has_duplicate:
            await safe_interaction_response(
                interaction,
                "❌ You already have an open ticket for the same trader and tier. "
                "Please wait for your current ticket to be processed.",
                ephemeral=True
            )
            return
        
        await safe_interaction_defer(interaction, ephemeral=True)
        
        try:
            guild = interaction.guild
            category = guild.get_channel(TICKET_CATEGORY_ID)
            
            if not category:
                await safe_interaction_followup(
                    interaction,
                    "❌ Ticket category not found. Contact an admin.",
                    ephemeral=True
                )
                return
            
            global ticket_counter
            ticket_counter['mm'] += 1
            
            # Set up permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True
                )
            }
            
            # Add tier roles
            tier_level = TIER_HIERARCHY[self.tier]
            for tier_name, role_id in TIER_ROLES.items():
                if TIER_HIERARCHY[tier_name] >= tier_level:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True
                        )
            
            # Create channel
            channel = await safe_discord_request(
                category.create_text_channel(
                    name=f"mm-{self.tier}-{ticket_counter['mm']}",
                    overwrites=overwrites
                )
            )
            
            # Create ticket in database
            ticket_id = await db.create_mm_ticket(
                channel.id,
                interaction.user.id,
                str(self.trader.value),
                str(self.giving.value),
                str(self.receiving.value),
                self.tier
            )
            
            # Create ticket embed
            embed = discord.Embed(
                title="Middleman Request",
                color=0x2B2D31,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Requester", value=interaction.user.mention, inline=True)
            embed.add_field(name="Trader", value=f"{self.trader.value}", inline=True)
            embed.add_field(
                name="Tier",
                value=f"{TIER_NAMES.get(self.tier, self.tier.title())}\n{TIER_LIMITS.get(self.tier)}",
                inline=False
            )
            embed.add_field(
                name=f"{interaction.user.display_name} is giving",
                value=f"{self.giving.value}",
                inline=False
            )
            embed.add_field(
                name="Other trader is giving",
                value=f"{self.receiving.value}",
                inline=False
            )
            
            if self.tier == 'owner':
                embed.add_field(
                    name="Important",
                    value="This trade requires a middleman fee payment before processing.\n"
                          "Please wait for the Owner to provide payment details.",
                    inline=False
                )
            
            embed.set_footer(text=f"Ticket #{ticket_id}")
            
            # Send ticket message with buttons
            view = TicketActionsView(ticket_id, 'mm')
            role_id = TIER_ROLES.get(self.tier)
            role_mention = f"<@&{role_id}>" if role_id else ""
            
            await safe_send_message(channel, content=role_mention, embed=embed, view=view)
            
            # Send review reminder
            review_embed = discord.Embed(
                description="Vouching and rating the MM after trade is required\n"
                           "Copy MM user ID and paste in 'submit a review'",
                color=0x2B2D31
            )
            await safe_send_message(channel, embed=review_embed)
            
            # Log action
            asyncio.create_task(db.log_action(ticket_id, 'mm', 'created', interaction.user.id))
            
            # Log to log channel
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(
                    title="📝 New MM Ticket",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Ticket", value=f"#{ticket_id}", inline=True)
                log_embed.add_field(name="Channel", value=channel.mention, inline=True)
                log_embed.add_field(name="Requester", value=interaction.user.mention, inline=True)
                log_embed.add_field(name="Tier", value=TIER_NAMES.get(self.tier), inline=True)
                asyncio.create_task(safe_send_message(log_channel, embed=log_embed))
            
            await safe_interaction_followup(
                interaction,
                f"✅ Ticket created! Please head to {channel.mention}",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error creating MM ticket: {e}")
            try:
                await safe_interaction_followup(
                    interaction,
                    "❌ An error occurred while creating your ticket. Please contact an administrator.",
                    ephemeral=True
                )
            except:
                pass

# ==================== PVP DETAILS MODAL ====================

class PvPDetailsModal(discord.ui.Modal, title="PvP Trade Details"):
    def __init__(self, tier):
        super().__init__()
        self.tier = tier
    
    opponent = discord.ui.TextInput(
        label="Who are you PvPing with?",
        placeholder="@user1234 or 1187380593516879942",
        required=True,
        max_length=100
    )
    betting = discord.ui.TextInput(
        label="What are you betting?",
        placeholder="2 los 67, 1 garama (BE SPECIFIC)",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    opponent_betting = discord.ui.TextInput(
        label="What is the other player betting?",
        placeholder="4020 Robux (BE SPECIFIC)",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    can_join = discord.ui.TextInput(
        label="Can both users join links?",
        placeholder="YES or NO",
        required=True,
        max_length=3
    )
    pvp_type = discord.ui.TextInput(
        label="Which type of PvP?",
        placeholder="stealing 1v1, rooftop etc",
        required=True,
        max_length=200
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        # Check for duplicate
        has_duplicate = await db.check_duplicate_pvp_ticket(
            interaction.user.id,
            str(self.opponent.value),
            self.tier
        )
        if has_duplicate:
            await safe_interaction_response(
                interaction,
                "❌ You already have an open PvP ticket for the same opponent and tier.",
                ephemeral=True
            )
            return
        
        await safe_interaction_defer(interaction, ephemeral=True)
        
        try:
            guild = interaction.guild
            category = guild.get_channel(PVP_TICKET_CATEGORY_ID)
            
            if not category:
                await safe_interaction_followup(
                    interaction,
                    "❌ PvP ticket category not found. Contact an admin.",
                    ephemeral=True
                )
                return
            
            global ticket_counter
            ticket_counter['pvp'] += 1
            
            # Set up permissions
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True
                )
            }
            
            # Add tier roles
            tier_level = TIER_HIERARCHY[self.tier]
            for tier_name, role_id in TIER_ROLES.items():
                if TIER_HIERARCHY[tier_name] >= tier_level:
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True
                        )
            
            # Create channel
            channel = await safe_discord_request(
                category.create_text_channel(
                    name=f"pvp-{self.tier}-{ticket_counter['pvp']}",
                    overwrites=overwrites
                )
            )
            
            # Create ticket in database
            ticket_id = await db.create_pvp_ticket(
                channel.id,
                interaction.user.id,
                str(self.opponent.value),
                str(self.betting.value),
                str(self.opponent_betting.value),
                str(self.can_join.value).upper(),
                str(self.pvp_type.value),
                self.tier
            )
            
            # Create ticket embed
            embed = discord.Embed(
                title="PvP Service",
                description=f"{interaction.user.mention} **created a PvP ticket**",
                color=0x2B2D31,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Requester", value=interaction.user.mention, inline=True)
            embed.add_field(name="Opponent", value=f"`{self.opponent.value}`", inline=True)
            embed.add_field(
                name="Tier",
                value=f"{TIER_NAMES.get(self.tier)}\n{TIER_LIMITS.get(self.tier)}",
                inline=False
            )
            embed.add_field(
                name=f"{interaction.user.display_name} is betting",
                value=f"{self.betting.value}",
                inline=False
            )
            embed.add_field(
                name="Opponent is betting",
                value=f"{self.opponent_betting.value}",
                inline=False
            )
            embed.add_field(name="Can join links", value=f"`{self.can_join.value.upper()}`", inline=True)
            embed.add_field(name="PvP Type", value=f"`{self.pvp_type.value}`", inline=True)
            embed.set_footer(text=f"PvP Ticket #{ticket_id}")
            
            # Send ticket message
            view = TicketActionsView(ticket_id, 'pvp')
            role_id = TIER_ROLES.get(self.tier)
            role_mention = f"<@&{role_id}>" if role_id else ""
            
            await safe_send_message(channel, content=role_mention, embed=embed, view=view)
            
            # Log action
            asyncio.create_task(db.log_action(ticket_id, 'pvp', 'created', interaction.user.id))
            
            # Log to log channel
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(
                    title="📝 New PvP Ticket",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                log_embed.add_field(name="Ticket", value=f"#{ticket_id}", inline=True)
                log_embed.add_field(name="Channel", value=channel.mention, inline=True)
                log_embed.add_field(name="Requester", value=interaction.user.mention, inline=True)
                asyncio.create_task(safe_send_message(log_channel, embed=log_embed))
            
            await safe_interaction_followup(
                interaction,
                f"✅ PvP Ticket created! {channel.mention}",
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Error creating PvP ticket: {e}")
            try:
                await safe_interaction_followup(
                    interaction,
                    "❌ An error occurred while creating your ticket.",
                    ephemeral=True
                )
            except:
                pass

# ==================== TICKET ACTIONS VIEW ====================

class TicketActionsView(discord.ui.View):
    def __init__(self, ticket_id, ticket_type):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.ticket_type = ticket_type
    
    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="claim_ticket")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.ticket_type == 'mm':
            ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
        else:
            ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
        
        if not ticket:
            await safe_interaction_response(interaction, "❌ Ticket not found.", ephemeral=True)
            return
        
        if not has_middleman_role(interaction.user):
            await safe_interaction_response(
                interaction,
                "❌ Only middlemen can claim tickets.",
                ephemeral=True
            )
            return
        
        if not can_access_tier(interaction.user, ticket['tier']):
            required_tier = TIER_NAMES.get(ticket['tier'])
            await safe_interaction_response(
                interaction,
                f"❌ You need {required_tier} role or higher to claim this ticket.\n"
                f"This ticket requires: {TIER_LIMITS.get(ticket['tier'])}",
                ephemeral=True
            )
            return
        
        if ticket['claimed_by']:
            claimer = await get_member_cached(interaction.guild, ticket['claimed_by'])
            claimer_mention = claimer.mention if claimer else f"<@{ticket['claimed_by']}>"
            await safe_interaction_response(
                interaction,
                f"❌ This ticket has already been claimed by {claimer_mention}",
                ephemeral=True
            )
            return
        
        # Claim the ticket
        if self.ticket_type == 'mm':
            await db.claim_mm_ticket(interaction.channel.id, interaction.user.id)
        else:
            await db.claim_pvp_ticket(interaction.channel.id, interaction.user.id)
        
        # Create claim message
        claim_embed = discord.Embed(
            description=f"{interaction.user.name} will be your middleman",
            color=0x2B2D31
        )
        
        requester = await get_member_cached(interaction.guild, ticket['requester_id'])
        requester_mention = requester.mention if requester else f"<@{ticket['requester_id']}>"
        
        if self.ticket_type == 'mm':
            trader_text = ticket['trader_username']
        else:
            trader_text = ticket['opponent_username']
        
        claim_embed.add_field(
            name="Participants",
            value=f"{requester_mention} {trader_text}",
            inline=False
        )
        
        if ticket['tier'] == 'owner':
            claim_embed.add_field(
                name="Fee Payment Required",
                value="Please ensure the middleman fee is paid before proceeding with the trade.",
                inline=False
            )
        
        await safe_interaction_response(interaction, embed=claim_embed)
        
        # Try to pin the message
        try:
            message = await interaction.original_response()
            await message.pin()
        except:
            pass
        
        # Log action
        asyncio.create_task(db.log_action(ticket['ticket_id'], self.ticket_type, "claimed", interaction.user.id))
        
        # Log to log channel
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(
                title="✅ Ticket Claimed",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            log_embed.add_field(name="Ticket", value=interaction.channel.mention, inline=True)
            log_embed.add_field(name="Claimed by", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Tier", value=TIER_NAMES.get(ticket['tier']), inline=True)
            asyncio.create_task(safe_send_message(log_channel, embed=log_embed))

# ==================== CONFIRMATION VIEW ====================

class ConfirmationView(discord.ui.View):
    def __init__(self, ticket_id, ticket_type):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.ticket_type = ticket_type
        self.confirmations = set()
    
    @discord.ui.button(label="Click to Confirm", style=discord.ButtonStyle.green, custom_id="confirm_trade")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        
        if user_id in self.confirmations:
            await safe_interaction_response(interaction, "✅ You already confirmed!", ephemeral=True)
            return
        
        self.confirmations.add(user_id)
        await db.add_confirmation(self.ticket_id, self.ticket_type, user_id)
        
        count = len(self.confirmations)
        confirm_embed = discord.Embed(
            description=f"✅ **{interaction.user.mention} has confirmed ({count}/2)**",
            color=0x2B2D31
        )
        await safe_interaction_response(interaction, embed=confirm_embed)
        
        if count >= 2:
            final_embed = discord.Embed(
                description="**✅ Both users confirmed! Middleman may proceed.**",
                color=discord.Color.green()
            )
            await safe_send_message(interaction.channel, embed=final_embed)

# ==================== CREATE TICKET BUTTONS ====================

class CreateMMTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(
        label="Create Middleman Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="create_mm_ticket"
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = TierSelectView('mm')
        await safe_interaction_response(
            interaction,
            "Please select the middleman tier for your trade:",
            view=view,
            ephemeral=True
        )

class CreatePvPTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(
        label="Create PvP Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="create_pvp_ticket"
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = TierSelectView('pvp')
        await safe_interaction_response(
            interaction,
            "Please select your middleman tier for PvP:",
            view=view,
            ephemeral=True
        )
        # ==================== SLASH COMMANDS ====================
# Add this after the UI components in bot.py

@bot.tree.command(name="setup", description="Setup middleman request button (Admin only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def setup(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await safe_interaction_response(interaction, "❌ Admin only.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="Middleman Services",
        description=(
            "Click the button below to request a middleman for your trade.\n\n"
            "**Available Tiers:**\n"
            "1. <100m/s - Trades under 100M\n"
            "2. 100-250m/s - Trades 100M-250M\n"
            "3. 250-500m/s - Trades 250M-500M\n"
            "4. 500+m/s - Trades over 500M\n"
            "5. Owner - Owner to MM (fee)"
        ),
        color=0x2B2D31
    )
    
    view = CreateMMTicketView()
    msg = await safe_send_message(interaction.channel, embed=embed, view=view)
    
    # Save to database for persistence
    await db.save_setup_message('mm', interaction.channel.id, msg.id)
    
    await safe_interaction_response(interaction, "✅ MM Setup complete!", ephemeral=True)

@bot.tree.command(name="setuppvp", description="Setup PvP request button (Admin only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def setuppvp(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await safe_interaction_response(interaction, "❌ Admin only.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="PvP Services",
        description=(
            "**How PvP Works:**\n"
            "├ MM holds both players' brainrots\n"
            "├ Grab brainrot from red carpet\n"
            "├ Agree on rules (bats only, no blocking)\n"
            "└ Winner receives brainrots after PvP\n\n"
            "**Tiers:**\n"
            "├ <100m/s - Trades under 100M\n"
            "├ 100-250m/s - Trades 100M-250M\n"
            "├ 250-500m/s - Trades 250M-500M\n"
            "├ 500+m/s - Trades over 500M\n"
            "└ Owner - Owner to MM (fee)\n\n"
            "*Choose your middleman for PvP accordingly*"
        ),
        color=0x2B2D31
    )
    
    view = CreatePvPTicketView()
    msg = await safe_send_message(interaction.channel, embed=embed, view=view)
    
    # Save to database for persistence
    await db.save_setup_message('pvp', interaction.channel.id, msg.id)
    
    await safe_interaction_response(interaction, "✅ PvP setup complete!", ephemeral=True)

@bot.tree.command(name="claim", description="Claim a ticket (MM only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def claim_slash(interaction: discord.Interaction):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    if not has_middleman_role(interaction.user):
        await safe_interaction_response(
            interaction,
            "❌ Only middlemen can claim tickets.",
            ephemeral=True
        )
        return
    
    if not can_access_tier(interaction.user, ticket['tier']):
        await safe_interaction_response(
            interaction,
            "❌ You don't have permission to claim this tier ticket.",
            ephemeral=True
        )
        return
    
    if ticket['claimed_by']:
        claimer = await get_member_cached(interaction.guild, ticket['claimed_by'])
        claimer_mention = claimer.mention if claimer else f"<@{ticket['claimed_by']}>"
        await safe_interaction_response(
            interaction,
            f"❌ Already claimed by {claimer_mention}",
            ephemeral=True
        )
        return
    
    if ticket_type == 'mm':
        await db.claim_mm_ticket(interaction.channel.id, interaction.user.id)
    else:
        await db.claim_pvp_ticket(interaction.channel.id, interaction.user.id)
    
    claim_embed = discord.Embed(
        description=f"**✅ {interaction.user.mention} will be your middleman**",
        color=0x2B2D31
    )
    await safe_interaction_response(interaction, embed=claim_embed)
    await db.log_action(ticket['ticket_id'], ticket_type, "claimed", interaction.user.id)

@bot.tree.command(name="unclaim", description="Unclaim a ticket (MM/Admin only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def unclaim_slash(interaction: discord.Interaction):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    if not ticket['claimed_by']:
        await safe_interaction_response(
            interaction,
            "❌ This ticket is not claimed.",
            ephemeral=True
        )
        return
    
    if ticket['claimed_by'] != interaction.user.id and not is_admin(interaction.user):
        await safe_interaction_response(
            interaction,
            "❌ Only the claimer or admins can unclaim this ticket.",
            ephemeral=True
        )
        return
    
    if ticket_type == 'mm':
        await db.unclaim_mm_ticket(interaction.channel.id)
    else:
        await db.unclaim_pvp_ticket(interaction.channel.id)
    
    unclaim_embed = discord.Embed(
        description=f"**🔓 {interaction.user.mention} has unclaimed this ticket**\nMiddlemen can now claim it.",
        color=discord.Color.orange()
    )
    await safe_interaction_response(interaction, embed=unclaim_embed)
    await db.log_action(ticket['ticket_id'], ticket_type, "unclaimed", interaction.user.id)

@bot.tree.command(name="add", description="Add a user to the ticket")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to add to this ticket")
async def add_slash(interaction: discord.Interaction, user: discord.Member):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    has_permission = False
    if (ticket['claimed_by'] == interaction.user.id or 
        ticket['requester_id'] == interaction.user.id or 
        has_middleman_role(interaction.user)):
        has_permission = True
    
    if not has_permission:
        await safe_interaction_response(
            interaction,
            "❌ You don't have permission to add users to this ticket.",
            ephemeral=True
        )
        return
    
    if interaction.channel.permissions_for(user).view_channel:
        await safe_interaction_response(
            interaction,
            f"❌ {user.mention} already has access to this ticket.",
            ephemeral=True
        )
        return
    
    try:
        await safe_discord_request(
            interaction.channel.set_permissions(
                user,
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
        )
        asyncio.create_task(db.log_action(
            ticket['ticket_id'],
            ticket_type,
            f"user_added:{user.id}",
            interaction.user.id
        ))
        
        embed = discord.Embed(
            description=f"✅ {user.mention} has been added to the ticket by {interaction.user.mention}",
            color=discord.Color.green()
        )
        await safe_interaction_response(interaction, embed=embed)
    except Exception as e:
        logger.error(f"Error adding user to ticket: {e}")
        await safe_interaction_response(
            interaction,
            "❌ An error occurred while adding the user.",
            ephemeral=True
        )

@bot.tree.command(name="remove", description="Remove a user from the ticket")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to remove from this ticket")
async def remove_slash(interaction: discord.Interaction, user: discord.Member):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    has_permission = False
    if (ticket['claimed_by'] == interaction.user.id or 
        ticket['requester_id'] == interaction.user.id or 
        has_middleman_role(interaction.user)):
        has_permission = True
    
    if not has_permission:
        await safe_interaction_response(
            interaction,
            "❌ You don't have permission to remove users from this ticket.",
            ephemeral=True
        )
        return
    
    if user.id == ticket['requester_id']:
        await safe_interaction_response(
            interaction,
            "❌ You cannot remove the ticket requester.",
            ephemeral=True
        )
        return
    
    if user.id == ticket['claimed_by']:
        await safe_interaction_response(
            interaction,
            "❌ You cannot remove the assigned middleman.",
            ephemeral=True
        )
        return
    
    try:
        await safe_discord_request(interaction.channel.set_permissions(user, overwrite=None))
        asyncio.create_task(db.log_action(
            ticket['ticket_id'],
            ticket_type,
            f"user_removed:{user.id}",
            interaction.user.id
        ))
        
        embed = discord.Embed(
            description=f"✅ {user.mention} has been removed from the ticket by {interaction.user.mention}",
            color=discord.Color.orange()
        )
        await safe_interaction_response(interaction, embed=embed)
    except Exception as e:
        logger.error(f"Error removing user from ticket: {e}")
        await safe_interaction_response(
            interaction,
            "❌ An error occurred while removing the user.",
            ephemeral=True
        )

@bot.tree.command(name="confirm", description="Start trade confirmation (MM/Admin only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def confirm_slash(interaction: discord.Interaction):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    if not (is_admin(interaction.user) or ticket['claimed_by'] == interaction.user.id):
        await safe_interaction_response(
            interaction,
            "❌ Only admins or the claimer can use this.",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="⚠️ Trade Confirmation",
        description=(
            "**Do you confirm the trade?**\n"
            "**Can you join PS links?**\n"
            "**Do you agree to vouch the MM after trade?**\n"
            "**Do you promise to stay at base and keep it locked?**\n\n"
            "Press the button if you have read, agree, and wish to continue.\n\n"
            "*Failure to follow rules may result in blacklist.*"
        ),
        color=0x2B2D31
    )
    
    view = ConfirmationView(ticket['ticket_id'], ticket_type)
    await safe_send_message(interaction.channel, embed=embed, view=view)
    await safe_interaction_response(interaction, "✅ Confirmation started!", ephemeral=True)

@bot.tree.command(name="proof", description="Mark trade complete & send proof (MM only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def proof_slash(interaction: discord.Interaction):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    if not has_middleman_role(interaction.user):
        await safe_interaction_response(
            interaction,
            "❌ Only middlemen can use this command.",
            ephemeral=True
        )
        return
    
    await safe_interaction_defer(interaction, ephemeral=True)
    
    try:
        proof_channel = interaction.guild.get_channel(PROOF_CHANNEL_ID)
        if not proof_channel:
            await safe_interaction_followup(
                interaction,
                "❌ Proof channel not found.",
                ephemeral=True
            )
            return
        
        requester = await get_member_cached(interaction.guild, ticket['requester_id'])
        requester_mention = requester.mention if requester else f"<@{ticket['requester_id']}>"
        requester_name = requester.display_name if requester else "Unknown User"
        
        proof_embed = discord.Embed(
            title="✅ Trade Completed",
            description=f"Trade successfully completed by {interaction.user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        proof_embed.add_field(name="Middleman", value=interaction.user.mention, inline=True)
        proof_embed.add_field(name="Type", value=ticket_type.upper(), inline=True)
        proof_embed.add_field(name="Tier", value=TIER_NAMES.get(ticket['tier']), inline=True)
        proof_embed.add_field(name="Requester", value=requester_mention, inline=True)
        
        if ticket_type == 'mm':
            proof_embed.add_field(name="Trader", value=f"`{ticket['trader_username']}`", inline=True)
            proof_embed.add_field(name=f"{requester_name} gave", value=f"{ticket['giving']}", inline=False)
            proof_embed.add_field(name="Other trader gave", value=f"{ticket['receiving']}", inline=False)
        else:
            proof_embed.add_field(name="Opponent", value=f"`{ticket['opponent_username']}`", inline=True)
            proof_embed.add_field(name="Bet", value=f"{ticket['betting']}", inline=False)
            proof_embed.add_field(name="Opponent Bet", value=f"{ticket['opponent_betting']}", inline=False)
        
        proof_embed.add_field(name="Ticket Channel", value=interaction.channel.mention, inline=True)
        proof_embed.set_footer(text=f"Ticket #{ticket['ticket_id']}")
        
        await safe_send_message(proof_channel, embed=proof_embed)
        
        await db.add_proof(ticket['ticket_id'], ticket_type, interaction.user.id)
        await db.log_action(ticket['ticket_id'], ticket_type, "proof_submitted", interaction.user.id)
        
        await safe_interaction_followup(
            interaction,
            "✅ Trade proof has been sent to the proof channel!",
            ephemeral=True
        )
        
        success_embed = discord.Embed(
            description=f"✅ **Trade completed by {interaction.user.mention}**\nProof submitted!",
            color=discord.Color.green()
        )
        await safe_send_message(interaction.channel, embed=success_embed)
    except Exception as e:
        logger.error(f"Error submitting proof: {e}")
        await safe_interaction_followup(
            interaction,
            "❌ An error occurred while submitting proof.",
            ephemeral=True
        )

@bot.tree.command(name="close", description="Close the current ticket (MM only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def close_slash(interaction: discord.Interaction):
    mm_ticket = await db.get_mm_ticket_by_channel(interaction.channel.id)
    pvp_ticket = await db.get_pvp_ticket_by_channel(interaction.channel.id)
    ticket = mm_ticket or pvp_ticket
    ticket_type = 'mm' if mm_ticket else 'pvp'
    
    if not ticket:
        await safe_interaction_response(
            interaction,
            "❌ This command can only be used in ticket channels.",
            ephemeral=True
        )
        return
    
    if not has_middleman_role(interaction.user):
        await safe_interaction_response(
            interaction,
            "❌ Only middlemen can close tickets.",
            ephemeral=True
        )
        return
    
    await safe_interaction_response(interaction, "🔒 Closing ticket now...", ephemeral=True)
    
    if ticket_type == 'mm':
        await db.close_mm_ticket(interaction.channel.id)
    else:
        await db.close_pvp_ticket(interaction.channel.id)
    
    asyncio.create_task(db.log_action(ticket['ticket_id'], ticket_type, "closed", interaction.user.id))
    
    log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(
            title="🔒 Ticket Closed",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        log_embed.add_field(name="Ticket", value=interaction.channel.name, inline=True)
        log_embed.add_field(name="Closed by", value=interaction.user.mention, inline=True)
        asyncio.create_task(safe_send_message(log_channel, embed=log_embed))
    
    await asyncio.sleep(1)
    await safe_discord_request(interaction.channel.delete(reason=f"Ticket closed by {interaction.user}"))

@bot.tree.command(name="stats", description="View bot statistics (Admin only)")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def stats(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await safe_interaction_response(
            interaction,
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )
        return
    
    await safe_interaction_defer(interaction, ephemeral=True)
    
    try:
        mm_open = await db.get_open_mm_tickets()
        pvp_open = await db.get_open_pvp_tickets()
        mm_total = await db.get_all_mm_tickets_count()
        pvp_total = await db.get_all_pvp_tickets_count()
        
        embed = discord.Embed(
            title="📊 Bot Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(
            name="MM Tickets",
            value=f"Total: `{mm_total}`\nOpen: `{len(mm_open)}`\nClosed: `{mm_total - len(mm_open)}`",
            inline=True
        )
        embed.add_field(
            name="PvP Tickets",
            value=f"Total: `{pvp_total}`\nOpen: `{len(pvp_open)}`\nClosed: `{pvp_total - len(pvp_open)}`",
            inline=True
        )
        embed.add_field(
            name="Overall",
            value=f"Total: `{mm_total + pvp_total}`\nOpen: `{len(mm_open) + len(pvp_open)}`",
            inline=True
        )
        
        embed.set_footer(text=f"Bot Statistics • {bot.user.name}")
        await safe_interaction_followup(interaction, embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        await safe_interaction_followup(
            interaction,
            "❌ An error occurred while fetching statistics.",
            ephemeral=True
        )

@bot.tree.command(name="mmstats", description="Check middleman statistics")
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(middleman="The middleman to check stats for")
async def mmstats_cmd(interaction: discord.Interaction, middleman: discord.Member):
    try:
        stats = await db.get_mm_stats(middleman.id)
        
        if stats['total'] == 0:
            embed = discord.Embed(
                title="Middleman Stats",
                description=f"{middleman.mention}\n\n**Total Tickets Completed: 0**\n\nThis middleman hasn't completed any tickets yet.",
                color=0x2B2D31
            )
            await safe_interaction_response(interaction, embed=embed, ephemeral=True)
            return
        
        rankings = await db.get_mm_rankings()
        rank = next((i+1 for i, r in enumerate(rankings) if r['middleman_id'] == middleman.id), None)
        
        embed = discord.Embed(
            title="Middleman Stats",
            description=f"{middleman.mention}",
            color=0x2B2D31,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Total Tickets", value=f"`{stats['total']}`", inline=True)
        embed.add_field(name="MM Tickets", value=f"`{stats['mm']}`", inline=True)
        embed.add_field(name="PvP Tickets", value=f"`{stats['pvp']}`", inline=True)
        
        if rank:
            embed.add_field(
                name="Rank",
                value=f"`#{rank}` out of `{len(rankings)}` active middlemen",
                inline=False
            )
        
        embed.set_footer(text=f"Stats for {middleman.display_name}")
        await safe_interaction_response(interaction, embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error fetching MM stats: {e}")
        await safe_interaction_response(
            interaction,
            "❌ An error occurred while fetching stats.",
            ephemeral=True
        )

@bot.tree.command(name="help", description="Display help information about the bot")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡 Middleman Bot Help",
        description="Here are all the available commands:",
        color=discord.Color.blue()
    )
    
    if is_admin(interaction.user):
        embed.add_field(
            name="Admin Commands",
            value=(
                "/setup - Setup MM ticket button\n"
                "/setuppvp - Setup PvP ticket button\n"
                "/stats - View bot statistics"
            ),
            inline=False
        )
    
    if has_middleman_role(interaction.user):
        embed.add_field(
            name="Middleman Commands",
            value=(
                "/claim - Claim ticket\n"
                "/unclaim - Unclaim ticket\n"
                "/confirm - Start confirmation\n"
                "/proof - Mark complete\n"
                "/close - Close ticket\n"
                "/add @user - Add user\n"
                "/remove @user - Remove user"
            ),
            inline=False
        )
    
    embed.add_field(
        name="User Commands",
        value=(
            "/mmstats @user - Check MM stats\n"
            "/help - Show commands\n"
            "/ping - Check bot status"
        ),
        inline=False
    )
    
    embed.add_field(
        name="Tier Information",
        value=(
            "<100m/s - Trades under 100M\n"
            "100-250m/s - Trades 100M-250M\n"
            "250-500m/s - Trades 250M-500M\n"
            "500+m/s - Trades over 500M\n"
            "Owner - Owner to MM (fee)"
        ),
        inline=False
    )
    
    embed.set_footer(text="For support, contact an administrator")
    await safe_interaction_response(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check if the bot is online and responsive")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Bot is online and responsive!",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="📶 Latency", value=f"{latency}ms", inline=True)
    embed.add_field(name="🌐 Status", value="✅ Operational", inline=True)
    embed.add_field(
        name="🗄️ Database",
        value="✅ Connected" if await db.health_check() else "❌ Disconnected",
        inline=True
    )
    
    if hasattr(bot, 'start_time'):
        uptime = datetime.utcnow() - bot.start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m"
        embed.add_field(name="⏱️ Uptime", value=uptime_str, inline=False)
    
    embed.set_footer(
        text=f"Requested by {interaction.user.name}",
        icon_url=interaction.user.display_avatar.url
    )
    
    await safe_interaction_response(interaction, embed=embed, ephemeral=True)

# ==================== MAIN ====================

if __name__ == "__main__":
    async def main():
        async with bot:
            await start_health_server()
            await bot.start(TOKEN)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
