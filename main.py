import asyncio
import logging
import json
import datetime
import os
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv

# .env faylidagi yashirin o'zgaruvchilarni yuklash
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_NAME = "bot_database.db"

if not BOT_TOKEN or not GROQ_API_KEY or not ADMIN_ID:
    raise ValueError("❌ Xatolik: BOT_TOKEN, GROQ_API_KEY yoki ADMIN_ID topilmadi!")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- FSM (STATE) HOLATLAR (Admin uchun) ---
class AdminState(StatesGroup):
    broadcast = State()
    set_channel = State()
    set_card = State()
    set_owner = State()
    set_phone = State()
    set_price = State()
    give_pro = State()  # YANGA QO'SHILDI: PRO berish uchun state

# ========================================================
# 🗄 MA'LUMOTLAR BAZASI VA SOZLAMALAR
# ========================================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, plan TEXT DEFAULT 'bepul',
            requests_count INTEGER DEFAULT 0, last_request_week TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            remind_at TEXT, text TEXT, is_sent INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )''')
        # Boshlang'ich qiymatlar
        defaults = [
            ('pro_price', '15000'),
            ('card', '8600 1234 5678 9012'),
            ('owner', 'Abdumannofov Behruz'),
            ('phone', '+998901234567'),
            ('channel', 'Mavjud emas')
        ]
        for key, value in defaults:
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def get_setting(key, default="Mavjud emas"):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

# ========================================================
# 🔐 MAJBURIY OBUNA VA HAFTALIK LIMIT
# ========================================================
async def check_subscription(user_id: int) -> bool:
    channel = await get_setting("channel", "Mavjud emas")
    if not channel or channel.lower() in ["mavjud emas", "yox", "yoq", "none"]:
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return True

async def check_user_limit(user_id: int) -> bool:
    current_week = datetime.datetime.now().strftime("%Y-%W")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT plan, requests_count, last_request_week FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            plan, count, last_week = row
            if plan == 'pro':
                return True
                
            if last_week != current_week:
                await db.execute("UPDATE users SET requests_count = 1, last_request_week = ? WHERE user_id = ?", (current_week, user_id))
                await db.commit()
                return True
            else:
                if count >= 2:
                    return False
                await db.execute("UPDATE users SET requests_count = requests_count + 1 WHERE user_id = ?", (user_id,))
                await db.commit()
                return True

async def get_user_stats(user_id: int):
    current_week = datetime.datetime.now().strftime("%Y-%W")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT plan, requests_count, last_request_week FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return ("bepul", 2)
            plan, count, last_week = row
            if plan == 'pro':
                return ("pro", "Cheksiz")
            if last_week != current_week:
                return ("bepul", 2)
            else:
                qolgan = 2 - count
                return ("bepul", qolgan if qolgan > 0 else 0)

# ========================================================
# 🤖 AI VA WHISPER FUNKSIYALARI
# ========================================================
async def process_text_with_ai(user_text: str) -> dict:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        f"Hozirgi vaqt: {now_str}.\n"
        "Siz professional muharrir va eslatma assistentisiz.\n\n"
        "1. AGAR MATNDA ESLATMA / VAZIFA / QARZ BO'LSA:\n"
        "Faqat quyidagi JSON formatida javob bering:\n"
        '{"type": "reminder", "remind_at": "YYYY-MM-DD HH:MM:00", "reminder_text": "Matn", "formatted_date": "Vaqt"}\n\n'
        "2. AGAR ODDIY MATN YOKI E'LON BO'LSA:\n"
        "Mazmunni o'zgartirmasdan, 3 xil uslubda tayyorlang va faqat quyidagi JSON formatida bering:\n"
        '{"type": "content", "variant_1": "Rasmiy uslub", "variant_2": "Ijodiy emojilar bilan", "variant_3": "Qisqa va lo\'nda"}'
    )

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    content = res['choices'][0]['message']['content'].strip()
                    if content.startswith("```"):
                        content = content.strip("`").replace("json\n", "").replace("json", "").strip()
                    return json.loads(content)
                else:
                    error_text = await resp.text()
                    logging.error(f"Groq API Chat xatosi: {error_text}")
                    return {"type": "error"}
        except Exception as e:
            logging.error(f"Koddagi AI (Chat) Xatosi: {e}")
            return {"type": "error"}

async def transcribe_voice(file_path: str) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    async with aiohttp.ClientSession() as session:
        try:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename='voice.ogg', content_type='audio/ogg')
                data.add_field('model', 'whisper-large-v3')
                # TURLI XATOLIKLAR OLDI OLINDI (URL TO'G'RILANDI)
                async with session.post("[https://api.groq.com/openai/v1/audio/transcriptions](https://api.groq.com/openai/v1/audio/transcriptions)", headers=headers, data=data) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        return res_json.get("text", "")
                    else:
                        error_text = await resp.text()
                        logging.error(f"Groq API Audio xatosi: {error_text}")
                        return ""
        except Exception as e:
            logging.error(f"Koddagi AI (Audio) Xatosi: {e}")
            return ""

# ========================================================
# 🎨 FOYDALANUVCHI INTERFEYSI
# ========================================================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 PRO tarifni xarid qilish", callback_data="ui_buy_pro")],
            [
                InlineKeyboardButton(text="👤 Profilim", callback_data="ui_profile"),
                InlineKeyboardButton(text="ℹ️ Bot haqida", callback_data="ui_about")
            ]
        ]
    )

def back_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Asosiy menyuga qaytish", callback_data="ui_back")]]
    )

async def get_start_message(user_name: str) -> str:
    price = await get_setting("pro_price")
    return (
        f"👋 Assalomu alaykum, <b>{user_name}</b>!\n\n"
        f"🤖 <b>Men sizning shaxsiy aqlli yordamchingizman.</b>\n"
        f"Matnlaringizni 3 xil professional variantda tahrirlayman, ovozli xabarlarni matnga aylantiraman va eslatmalar/qarzlar hisobini yuritaman.\n\n"
        f"📊 <b>Foydalanish shartlari:</b>\n"
        f"Barcha foydalanuvchilarga sinab ko'rish uchun <b>haftasiga 2 ta bepul so'rov</b> taqdim etiladi.\n\n"
        f"💎 <b>PRO Tarif imkoniyatlari:</b>\n"
        f"• Cheksiz muloqot va so'rovlar\n"
        f"• Ovozli xabarlarni cheksiz tahlil qilish\n"
        f"• Kutish vaqtsiz tezkor AI xizmati\n\n"
        f"💵 <b>PRO tarif narxi:</b> Oyiga <b>{price} so'm</b>\n\n"
        f"👇 <i>Quyidagi tugmalar orqali xaridni amalga oshirishingiz yoki profilingizni ko'rishingiz mumkin:</i>"
    )

# ========================================================
# 👤 FOYDALANUVCHI BUYRUQLARI
# ========================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, last_request_week) VALUES (?, ?)", 
                         (message.from_user.id, datetime.datetime.now().strftime("%Y-%W")))
        await db.commit()

    text = await get_start_message(message.from_user.first_name)
    await message.answer(text, reply_markup=main_menu_kb())

@dp.callback_query(F.data.startswith("ui_"))
async def ui_callbacks(call: CallbackQuery):
    action = call.data
    
    if action == "ui_back":
        text = await get_start_message(call.from_user.first_name)
        await call.message.edit_text(text, reply_markup=main_menu_kb())
        
    elif action == "ui_buy_pro":
        card, owner, price = await get_setting("card"), await get_setting("owner"), await get_setting("pro_price")
        text = (
            "🌟 <b>PRO tarifiga ulanish</b>\n\n"
            "Cheksiz imkoniyatlarga ega bo'lish uchun to'lovni quyidagi karta raqamiga amalga oshiring:\n\n"
            f"💳 Karta raqami: <code>{card}</code>\n"
            f"👤 Qabul qiluvchi: <b>{owner}</b>\n"
            f"💰 Summa: <b>{price} so'm</b>\n\n"
            "<i>To'lovni amalga oshirgach, chekni Adminga yuboring va hisobingiz darhol PRO tarifiga o'tkaziladi.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ To'lov qildim (Adminga yozish)", url=f"tg://user?id={ADMIN_ID}")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="ui_back")]
        ])
        await call.message.edit_text(text, reply_markup=kb)
        
    elif action == "ui_profile":
        plan, left = await get_user_stats(call.from_user.id)
        status_text = "💎 PRO (Cheksiz)" if plan == 'pro' else "🆓 Bepul"
        limit_text = "Cheksiz" if plan == 'pro' else f"{left} ta so'rov"
        
        text = (
            f"👤 <b>Sizning profilingiz</b>\n\n"
            f"🆔 ID: <code>{call.from_user.id}</code>\n"
            f"👤 Ism: {call.from_user.first_name}\n"
            f"🏷 Status: <b>{status_text}</b>\n\n"
            f"🔄 <b>Qolgan limitlar:</b> {limit_text} (Ushbu hafta uchun)\n\n"
            f"<i>Yozgan har bir matn va ovozli xabaringiz 1 ta so'rov hisoblanadi.</i>"
        )
        await call.message.edit_text(text, reply_markup=back_menu_kb())
        
    elif action == "ui_about":
        text = (
            "ℹ️ <b>Bot haqida ma'lumot</b>\n\n"
            "Ushbu bot eng so'nggi Llama-3 va Whisper sun'iy intellekt texnologiyalari asosida ishlaydi.\n\n"
            "Yordam beradigan sohalar:\n"
            "📝 Matn tahrirlash (Rasmiy, Ijodiy, Qisqa)\n"
            "🎙 Ovozli xabarlarni matnga o'girish\n"
            "⏰ Qarzlar va vazifalarni eslatib turish tizimi\n\n"
            "👨‍💻 Dasturchi: <b>Behruz Abdumannofov</b>"
        )
        await call.message.edit_text(text, reply_markup=back_menu_kb())
        
    await call.answer()

# ========================================================
# 💬 AI MATN VA OVOZLI XABARLARNI QABUL QILISH
# ========================================================
@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text(message: types.Message):
    if not await check_subscription(message.from_user.id):
        ch = await get_setting("channel")
        await message.answer(f"❌ <b>Botdan foydalanish uchun quyidagi kanalga obuna bo'ling:</b>\n\n👉 {ch}\n\nObuna bo'lgach, xabaringizni qaytadan yuboring.")
        return

    if not await check_user_limit(message.from_user.id):
        await message.answer("🚫 <b>Ushbu hafta uchun bepul limitingiz tugadi! (Haftasiga 2 ta)</b>\n\nCheksiz foydalanish uchun menyu orqali 💎 PRO tarifiga o'ting.", reply_markup=main_menu_kb())
        return

    wait_msg = await message.answer("⏳ <i>Matn sun'iy intellekt yordamida tahlil qilinmoqda...</i>")
    result = await process_text_with_ai(message.text)
    
    try:
        await wait_msg.delete()
    except:
        pass

    if result.get("type") == "error":
        await message.answer("❌ Tizimda xatolik yuz berdi. Bot ma'muri loglarni tekshirishi kerak. Iltimos, birozdan so'ng yana urinib ko'ring.")
        return

    if result.get("type") == "reminder":
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO reminders (user_id, remind_at, text) VALUES (?, ?, ?)", 
                             (message.from_user.id, result.get('remind_at'), result.get('reminder_text')))
            await db.commit()
        await message.answer(f"✅ <b>Eslatma saqlandi!</b>\n\n📅 Vaqti: {result.get('formatted_date')}\n📝 Matn: {result.get('reminder_text')}")
        
    elif result.get("type") == "content":
        ans = (
            "🎯 <b>Siz uchun 3 xil mukammal variant:</b>\n\n"
            "💼 <b>1-variant (Rasmiy):</b>\n"
            f"<code>{result.get('variant_1')}</code>\n\n"
            "🎨 <b>2-variant (Ijodiy):</b>\n"
            f"<code>{result.get('variant_2')}</code>\n\n"
            "⚡ <b>3-variant (Qisqa):</b>\n"
            f"<code>{result.get('variant_3')}</code>\n\n"
            "<i>💡 Nusxa olish uchun matn ustiga bosing!</i>"
        )
        await message.answer(ans)

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if not await check_subscription(message.from_user.id):
        ch = await get_setting("channel")
        await message.answer(f"❌ Kanalga obuna bo'ling: {ch}")
        return

    if not await check_user_limit(message.from_user.id):
        await message.answer("🚫 Ushbu hafta uchun limitingiz tugadi! PRO tarifiga o'ting.", reply_markup=main_menu_kb())
        return

    wait_msg = await message.answer("🎙 <i>Ovozli xabar matnga o'girilmoqda...</i>")
    file_path = f"voice_{message.from_user.id}_{message.message_id}.ogg"
    
    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, file_path)
        
        text = await transcribe_voice(file_path)
        if not text:
            await wait_msg.edit_text("❌ Ovozli xabarni o'qib bo'lmadi, qisqa bo'lishi yoki API xatosi bo'lishi mumkin.")
            return
            
        await wait_msg.edit_text(f"📝 <b>Aniqlangan matn:</b> <i>{text}</i>\n\n⏳ Endi AI tahlil qilmoqda...")
        result = await process_text_with_ai(text)
        
        try:
            await wait_msg.delete()
        except:
            pass
        
        if result.get("type") == "error":
            await message.answer("❌ Tizimda AI ishlov berishda xatolik yuz berdi.")
            return
            
        if result.get("type") == "content":
            ans = (
                "🎯 <b>3 xil mukammal variant (Ovozdan):</b>\n\n"
                f"<code>{result.get('variant_1')}</code>\n\n"
                f"<code>{result.get('variant_2')}</code>\n\n"
                f"<code>{result.get('variant_3')}</code>"
            )
            await message.answer(ans)
        elif result.get("type") == "reminder":
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("INSERT INTO reminders (user_id, remind_at, text) VALUES (?, ?, ?)", 
                                 (message.from_user.id, result.get('remind_at'), result.get('reminder_text')))
                await db.commit()
            await message.answer(f"✅ Ovozli xabardan <b>eslatma</b> saqlandi!\n📅 Vaqti: {result.get('formatted_date')}\n📝 Matn: {result.get('reminder_text')}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ========================================================
# 🛠 ADMIN PANEL LOGIKASI
# ========================================================
def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stat"), InlineKeyboardButton(text="📢 Rassilka", callback_data="adm_broad")],
        [InlineKeyboardButton(text="⚙️ Majburiy Obuna", callback_data="adm_channel")],
        [InlineKeyboardButton(text="💳 To'lov sozlamalari", callback_data="adm_pay_menu")],
        [InlineKeyboardButton(text="💰 PRO narxi", callback_data="adm_price")],
        [InlineKeyboardButton(text="👑 PRO berish ID orqali", callback_data="adm_give_pro")] # Tugma o'rnatildi
    ])

def admin_pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Karta raqamini o'zgartirish", callback_data="adm_card")],
        [InlineKeyboardButton(text="👤 Karta egasini o'zgartirish", callback_data="adm_owner")],
        [InlineKeyboardButton(text="📞 Telefon raqamni o'zgartirish", callback_data="adm_phone")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm_back")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_cancel")]])

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if str(message.from_user.id) == str(ADMIN_ID):
        await message.answer("🛠 <b>Admin Panelga xush kelibsiz!</b>", reply_markup=admin_main_kb())

@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(call: CallbackQuery, state: FSMContext):
    if str(call.from_user.id) != str(ADMIN_ID):
        return
    action = call.data
    
    if action in ["adm_back", "adm_cancel"]:
        await state.clear()
        await call.message.edit_text("🛠 <b>Admin Panel</b>", reply_markup=admin_main_kb())
        
    elif action == "adm_stat":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                count = (await cursor.fetchone())[0]
        await call.message.edit_text(f"📊 <b>Bot Statistikasi:</b>\n\nJami foydalanuvchilar: <b>{count} ta</b>", reply_markup=admin_main_kb())
        
    elif action == "adm_broad":
        await call.message.edit_text("📢 Xabaringizni yuboring:", reply_markup=cancel_kb())
        await state.set_state(AdminState.broadcast)
        
    elif action == "adm_channel":
        curr = await get_setting("channel")
        await call.message.edit_text(f"⚙️ Hozirgi kanal: <b>{curr}</b>\n\nYangi kanal userini kiriting (@ bilan):", reply_markup=cancel_kb())
        await state.set_state(AdminState.set_channel)
        
    elif action == "adm_pay_menu":
        card, owner, phone = await get_setting("card"), await get_setting("owner"), await get_setting("phone")
        await call.message.edit_text(f"💳 <b>To'lov ma'lumotlari:</b>\n\nKarta: <code>{card}</code>\nEga: {owner}\nTel: {phone}", reply_markup=admin_pay_kb())
        
    elif action in ["adm_card", "adm_owner", "adm_phone", "adm_price"]:
        states_map = {"adm_card": AdminState.set_card, "adm_owner": AdminState.set_owner, "adm_phone": AdminState.set_phone, "adm_price": AdminState.set_price}
        await call.message.edit_text("Yangi qiymatni kiriting:", reply_markup=cancel_kb())
        await state.set_state(states_map[action])

    # YANGA QO'SHILDI: PRO berish logikasi
    elif action == "adm_give_pro":
        await call.message.edit_text("👤 PRO bermoqchi bo'lgan foydalanuvchining <b>Telegram ID</b> raqamini kiriting:", reply_markup=cancel_kb())
        await state.set_state(AdminState.give_pro)
        
    await call.answer()

# Qisqartirilgan holatlarni saqlash
@dp.message(AdminState.set_card)
async def p_card(m: types.Message, state: FSMContext): await set_setting("card", m.text); await state.clear(); await m.answer("✅ Karta yangilandi!", reply_markup=admin_main_kb())

@dp.message(AdminState.set_owner)
async def p_owner(m: types.Message, state: FSMContext): await set_setting("owner", m.text); await state.clear(); await m.answer("✅ Ega yangilandi!", reply_markup=admin_main_kb())

@dp.message(AdminState.set_phone)
async def p_phone(m: types.Message, state: FSMContext): await set_setting("phone", m.text); await state.clear(); await m.answer("✅ Telefon yangilandi!", reply_markup=admin_main_kb())

@dp.message(AdminState.set_price)
async def p_price(m: types.Message, state: FSMContext): await set_setting("pro_price", m.text); await state.clear(); await m.answer("✅ Narx yangilandi!", reply_markup=admin_main_kb())

@dp.message(AdminState.set_channel)
async def p_channel(m: types.Message, state: FSMContext): await set_setting("channel", m.text); await state.clear(); await m.answer("✅ Kanal yangilandi!", reply_markup=admin_main_kb())

# YANGA QO'SHILDI: PRO berishni qabul qilish
@dp.message(AdminState.give_pro)
async def p_give_pro(m: types.Message, state: FSMContext):
    try:
        user_id = int(m.text.strip())
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET plan='pro' WHERE user_id=?", (user_id,))
            await db.commit()
        await m.answer(f"✅ {user_id} egasiga muvaffaqiyatli PRO tarif berildi!", reply_markup=admin_main_kb())
        
        # Foydalanuvchini ogohlantirishga harakat qilish
        try:
            await bot.send_message(user_id, "🎉 <b>Tabriklaymiz!</b> Admin tomonidan sizga 💎 PRO tarif taqdim etildi. Endi botdan cheksiz foydalanishingiz mumkin!")
        except Exception:
            pass # Foydalanuvchi botni bloklagan bo'lishi mumkin
            
    except ValueError:
        await m.answer("❌ Xatolik: Iltimos, faqat to'g'ri ID raqamini kiriting (masalan: 12345678).", reply_markup=admin_main_kb())
    finally:
        await state.clear()

@dp.message(AdminState.broadcast)
async def p_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    msg = m.text
    succ = 0
    w = await m.answer("⏳ Yuborilmoqda...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            for row in await c.fetchall():
                try:
                    await bot.send_message(row[0], f"📢 <b>E'lon:</b>\n\n{msg}")
                    succ += 1
                except: pass
    await w.edit_text(f"✅ {succ} ta foydalanuvchiga yuborildi.", reply_markup=admin_main_kb())


# ========================================================
# ⏰ ORQA FON VAZIFASI (ESLATMALAR)
# ========================================================
async def check_reminders():
    while True:
        try:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT id, user_id, text FROM reminders WHERE is_sent=0 AND remind_at <= ?", (now_str,)) as cursor:
                    for r_id, u_id, r_text in await cursor.fetchall():
                        try:
                            await bot.send_message(u_id, f"⏰ <b>ESLATMA VAQTI KELDI:</b>\n\n{r_text}")
                            await db.execute("UPDATE reminders SET is_sent=1 WHERE id=?", (r_id,))
                            await db.commit()
                        except: pass
        except: pass
        await asyncio.sleep(30)


# ========================================================
# 🚀 ASOSIY ISHGA TUSHIRISH
# ========================================================
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_reminders())
    print("🚀 Mukammal AI Bot to'liq ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
