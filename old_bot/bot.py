import asyncio
import logging
import os
import random
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import messages as m

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE = Path(__file__).parent
PAUSE = 1.0  # пауза между сообщениями, сек


async def send_photo(chat_id: int, path: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    file = BASE / path
    if not file.exists():
        log.warning("Нет картинки %s, пропускаю", path)
        return
    with file.open("rb") as f:
        await context.bot.send_photo(chat_id, f)


async def send_final(context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Нейросети", url=m.URL_NEURO)],
            [InlineKeyboardButton("Премиум", url=m.URL_PREMIUM)],
            [InlineKeyboardButton("Вип", url=m.URL_VIP)],
            [InlineKeyboardButton("Задать вопрос", url=m.URL_ASK)],
        ]
    )
    await context.bot.send_message(context.job.chat_id, m.TEXT_FINAL, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    say = lambda text: context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    steps = [
        (m.TEXT_1, None),
        (m.TEXT_2, None),
        (m.TEXT_3, None),
        (m.TEXT_VARIANT_1, m.IMG_NEURO),
        (m.TEXT_VARIANT_2, m.IMG_PREMIUM),
        (m.TEXT_VARIANT_3, m.IMG_VIP),
        (m.TEXT_GIFT, None),
    ]
    for text, img in steps:
        await say(text)
        if img:
            await send_photo(chat_id, img, context)
        await asyncio.sleep(PAUSE)

    await context.bot.send_message(chat_id, m.GUEST_KEY_URL)

    # Последнее сообщение с кнопками - через 20-30 минут
    delay = random.randint(*m.FINAL_DELAY_MIN) * 60
    context.job_queue.run_once(send_final, delay, chat_id=chat_id)


def main() -> None:
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()


if __name__ == "__main__":
    main()
