import aiosqlite
import logging

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path="middleman.db"):
        self.db_path = db_path
    
    async def init_db(self):
        """Initialize database with required tables"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER UNIQUE,
                    requester_id INTEGER,
                    trader_username TEXT,
                    giving TEXT,
                    receiving TEXT,
                    tier TEXT,
                    claimed_by INTEGER,
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    action TEXT,
                    user_id INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
                )
            """)
            
            await db.commit()
            logger.info("Database initialized successfully")
    
    async def create_ticket(self, channel_id, requester_id, trader_username, giving, receiving, tier):
        """Create a new ticket record"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO tickets (channel_id, requester_id, trader_username, giving, receiving, tier)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (channel_id, requester_id, trader_username, giving, receiving, tier))
            await db.commit()
            return cursor.lastrowid
    
    async def get_ticket_by_channel(self, channel_id):
        """Get ticket by channel ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM tickets WHERE channel_id = ?
            """, (channel_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def get_ticket_by_id(self, ticket_id):
        """Get ticket by ticket ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM tickets WHERE ticket_id = ?
            """, (ticket_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def check_duplicate_ticket(self, requester_id, trader_username, tier):
        """Check if user already has open ticket for same trader/tier"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT ticket_id FROM tickets 
                WHERE requester_id = ? AND trader_username = ? AND tier = ? AND status = 'open'
            """, (requester_id, trader_username, tier)) as cursor:
                row = await cursor.fetchone()
                return row is not None
    
    async def claim_ticket(self, channel_id, user_id):
        """Mark ticket as claimed"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE tickets SET claimed_by = ? WHERE channel_id = ?
            """, (user_id, channel_id))
            await db.commit()
    
    async def close_ticket(self, channel_id):
        """Mark ticket as closed"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP 
                WHERE channel_id = ?
            """, (channel_id,))
            await db.commit()
    
    async def log_action(self, ticket_id, action, user_id):
        """Log an action on a ticket"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO ticket_logs (ticket_id, action, user_id)
                VALUES (?, ?, ?)
            """, (ticket_id, action, user_id))
            await db.commit()
    
    async def get_open_tickets(self):
        """Get all open tickets"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM tickets WHERE status = 'open' ORDER BY created_at DESC
            """) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def get_all_tickets_count(self):
        """Get total count of all tickets"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM tickets") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    
    async def health_check(self):
        """Check database health for monitoring"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False
