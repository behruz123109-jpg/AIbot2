import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from groq import AsyncGroq
from dotenv import load_dotenv

# .env fayldan ma'lumotlarni o'qish
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("GROQ_MODEL", "llama3-70b-8192")

# Loglarni sozlash
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Bot va Dispatcher obyektlari
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "👋 Assalomu alaykum! Men sun'iy intellektga asoslangan aqlli botman.\n"
        "Menga istalgan savolingizni yo'llashingiz mumkin."
    )

@dp.message(F.text)
async def chat_handler(message: types.Message):
    user_text = message.text
    
    # Bot o'ylayotganini ko'rsatish
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        # Groq API orqali so'rov yuborish
        chat_completion = await groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "Siz aqlli va foydali yordamchisiz. Javoblarni o'zbek tilida, tushunarli qilib bering."
                },
                {
                    "role": "user",
                    "content": user_text,
                }
            ],
            model=MODEL_NAME.strip(), # Bo'sh joylarni olib tashlash
            temperature=0.7,
            max_tokens=2048,
        )
        
        response_text = chat_completion.choices[0].message.content

        # Telegram xabar uzunligi chegarasi (4096 belgi) uchun moslash
        if len(response_text) > 4000:
            for i in range(0, len(response_text), 4000):
                await message.reply(response_text[i:i+4000])
        else:
            await message.reply(response_text)

    except Exception as e:
        error_msg = str(e)
        logging.error(f"Xatolik yuz berdi: {error_msg}")
        
        if "model_not_found" in error_msg or "404" in error_msg:
            await message.reply(
                "⚠️ <b>Kechirasiz, ko'rsatilgan AI modeli topilmadi yoki ruxsat etilmagan.</b>\n\n"
                f"Siz so'ragan model: <code>{MODEL_NAME}</code>\n"
                "Iltimos, serverdagi `.env` sozlamalarida `GROQ_MODEL` ni `llama3-70b-8192` ga o'zgartiring."
            )
        else:
            await message.reply("⚠️ Tizimda kutilmagan xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.")

async def main():
    logging.info("🚀 Bot to'liq ishga tushdi va xabarlarni kutmoqda!")
    # To'qnashuvlarni oldini olish uchun eski webhooklarni o'chirish
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot to'xtatildi.")
