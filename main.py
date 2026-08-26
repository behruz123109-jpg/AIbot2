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
from aiogram.enums import ParseMode, ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# ========================================================
# ⚙️  SOZLAMALAR VA YASHIRIN O'ZGARUVCHILAR
# ========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_NAME = "bot_database.db"

# Groq 2026-yil avgustida bir qator modellarni (jumladan llama-3.1-8b-instant)
# rasman yopdi. Shu sabab matn modeli sifatida ularning joriy tavsiyasi -
# gpt-oss oilasi ishlatiladi. Agar birinchi model ishlamasa, avtomatik
# ravishda zaxira modelga o'tiladi (fallback), shunda bot "modellar ishlamayapti"
# muammosiga kelajakda ham chidamli bo'ladi.
GROQ_TEXT_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
GROQ_AUDIO_MODEL = "whisper-large-v3-turbo"  # tezroq va arzonroq, hali faol model
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

if not BOT_TOKEN or not GROQ_API_KEY or not ADMIN_ID:
    raise ValueError("❌ Xatolik: BOT_TOKEN, GROQ_API_KEY yoki ADMIN_ID .env faylida topilmadi!")

try:
    ADMIN_ID_INT = int(ADMIN_ID)
except ValueError:
    raise ValueError("❌ Xatolik: ADMIN_ID faqat raqamlardan iborat bo'lishi kerak (masalan: 123456789)")

# Log fayl + konsolga yozib borish (xatolarni keyinchalik topish osonroq bo'ladi)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Butun bot davomida bitta aiohttp sessiyasidan foydalanamiz (har safar
# yangi sessiya ochish o'rniga) — bu tezroq va resurs tejamkorroq ishlaydi.
http_session: aiohttp.ClientSession | None = None


# --- FSM (STATE) HOLATLAR (Admin uchun) ---
class AdminState(StatesGroup):
    broadcast = State()
    broadcast_confirm = State()
    set_channel = State()
    set_card = State()
    set_owner = State()
    set_phone = State()
    set_price = State()
    give_pro = State()


# ========================================================
# 🗄  MA'LUMOTLAR BAZASI
# ========================================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, plan TEXT DEFAULT 'bepul',
            requests_count INTEGER DEFAULT 0, last_request_week TEXT,
            joined_at TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            remind_at TEXT, text TEXT, is_sent INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )''')
        defaults = [
            ('pro_price', '15000'),
            ('card', '8600 1234 5678 9012'),
            ('owner', 'Abdumannofov Behruz'),
            ('phone', '+998901234567'),
            ('channel', 'Mavjud emas'),
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
    if not channel or channel.lower() in ["mavjud emas", "yox", "yoq", "yo'q", "none"]:
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        # Kanal noto'g'ri kiritilgan yoki bot admin emas bo'lsa - foydalanuvchini
        # bloklab qo'ymaslik uchun ruxsat beramiz, lekin logda ogohlantiramiz.
        logger.warning(f"Obuna tekshiruvida xatolik (kanal={channel}, user={user_id}): {e}")
        return True


async def check_user_limit(user_id: int) -> bool:
    current_week = datetime.datetime.now().strftime("%Y-%W")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT plan, requests_count, last_request_week FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            plan, count, last_week = row
            if plan == 'pro':
                return True

            if last_week != current_week:
                await db.execute(
                    "UPDATE users SET requests_count = 1, last_request_week = ? WHERE user_id = ?",
                    (current_week, user_id),
                )
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
        async with db.execute(
            "SELECT plan, requests_count, last_request_week FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
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
async def _call_groq_chat(model: str, system_prompt: str, user_text: str) -> dict | None:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5,
    }
    try:
        async with http_session.post(GROQ_API_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                res = await resp.json()
                content = res['choices'][0]['message']['content'].strip()
                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").replace("json", "").strip()
                return json.loads(content)
            else:
                error_text = await resp.text()
                logger.error(f"Groq API xatosi (model={model}, status={resp.status}): {error_text}")
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, KeyError) as e:
        logger.error(f"Groq chatga ulanishda xatolik (model={model}): {e}")
        return None


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

    # Bir nechta model bo'yicha ketma-ket urinib ko'ramiz (fallback). Shunday
    # qilib, agar Groq bitta modelni o'chirib qo'ysa ham, bot ishlashda davom etadi.
    for model in GROQ_TEXT_MODELS:
        result = await _call_groq_chat(model, system_prompt, user_text)
        if result is not None:
            return result

    return {"type": "error"}


async def transcribe_voice(file_path: str) -> str:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        with open(file_path, 'rb') as f:
            data = aiohttp.FormData()
            data.add_field('file', f, filename='voice.ogg', content_type='audio/ogg')
            data.add_field('model', GROQ_AUDIO_MODEL)
            async with http_session.post(GROQ_AUDIO_URL, headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    return res_json.get("text", "").strip()
                else:
                    error_text = await resp.text()
                    logger.error(f"Groq Audio API xatosi: {error_text}")
                    return ""
    except Exception as e:
        logger.error(f"Ovozni matnga o'girishda xatolik: {e}")
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
                InlineKeyboardButton(text="⏰ Eslatmalarim", callback_data="ui_reminders"),
            ],
            [InlineKeyboardButton(text="ℹ️ Bot haqida", callback_data="ui_about")],
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
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, last_request_week, joined_at) VALUES (?, ?, ?)",
            (message.from_user.id, datetime.datetime.now().strftime("%Y-%W"), datetime.datetime.now().isoformat()),
        )
        await db.commit()

    text = await get_start_message(message.from_user.first_name)
    await message.answer(text, reply_markup=main_menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "🆘 <b>Botdan qanday foydalanish mumkin:</b>\n\n"
        "📝 Menga oddiy matn yozing — men uni 3 xil uslubda (rasmiy, ijodiy, qisqa) qayta tuzib beraman.\n\n"
        "🎙 Ovozli xabar yuboring — men uni matnga o'giraman va kerak bo'lsa avtomatik tahlil qilaman.\n\n"
        "⏰ \"Ertaga soat 15:00 da Aliyevga qo'ng'iroq qilish\" kabi eslatma yozsangiz — men buni eslatma sifatida saqlab, belgilangan vaqtda sizga xabar beraman.\n\n"
        "👤 /start — Bosh menyu\n"
        "🆔 /myid — Telegram ID raqamingizni ko'rish\n\n"
        f"❓ Savollar bo'lsa admin bilan bog'laning: <a href='tg://user?id={ADMIN_ID_INT}'>shu yerga bosing</a>"
    )
    await message.answer(text, disable_web_page_preview=True)


@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Sizning Telegram ID raqamingiz: <code>{message.from_user.id}</code>")


@dp.callback_query(F.data.startswith("ui_"))
async def ui_callbacks(call: CallbackQuery):
    action = call.data

    if action == "ui_back":
        text = await get_start_message(call.from_user.first_name)
        await safe_edit(call.message, text, main_menu_kb())

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
            [InlineKeyboardButton(text="✅ To'lov qildim (Adminga yozish)", url=f"tg://user?id={ADMIN_ID_INT}")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="ui_back")],
        ])
        await safe_edit(call.message, text, kb)

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
        await safe_edit(call.message, text, back_menu_kb())

    elif action == "ui_reminders":
        await show_user_reminders(call)

    elif action == "ui_about":
        text = (
            "ℹ️ <b>Bot haqida ma'lumot</b>\n\n"
            "Ushbu bot eng so'nggi sun'iy intellekt texnologiyalari asosida ishlaydi.\n\n"
            "Yordam beradigan sohalar:\n"
            "📝 Matn tahrirlash (Rasmiy, Ijodiy, Qisqa)\n"
            "🎙 Ovozli xabarlarni matnga o'girish\n"
            "⏰ Qarzlar va vazifalarni eslatib turish tizimi\n\n"
            "👨‍💻 Dasturchi: <b>Behruz Abdumannofov</b>"
        )
        await safe_edit(call.message, text, back_menu_kb())

    await call.answer()


async def safe_edit(message: types.Message, text: str, kb: InlineKeyboardMarkup):
    """Xabar matnini xavfsiz tahrirlaydi (bir xil matn bo'lsa Telegram xatosini e'tiborsiz qoldiradi)."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def show_user_reminders(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, remind_at, text FROM reminders WHERE user_id=? AND is_sent=0 ORDER BY remind_at ASC LIMIT 10",
            (call.from_user.id,),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        text = "⏰ <b>Sizda hozircha faol eslatmalar yo'q.</b>\n\nMenga \"Ertaga soat 10:00 da hisobot topshirish\" kabi xabar yozing — men avtomatik saqlab qo'yaman."
        await safe_edit(call.message, text, back_menu_kb())
        return

    lines = ["⏰ <b>Sizning faol eslatmalaringiz:</b>\n"]
    buttons = []
    for r_id, remind_at, r_text in rows:
        lines.append(f"📅 <b>{remind_at}</b> — {r_text}")
        buttons.append([InlineKeyboardButton(text=f"🗑 O'chirish: {remind_at}", callback_data=f"delrem_{r_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="ui_back")])

    await safe_edit(call.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("delrem_"))
async def delete_reminder(call: CallbackQuery):
    r_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (r_id, call.from_user.id))
        await db.commit()
    await call.answer("✅ Eslatma o'chirildi")
    await show_user_reminders(call)


# ========================================================
# 💬 AI MATN VA OVOZLI XABARLARNI QABUL QILISH
# ========================================================
async def _handle_ai_result(message: types.Message, result: dict, from_voice: bool = False):
    if result.get("type") == "error":
        await message.answer(
            "❌ Hozircha AI xizmatiga ulanib bo'lmadi. Bu odatda vaqtincha bo'ladi — "
            "iltimos, bir necha soniyadan so'ng qayta urinib ko'ring. Muammo davom etsa, admin xabardor qilinadi."
        )
        return

    if result.get("type") == "reminder":
        remind_at = result.get('remind_at')
        reminder_text = result.get('reminder_text', '')
        if not remind_at or not reminder_text:
            await message.answer("⚠️ Eslatma vaqtini aniqlay olmadim. Iltimos, sana va vaqtni aniqroq yozing (masalan: \"ertaga soat 15:00 da...\").")
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO reminders (user_id, remind_at, text) VALUES (?, ?, ?)",
                (message.from_user.id, remind_at, reminder_text),
            )
            await db.commit()
        prefix = "Ovozli xabardan " if from_voice else ""
        await message.answer(
            f"✅ {prefix}<b>Eslatma saqlandi!</b>\n\n📅 Vaqti: {result.get('formatted_date', remind_at)}\n📝 Matn: {reminder_text}"
        )

    elif result.get("type") == "content":
        v1, v2, v3 = result.get('variant_1', '—'), result.get('variant_2', '—'), result.get('variant_3', '—')
        ans = (
            "🎯 <b>Siz uchun 3 xil mukammal variant:</b>\n\n"
            "💼 <b>1-variant (Rasmiy):</b>\n"
            f"<code>{v1}</code>\n\n"
            "🎨 <b>2-variant (Ijodiy):</b>\n"
            f"<code>{v2}</code>\n\n"
            "⚡ <b>3-variant (Qisqa):</b>\n"
            f"<code>{v3}</code>\n\n"
            "<i>💡 Nusxa olish uchun matn ustiga bosing!</i>"
        )
        await message.answer(ans)
    else:
        await message.answer("⚠️ Kutilmagan javob keldi. Iltimos, xabaringizni boshqacharoq yozib qayta yuboring.")


@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text(message: types.Message):
    if not await check_subscription(message.from_user.id):
        ch = await get_setting("channel")
        await message.answer(f"❌ <b>Botdan foydalanish uchun quyidagi kanalga obuna bo'ling:</b>\n\n👉 {ch}\n\nObuna bo'lgach, xabaringizni qaytadan yuboring.")
        return

    if not await check_user_limit(message.from_user.id):
        await message.answer("🚫 <b>Ushbu hafta uchun bepul limitingiz tugadi! (Haftasiga 2 ta)</b>\n\nCheksiz foydalanish uchun menyu orqali 💎 PRO tarifiga o'ting.", reply_markup=main_menu_kb())
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    wait_msg = await message.answer("⏳ <i>Matn sun'iy intellekt yordamida tahlil qilinmoqda...</i>")
    result = await process_text_with_ai(message.text)

    try:
        await wait_msg.delete()
    except TelegramBadRequest:
        pass

    await _handle_ai_result(message, result)


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if not await check_subscription(message.from_user.id):
        ch = await get_setting("channel")
        await message.answer(f"❌ Kanalga obuna bo'ling: {ch}")
        return

    if not await check_user_limit(message.from_user.id):
        await message.answer("🚫 Ushbu hafta uchun limitingiz tugadi! PRO tarifiga o'ting.", reply_markup=main_menu_kb())
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    wait_msg = await message.answer("🎙 <i>Ovozli xabar matnga o'girilmoqda...</i>")
    file_path = f"voice_{message.from_user.id}_{message.message_id}.ogg"

    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, file_path)

        text = await transcribe_voice(file_path)
        if not text:
            await wait_msg.edit_text("❌ Ovozli xabarni o'qib bo'lmadi. Xabar juda qisqa yoki noaniq bo'lishi, yoki AI xizmatida vaqtincha uzilish bo'lishi mumkin.")
            return

        await wait_msg.edit_text(f"📝 <b>Aniqlangan matn:</b> <i>{text}</i>\n\n⏳ Endi AI tahlil qilmoqda...")
        result = await process_text_with_ai(text)

        try:
            await wait_msg.delete()
        except TelegramBadRequest:
            pass

        await _handle_ai_result(message, result, from_voice=True)
    except Exception as e:
        logger.error(f"Ovozli xabarni qayta ishlashda xatolik: {e}")
        await message.answer("❌ Ovozli xabarni qayta ishlashda kutilmagan xatolik yuz berdi.")
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
        [InlineKeyboardButton(text="👑 PRO berish ID orqali", callback_data="adm_give_pro")],
    ])


def admin_pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Karta raqamini o'zgartirish", callback_data="adm_card")],
        [InlineKeyboardButton(text="👤 Karta egasini o'zgartirish", callback_data="adm_owner")],
        [InlineKeyboardButton(text="📞 Telefon raqamni o'zgartirish", callback_data="adm_phone")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm_back")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_cancel")]])


def broadcast_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, yuborish", callback_data="adm_broad_confirm")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_cancel")],
    ])


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID_INT


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer("🛠 <b>Admin Panelga xush kelibsiz!</b>", reply_markup=admin_main_kb())


@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔️ Sizda ruxsat yo'q.", show_alert=True)
        return
    action = call.data

    if action in ["adm_back", "adm_cancel"]:
        await state.clear()
        await safe_edit(call.message, "🛠 <b>Admin Panel</b>", admin_main_kb())

    elif action == "adm_stat":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                total = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE plan='pro'") as cursor:
                pro_count = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM reminders WHERE is_sent=0") as cursor:
                active_reminders = (await cursor.fetchone())[0]
        text = (
            f"📊 <b>Bot Statistikasi:</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total} ta</b>\n"
            f"💎 PRO foydalanuvchilar: <b>{pro_count} ta</b>\n"
            f"⏰ Faol eslatmalar: <b>{active_reminders} ta</b>"
        )
        await safe_edit(call.message, text, admin_main_kb())

    elif action == "adm_broad":
        await safe_edit(call.message, "📢 Rassilka qilmoqchi bo'lgan xabaringizni yuboring:", cancel_kb())
        await state.set_state(AdminState.broadcast)

    elif action == "adm_broad_confirm":
        data = await state.get_data()
        msg_text = data.get("broadcast_text", "")
        await state.clear()
        await run_broadcast(call.message, msg_text)

    elif action == "adm_channel":
        curr = await get_setting("channel")
        await safe_edit(call.message, f"⚙️ Hozirgi kanal: <b>{curr}</b>\n\nYangi kanal userini kiriting (@ bilan):", cancel_kb())
        await state.set_state(AdminState.set_channel)

    elif action == "adm_pay_menu":
        card, owner, phone = await get_setting("card"), await get_setting("owner"), await get_setting("phone")
        await safe_edit(call.message, f"💳 <b>To'lov ma'lumotlari:</b>\n\nKarta: <code>{card}</code>\nEga: {owner}\nTel: {phone}", admin_pay_kb())

    elif action in ["adm_card", "adm_owner", "adm_phone", "adm_price"]:
        states_map = {"adm_card": AdminState.set_card, "adm_owner": AdminState.set_owner, "adm_phone": AdminState.set_phone, "adm_price": AdminState.set_price}
        await safe_edit(call.message, "Yangi qiymatni kiriting:", cancel_kb())
        await state.set_state(states_map[action])

    elif action == "adm_give_pro":
        await safe_edit(call.message, "👤 PRO bermoqchi bo'lgan foydalanuvchining <b>Telegram ID</b> raqamini kiriting:", cancel_kb())
        await state.set_state(AdminState.give_pro)

    await call.answer()


@dp.message(AdminState.set_card)
async def p_card(m: types.Message, state: FSMContext):
    await set_setting("card", m.text)
    await state.clear()
    await m.answer("✅ Karta yangilandi!", reply_markup=admin_main_kb())


@dp.message(AdminState.set_owner)
async def p_owner(m: types.Message, state: FSMContext):
    await set_setting("owner", m.text)
    await state.clear()
    await m.answer("✅ Ega yangilandi!", reply_markup=admin_main_kb())


@dp.message(AdminState.set_phone)
async def p_phone(m: types.Message, state: FSMContext):
    await set_setting("phone", m.text)
    await state.clear()
    await m.answer("✅ Telefon yangilandi!", reply_markup=admin_main_kb())


@dp.message(AdminState.set_price)
async def p_price(m: types.Message, state: FSMContext):
    price_text = m.text.strip().replace(" ", "")
    if not price_text.isdigit():
        await m.answer("❌ Iltimos, narxni faqat raqamlarda kiriting (masalan: 15000).", reply_markup=cancel_kb())
        return
    await set_setting("pro_price", price_text)
    await state.clear()
    await m.answer("✅ Narx yangilandi!", reply_markup=admin_main_kb())


@dp.message(AdminState.set_channel)
async def p_channel(m: types.Message, state: FSMContext):
    await set_setting("channel", m.text)
    await state.clear()
    await m.answer("✅ Kanal yangilandi!", reply_markup=admin_main_kb())


@dp.message(AdminState.give_pro)
async def p_give_pro(m: types.Message, state: FSMContext):
    try:
        user_id = int(m.text.strip())
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("UPDATE users SET plan='pro' WHERE user_id=?", (user_id,))
            await db.commit()
            if cursor.rowcount == 0:
                await m.answer("⚠️ Bu ID hali botdan foydalanmagan (bazada topilmadi). Foydalanuvchi avval /start bosishi kerak.", reply_markup=admin_main_kb())
                return

        await m.answer(f"✅ {user_id} egasiga muvaffaqiyatli PRO tarif berildi!", reply_markup=admin_main_kb())

        try:
            await bot.send_message(user_id, "🎉 <b>Tabriklaymiz!</b> Admin tomonidan sizga 💎 PRO tarif taqdim etildi. Endi botdan cheksiz foydalanishingiz mumkin!")
        except Exception:
            pass

    except ValueError:
        await m.answer("❌ Xatolik: Iltimos, faqat to'g'ri ID raqamini kiriting (masalan: 12345678).", reply_markup=admin_main_kb())
    finally:
        await state.clear()


@dp.message(AdminState.broadcast)
async def p_broadcast(m: types.Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Iltimos, faqat matnli xabar yuboring.", reply_markup=cancel_kb())
        return
    await state.update_data(broadcast_text=m.text)
    await state.set_state(AdminState.broadcast_confirm)
    preview = (
        "👀 <b>Quyidagi xabar barcha foydalanuvchilarga yuboriladi:</b>\n\n"
        f"—————————\n📢 {m.text}\n—————————\n\n"
        "Tasdiqlaysizmi?"
    )
    await m.answer(preview, reply_markup=broadcast_confirm_kb())


async def run_broadcast(status_message: types.Message, msg: str):
    succ, fail = 0, 0
    w = await status_message.answer("⏳ Yuborilmoqda...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            rows = await c.fetchall()

    for i, row in enumerate(rows):
        try:
            await bot.send_message(row[0], f"📢 <b>E'lon:</b>\n\n{msg}")
            succ += 1
        except Exception:
            fail += 1
        # Telegram flood-limitiga tushib qolmaslik uchun har 25 xabardan keyin ozgina kutamiz
        if i % 25 == 0:
            await asyncio.sleep(1)

    await w.edit_text(f"✅ Yuborildi: {succ} ta\n❌ Yetib bormadi: {fail} ta", reply_markup=admin_main_kb())


# ========================================================
# ⏰ ORQA FON VAZIFASI (ESLATMALAR)
# ========================================================
async def check_reminders():
    while True:
        try:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute(
                    "SELECT id, user_id, text FROM reminders WHERE is_sent=0 AND remind_at <= ?", (now_str,)
                ) as cursor:
                    due = await cursor.fetchall()
                for r_id, u_id, r_text in due:
                    try:
                        await bot.send_message(u_id, f"⏰ <b>ESLATMA VAQTI KELDI:</b>\n\n{r_text}")
                    except Exception as e:
                        logger.warning(f"Eslatma yuborib bo'lmadi (user={u_id}): {e}")
                    await db.execute("UPDATE reminders SET is_sent=1 WHERE id=?", (r_id,))
                    await db.commit()
        except Exception as e:
            logger.error(f"Eslatmalarni tekshirishda xatolik: {e}")
        await asyncio.sleep(30)


# ========================================================
# 🚀 ASOSIY ISHGA TUSHIRISH
# ========================================================
async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    try:
        await init_db()
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(check_reminders())
        logger.info("🚀 Bot to'liq ishga tushdi!")
        await dp.start_polling(bot)
    finally:
        await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot to'xtatildi.")
