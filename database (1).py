import aiosqlite
import datetime

DB_NAME = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                is_premium INTEGER DEFAULT 0,
                language TEXT DEFAULT 'uz',
                weekly_count INTEGER DEFAULT 0,
                last_reset TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                reminder_text TEXT,
                remind_at TEXT,
                is_sent INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def get_or_create_user(user_id: int, full_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, full_name, is_premium, language, weekly_count, last_reset FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d")
            if not user:
                await db.execute(
                    "INSERT INTO users (user_id, full_name, is_premium, language, weekly_count, last_reset) VALUES (?, ?, 0, 'uz', 0, ?)",
                    (user_id, full_name, now_str)
                )
                await db.commit()
                return {"user_id": user_id, "full_name": full_name, "is_premium": 0, "language": 'uz', "weekly_count": 0, "last_reset": now_str}
            else:
                last_reset = user[5]
                if last_reset:
                    try:
                        last_date = datetime.datetime.strptime(last_reset, "%Y-%m-%d")
                        if (datetime.datetime.now() - last_date).days >= 7:
                            await db.execute("UPDATE users SET weekly_count = 0, last_reset = ? WHERE user_id = ?", (now_str, user_id))
                            await db.commit()
                    except Exception:
                        pass
                return {
                    "user_id": user[0],
                    "full_name": user[1],
                    "is_premium": user[2],
                    "language": user[3],
                    "weekly_count": user[4],
                    "last_reset": user[5]
                }

async def check_and_increment_limit(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_premium, weekly_count FROM users WHERE user_id = ?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            if not res:
                return False
            is_premium, weekly_count = res[0], res[1]
            if is_premium == 1:
                return True
            if weekly_count < 2:
                await db.execute("UPDATE users SET weekly_count = weekly_count + 1 WHERE user_id = ?", (user_id,))
                await db.commit()
                return True
            return False

async def set_user_premium(user_id: int, status: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_premium = ? WHERE user_id = ?", (status, user_id))
        await db.commit()

async def set_user_language(user_id: int, lang: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
        await db.commit()

async def get_setting(key: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1") as cursor:
            premium = (await cursor.fetchone())[0]
        return total, premium

async def get_all_user_ids():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def add_reminder(user_id: int, text: str, remind_at: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO reminders (user_id, reminder_text, remind_at, is_sent) VALUES (?, ?, ?, 0)", (user_id, text, remind_at))
        await db.commit()

async def get_pending_reminders():
    async with aiosqlite.connect(DB_NAME) as db:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with db.execute("SELECT id, user_id, reminder_text FROM reminders WHERE is_sent = 0 AND remind_at <= ?", (now_str,)) as cursor:
            return await cursor.fetchall()

async def mark_reminder_sent(rem_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE reminders SET is_sent = 1 WHERE id = ?", (rem_id,))
        await db.commit()
