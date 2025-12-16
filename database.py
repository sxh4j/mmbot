import asyncpg
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.pool = None
        self.database_url = os.getenv('DATABASE_URL')
    
    async def connect(self):
        """Connect to Supabase PostgreSQL database"""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            logger.info("✅ Connected to Supabase PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Failed to connect to database: {e}")
            raise
    
    async def init_db(self):
        """Initialize database tables (schema should already exist in Supabase)"""
        try:
            async with self.pool.acquire() as conn:
                # Verify tables exist
                tables = await conn.fetch("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_type = 'BASE TABLE'
                """)
                table_names = [row['table_name'] for row in tables]
                
                required_tables = [
                    'middleman_tickets', 'pvp_tickets', 'ticket_confirmations',
                    'mm_proofs', 'ticket_logs', 'setup_messages'
                ]
                
                for table in required_tables:
                    if table in table_names:
                        logger.info(f"✅ Table '{table}' exists")
                    else:
                        logger.warning(f"⚠️ Table '{table}' not found!")
                
            logger.info("✅ Database initialized")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
    
    # ==================== SETUP MESSAGES (PERSISTENT BUTTONS) ====================
    
    async def save_setup_message(self, message_type: str, channel_id: int, message_id: int):
        """Save or update setup message for persistence"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO setup_messages (message_type, channel_id, message_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (message_type) 
                DO UPDATE SET channel_id = $2, message_id = $3
            """, message_type, channel_id, message_id)
    
    async def get_setup_message(self, message_type: str):
        """Get setup message info"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT channel_id, message_id FROM setup_messages 
                WHERE message_type = $1
            """, message_type)
            return dict(row) if row else None
    
    # ==================== MM TICKETS ====================
    
    async def create_mm_ticket(self, channel_id, requester_id, trader_username, 
                               giving, receiving, tier):
        """Create a new MM ticket"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO middleman_tickets 
                (channel_id, requester_id, trader_username, giving, receiving, tier)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING ticket_id
            """, channel_id, requester_id, trader_username, giving, receiving, tier)
            return row['ticket_id']
    
    async def get_mm_ticket_by_channel(self, channel_id):
        """Get MM ticket by channel ID"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM middleman_tickets WHERE channel_id = $1
                """, channel_id)
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting MM ticket: {e}")
            return None
    
    async def get_mm_ticket_by_id(self, ticket_id):
        """Get MM ticket by ticket ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM middleman_tickets WHERE ticket_id = $1
            """, ticket_id)
            return dict(row) if row else None
    
    async def check_duplicate_mm_ticket(self, requester_id, trader_username, tier):
        """Check if user already has open ticket for same trader/tier"""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT check_duplicate_mm_ticket($1, $2, $3)
            """, requester_id, trader_username, tier)
            return result
    
    async def claim_mm_ticket(self, channel_id, user_id):
        """Mark MM ticket as claimed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE middleman_tickets SET claimed_by = $1 WHERE channel_id = $2
            """, user_id, channel_id)
    
    async def unclaim_mm_ticket(self, channel_id):
        """Mark MM ticket as unclaimed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE middleman_tickets SET claimed_by = NULL WHERE channel_id = $1
            """, channel_id)
    
    async def close_mm_ticket(self, channel_id):
        """Mark MM ticket as closed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE middleman_tickets 
                SET status = 'closed', closed_at = NOW() 
                WHERE channel_id = $1
            """, channel_id)
    
    async def get_open_mm_tickets(self):
        """Get all open MM tickets"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM middleman_tickets 
                WHERE status = 'open' 
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in rows]
    
    async def get_all_mm_tickets_count(self):
        """Get total count of all MM tickets"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) FROM middleman_tickets")
            return row['count']
    
    # ==================== PVP TICKETS ====================
    
    async def create_pvp_ticket(self, channel_id, requester_id, opponent_username, 
                                betting, opponent_betting, can_join_links, pvp_type, tier):
        """Create a new PvP ticket"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO pvp_tickets 
                    (channel_id, requester_id, opponent_username, betting, 
                     opponent_betting, can_join_links, pvp_type, tier)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING ticket_id
                """, channel_id, requester_id, opponent_username, betting, 
                     opponent_betting, can_join_links, pvp_type, tier)
                return row['ticket_id']
        except Exception as e:
            logger.error(f"Error creating PvP ticket: {e}")
            raise
    
    async def get_pvp_ticket_by_channel(self, channel_id):
        """Get PvP ticket by channel ID"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM pvp_tickets WHERE channel_id = $1
                """, channel_id)
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting PvP ticket: {e}")
            return None
    
    async def get_pvp_ticket_by_id(self, ticket_id):
        """Get PvP ticket by ticket ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM pvp_tickets WHERE ticket_id = $1
            """, ticket_id)
            return dict(row) if row else None
    
    async def check_duplicate_pvp_ticket(self, requester_id, opponent_username, tier):
        """Check if user already has open ticket for same opponent/tier"""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("""
                SELECT check_duplicate_pvp_ticket($1, $2, $3)
            """, requester_id, opponent_username, tier)
            return result
    
    async def claim_pvp_ticket(self, channel_id, user_id):
        """Mark PvP ticket as claimed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pvp_tickets SET claimed_by = $1 WHERE channel_id = $2
            """, user_id, channel_id)
    
    async def unclaim_pvp_ticket(self, channel_id):
        """Mark PvP ticket as unclaimed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pvp_tickets SET claimed_by = NULL WHERE channel_id = $1
            """, channel_id)
    
    async def close_pvp_ticket(self, channel_id):
        """Mark PvP ticket as closed"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE pvp_tickets 
                SET status = 'closed', closed_at = NOW() 
                WHERE channel_id = $1
            """, channel_id)
    
    async def get_open_pvp_tickets(self):
        """Get all open PvP tickets"""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM pvp_tickets 
                    WHERE status = 'open' 
                    ORDER BY created_at DESC
                """)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting open PvP tickets: {e}")
            return []
    
    async def get_all_pvp_tickets_count(self):
        """Get total count of all PvP tickets"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT COUNT(*) FROM pvp_tickets")
                return row['count']
        except Exception as e:
            logger.error(f"Error counting PvP tickets: {e}")
            return 0
    
    # ==================== CONFIRMATIONS ====================
    
    async def add_confirmation(self, ticket_id, ticket_type, user_id):
        """Add a confirmation for a ticket"""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ticket_confirmations (ticket_id, ticket_type, user_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (ticket_id, ticket_type, user_id) DO NOTHING
                """, ticket_id, ticket_type, user_id)
        except Exception as e:
            logger.error(f"Error adding confirmation: {e}")
            raise
    
    async def get_confirmations(self, ticket_id, ticket_type):
        """Get all confirmations for a ticket"""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM ticket_confirmations 
                    WHERE ticket_id = $1 AND ticket_type = $2
                """, ticket_id, ticket_type)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting confirmations: {e}")
            return []
    
    # ==================== PROOFS & STATS ====================
    
    async def add_proof(self, ticket_id, ticket_type, middleman_id):
        """Add a proof record for completed ticket"""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO mm_proofs (ticket_id, ticket_type, middleman_id)
                    VALUES ($1, $2, $3)
                """, ticket_id, ticket_type, middleman_id)
        except Exception as e:
            logger.error(f"Error adding proof: {e}")
            raise
    
    async def get_mm_stats(self, middleman_id):
        """Get statistics for a specific middleman"""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN ticket_type = 'mm' THEN 1 ELSE 0 END) as mm,
                        SUM(CASE WHEN ticket_type = 'pvp' THEN 1 ELSE 0 END) as pvp
                    FROM mm_proofs 
                    WHERE middleman_id = $1
                """, middleman_id)
                
                if row:
                    return {
                        'total': row['total'] or 0,
                        'mm': row['mm'] or 0,
                        'pvp': row['pvp'] or 0
                    }
                return {'total': 0, 'mm': 0, 'pvp': 0}
        except Exception as e:
            logger.error(f"Error getting MM stats: {e}")
            return {'total': 0, 'mm': 0, 'pvp': 0}
    
    async def get_mm_rankings(self):
        """Get rankings of all middlemen by total tickets completed"""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM mm_rankings
                """)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting MM rankings: {e}")
            return []
    
    # ==================== LOGS ====================
    
    async def log_action(self, ticket_id, ticket_type, action, user_id):
        """Log an action on a ticket"""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ticket_logs (ticket_id, ticket_type, action, user_id)
                    VALUES ($1, $2, $3, $4)
                """, ticket_id, ticket_type, action, user_id)
        except Exception as e:
            logger.error(f"Error logging action: {e}")
    
    # ==================== HEALTH CHECK ====================
    
    async def health_check(self):
        """Check database health"""
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchrow("SELECT 1")
                return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False
