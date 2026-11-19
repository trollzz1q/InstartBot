import asyncio
import html
import json
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import content as c

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE = Path(__file__).parent
USERS_FILE = BASE / "users.json"
GUIDE_FILE = BASE / "guide.json"
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # чат Марины, куда падают заявки
CAPTION_LIMIT = 1024  # максимум символов в подписи к фото


# ---------- хранилище пользователей ----------
def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # нет файла или он повреждён
        return {}


def save_users(users: dict) -> None:
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, USERS_FILE)  # атомарно: файл не повредится при сбое


def set_fields(uid: int, **fields) -> dict:
    users = load_users()
    rec = users.setdefault(str(uid), {"id": uid})
    rec.update(fields)
    save_users(users)
    return rec


def update_user(user, **fields) -> dict:
    return set_fields(user.id, name=user.full_name, username=user.username, **fields)


def get_user(uid: int) -> dict:
    return load_users().get(str(uid), {})


def now() -> datetime:
    return datetime.now(c.MSK)


# ---------- ссылки и клавиатуры ----------
def manager_url(request: str | None = None) -> str:
    username = c.MANAGER_USERNAME.strip().lstrip("@")
    url = f"https://t.me/{quote(username)}"
    if request:
        url += f"?text={quote(request)}"
    return url


def final_markup() -> Markup:
    return Markup([
        [Btn("Нейросети", callback_data="apply:neuro")],
        [Btn("Премиум", callback_data="apply:premium")],
        [Btn("Вип", callback_data="apply:vip")],
        [Btn("Задать вопрос", url=manager_url())],
    ])


# ---------- гайд (PDF) ----------
# PDF весит больше 50 МБ, а бот не может загружать такие файлы сам. Поэтому админ один раз
# отправляет PDF боту в личку, бот запоминает file_id и дальше рассылает по нему (без пересылки).
def load_guide() -> dict:
    try:
        return json.loads(GUIDE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_guide(file_id: str, name: str) -> None:
    GUIDE_FILE.write_text(json.dumps({"file_id": file_id, "name": name}, ensure_ascii=False),
                          encoding="utf-8")


def is_uploader(user_id: int) -> bool:
    ids = [x.strip() for x in os.getenv("UPLOAD_IDS", "").split(",")]
    if ADMIN_CHAT_ID:
        ids.append(str(ADMIN_CHAT_ID))
    return str(user_id) in {i for i in ids if i}


async def send_guide(bot, chat_id: int) -> None:
    file_id = load_guide().get("file_id")
    if not file_id:
        log.warning("Гайд не загружен: отправь PDF боту в личку (с аккаунта из UPLOAD_IDS)")
        return
    try:
        await bot.send_document(chat_id, file_id, caption=c.GUIDE_CAPTION)
    except Exception:
        log.warning("Не удалось отправить гайд %s", chat_id)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.document is None or not is_uploader(update.effective_user.id):
        return
    doc = msg.document
    save_guide(doc.file_id, doc.file_name or "guide.pdf")
    await context.bot.send_message(
        update.effective_chat.id, f"Гайд сохранён: {doc.file_name}. Теперь бот отправляет его.")


# ---------- смена «экрана»: новые сообщения -> удалить старые ----------
async def delete_messages(bot, chat_id: int, ids) -> None:
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:  # уже удалено или старше 48 часов
            pass


async def swap_screen(bot, chat_id: int, uid: int, build) -> None:
    """build() отправляет новые сообщения и возвращает те, что считаются «экраном».
    После отправки старые сообщения экрана удаляются."""
    old = set(get_user(uid).get("msgs", []))
    new_ids = [m.message_id for m in await build() if m is not None]
    set_fields(uid, msgs=new_ids)
    await delete_messages(bot, chat_id, old - set(new_ids))


# ---------- шаги ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    bot = context.bot
    update_user(user)

    async def build():
        screen = []
        for text in c.INTRO:
            screen.append(await bot.send_message(chat_id, text))
            await asyncio.sleep(c.STEP_PAUSE)

        for key, text, label in c.VARIANTS:
            button = Markup([[Btn(label, callback_data=f"apply:{key}")]])
            img = c.TARIFFS[key][1]
            file = BASE / img
            # Картинка с текстом в подписи и кнопкой под ней (лимит подписи 1024 символа)
            if file.exists() and len(text) <= CAPTION_LIMIT:
                with file.open("rb") as f:
                    screen.append(await bot.send_photo(chat_id, f, caption=text,
                                                       reply_markup=button))
            else:
                if not file.exists():
                    log.warning("Нет картинки %s, отправляю только текст", img)
                screen.append(await bot.send_message(chat_id, text, reply_markup=button))
                if file.exists():  # текст не влезает в подпись: картинка отдельно
                    with file.open("rb") as f:
                        screen.append(await bot.send_photo(chat_id, f))
            await asyncio.sleep(c.STEP_PAUSE)

        # Гайд остаётся в чате: в «экран» не входит, не удаляется
        await send_guide(bot, chat_id)
        return screen

    await swap_screen(bot, chat_id, user.id, build)
    when = now() + timedelta(minutes=random.randint(*c.FINAL_DELAY_MIN))
    set_fields(user.id, final_at=when.isoformat())
    schedule_final(context.application, user.id, when)


async def send_final(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Через 20-30 минут: «Готов начать?!» с кнопками, старые сообщения удаляются."""
    uid = context.job.data
    set_fields(uid, final_at=None)

    async def build():
        return [await context.bot.send_message(uid, c.TEXT_FINAL, reply_markup=final_markup())]

    try:
        await swap_screen(context.bot, uid, uid, build)
    except Exception:  # например, человек заблокировал бота
        log.warning("Не удалось отправить финальное сообщение %s", uid)


async def apply(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    if key not in c.TARIFFS:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    name = c.TARIFFS[key][0]
    prev_apply = get_user(user.id).get("apply_msg")
    update_user(user, applied=name)

    text = c.APPLY_TEXT.format(name=name)
    # Если Telegram не принял ссылку с готовым текстом, отдаём обычную ссылку
    sent = None
    for url in (manager_url(f"Хочу {name}"), manager_url()):
        try:
            sent = await context.bot.send_message(
                chat_id, text, reply_markup=Markup([[Btn("Написать Марине", url=url)]]))
            break
        except BadRequest:
            log.warning("Telegram не принял ссылку %s", url)
    if sent is None:
        sent = await context.bot.send_message(chat_id, f"{text}\n@{c.MANAGER_USERNAME.lstrip('@')}")

    set_fields(user.id, apply_msg=sent.message_id)
    if prev_apply:  # прошлое сообщение про заявку заменяем новым
        await delete_messages(context.bot, chat_id, [prev_apply])

    await notify_admin(context, user, name)


async def notify_admin(context, user, tariff: str) -> None:
    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID не задан, Марина не получит уведомление")
        return
    if user.username:
        who = f"@{html.escape(user.username)}"
    else:  # у человека нет username - даём ссылку на профиль по id
        who = f'<a href="tg://user?id={user.id}">открыть профиль</a>'
    text = (f"Новая заявка\nИмя: {html.escape(user.full_name or '-')}\n"
            f"Username: {who}\nТариф: {html.escape(tariff)}")
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, text, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Не удалось отправить уведомление Марине")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(update.effective_chat.id,
                                   f"Твой chat id: {update.effective_chat.id}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:  # сбой сети не должен ломать кнопку
        log.warning("Не удалось подтвердить нажатие кнопки")
    data = q.data or ""
    if data.startswith("apply:"):
        await apply(update, context, data[6:])
    else:
        log.warning("Неизвестная кнопка: %r", data)


# ---------- отложенное сообщение ----------
def schedule_final(app: Application, uid: int, when: datetime) -> None:
    name = f"final-{uid}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    app.job_queue.run_once(send_final, when, data=uid, name=name)


async def post_init(app: Application) -> None:
    # Восстановить отложенные сообщения, если бота перезапускали
    for uid, rec in load_users().items():
        if rec.get("final_at"):
            when = max(datetime.fromisoformat(rec["final_at"]), now() + timedelta(seconds=5))
            schedule_final(app, int(uid), when)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.warning("Ошибка: %s", context.error)  # без длинного traceback


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # не печатать токен в логах
    builder = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .post_init(post_init)
    )
    proxy = os.getenv("PROXY_URL")  # например socks5://127.0.0.1:1080 или http://127.0.0.1:7890
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.run_polling()


if __name__ == "__main__":
    main()
