import os
import html
import asyncio
import logging

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STORAGE_URL = os.getenv("STORAGE_URL", "").strip().rstrip("/")
STORAGE_KEY = os.getenv("STORAGE_KEY", "").strip()


def _parse_ids(raw: str) -> set[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))

BUCKET = "catalog"
PHOTOS_PER_CATALOG = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("catalog-uploader")

# ================= KATALOGLAR =================
# Kalitlar HTML ilovadagi fayl nomi formatiga mos: fasl_jins_tur  /  tur_joy
SEASON_LBL = {"warm": "Issiq fasl", "cold": "Sovuq fasl"}
GENDER_LBL = {"men": "Erkaklar", "women": "Ayollar"}
TYPE_LBL = {
    "shippak": "Shippaklar", "bosonojka": "Bosonojkalar", "setkali": "Setkali krasofkalar",
    "krasofka": "Krasofkalar", "keta": "Ketalar", "lofer": "Loferlar", "tufli": "Tuflilar",
    "batinka": "Batinkalar", "etik": "Etiklar",
}
PLACE_LBL = {
    "shippak_yard": "🏡 Shippaklar: hovlida kiyish uchun",
    "shippak_room": "🛏 Shippaklar: xonada kiyish uchun",
}

CATALOG_KEYS = [
    "warm_men_shippak", "warm_men_bosonojka", "warm_men_setkali", "warm_men_krasofka",
    "warm_men_keta", "warm_men_lofer", "warm_men_tufli",
    "cold_men_shippak", "cold_men_krasofka", "cold_men_batinka", "cold_men_etik",
    "warm_women_shippak", "warm_women_tufli", "warm_women_setkali", "warm_women_krasofka",
    "warm_women_keta", "warm_women_lofer",
    "cold_women_shippak", "cold_women_krasofka", "cold_women_etik",
    "shippak_yard", "shippak_room",
]


def catalog_label(key: str) -> str:
    if key in PLACE_LBL:
        return PLACE_LBL[key]
    season, gender, ptype = key.split("_")
    icon = "☀️" if season == "warm" else "❄️"
    return f"{icon} {SEASON_LBL[season]}, {GENDER_LBL[gender]}, {TYPE_LBL[ptype]}"


CATALOG_LABELS = {k: catalog_label(k) for k in CATALOG_KEYS}


# ================= BOT =================
class Upload(StatesGroup):
    photos = State()


bot = Bot(BOT_TOKEN or "0:invalid", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
upload_locks: dict[int, asyncio.Lock] = {}   # bir adminning rasmlari ketma-ket (tartib bilan) yuklansin


class AdminOnly(BaseMiddleware):
    """Faqat ADMIN_IDS dagi foydalanuvchilar botdan foydalana oladi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("⛔️ Ruxsat yo'q.")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔️ Ruxsat yo'q.", show_alert=True)
        return None


dp.message.outer_middleware(AdminOnly())
dp.callback_query.outer_middleware(AdminOnly())


# ================= SUPABASE STORAGE =================
async def upload_to_storage(file_name: str, data: bytes) -> None:
    """Rasmni Supabase Storage'dagi 'catalog' bucket'iga yuklaydi. Xato bo'lsa RuntimeError."""
    url = f"{STORAGE_URL}/storage/v1/object/{BUCKET}/{file_name}"
    headers = {
        "apikey": STORAGE_KEY,
        "Content-Type": "image/jpeg",
        "x-upsert": "true",   # shu nomli fayl bor bo'lsa, almashtiriladi (qayta yuklash xato bermaydi)
    }
    if STORAGE_KEY.startswith("eyJ"):   # JWT kalit uchun; yangi sb_... kalitlarda Authorization kerak emas
        headers["Authorization"] = f"Bearer {STORAGE_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status >= 400:
                    body = (await r.text())[:200]
                    raise RuntimeError(f"Storage xatosi (HTTP {r.status}): {body}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise RuntimeError(f"Storage ulanish xatosi: {e}") from e


def err(e: Exception) -> str:
    return html.escape(str(e))[:300]


# ================= KEYBOARDS =================
def kb_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=CATALOG_LABELS[k], callback_data=f"cat:{k}")] for k in CATALOG_KEYS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor", callback_data="cancel")]])


MENU_TEXT = "📚 Qaysi katalog uchun rasm yuklaymiz?"


async def show_menu_edit(cb: CallbackQuery, text: str = MENU_TEXT):
    try:
        await cb.message.edit_text(text, reply_markup=kb_menu())
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ================= HANDLERS =================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(f"👋 Salom, admin!\n\n{MENU_TEXT}", reply_markup=kb_menu())


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(f"❌ Bekor qilindi.\n\n{MENU_TEXT}", reply_markup=kb_menu())


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await show_menu_edit(cb, f"❌ Bekor qilindi.\n\n{MENU_TEXT}")


@dp.callback_query(F.data.startswith("cat:"))
async def cb_catalog(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    if key not in CATALOG_LABELS:
        return await cb.answer("Noma'lum katalog.", show_alert=True)
    await cb.answer()
    await state.set_state(Upload.photos)
    await state.update_data(catalog=key, count=0)
    try:
        await cb.message.edit_text(
            f"📚 Katalog: <b>{CATALOG_LABELS[key]}</b>\n\n"
            f"📷 Shu katalog uchun ketma-ket <b>{PHOTOS_PER_CATALOG} ta</b> rasm yuboring "
            f"(oddiy rasm sifatida, fayl emas).\n"
            f"Fayllar <code>{key}_1.jpg</code> ... <code>{key}_{PHOTOS_PER_CATALOG}.jpg</code> nomi bilan saqlanadi. "
            f"Avvalgi rasmlar almashtiriladi.",
            reply_markup=kb_cancel(),
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@dp.message(Upload.photos, F.photo)
async def msg_photo(msg: Message, state: FSMContext):
    lock = upload_locks.setdefault(msg.from_user.id, asyncio.Lock())
    async with lock:
        # kutish paytida katalog to'lib yoki bekor bo'lib qolgan bo'lishi mumkin (masalan, 5+ rasmli albom)
        if await state.get_state() != Upload.photos.state:
            return
        data = await state.get_data()
        key = data.get("catalog")
        count = int(data.get("count") or 0)
        if key not in CATALOG_LABELS or count >= PHOTOS_PER_CATALOG:
            return

        number = count + 1
        file_name = f"{key}_{number}.jpg"
        try:
            buf = await bot.download(msg.photo[-1])
            await upload_to_storage(file_name, buf.read())
        except Exception as e:
            log.warning("Yuklash xatosi (%s): %s", file_name, e)
            return await msg.answer(f"⚠️ Yuklanmadi: {err(e)}\n\nShu rasmni qayta yuboring.")

        await state.update_data(count=number)
        if number < PHOTOS_PER_CATALOG:
            await msg.answer(f"✅ {number}/{PHOTOS_PER_CATALOG} yuklandi: <code>{file_name}</code>")
            return

        await state.clear()
        names = "\n".join(f"• <code>{key}_{i}.jpg</code>" for i in range(1, PHOTOS_PER_CATALOG + 1))
        await msg.answer(
            f"✅ <b>Muvaffaqiyatli yuklandi!</b>\n{CATALOG_LABELS[key]}\n{names}\n\n{MENU_TEXT}",
            reply_markup=kb_menu(),
        )


@dp.message(Upload.photos, F.document)
async def msg_document(msg: Message):
    await msg.answer("📷 Rasmni fayl emas, oddiy <b>rasm</b> sifatida yuboring (siqish ruxsat berilgan).")


@dp.message(F.text, ~F.text.startswith("/"))
async def msg_text(msg: Message, state: FSMContext):
    if await state.get_state() == Upload.photos.state:
        await msg.answer("📷 Iltimos, rasm yuboring (matn emas). Bekor qilish: /cancel")
    else:
        await msg.answer(MENU_TEXT, reply_markup=kb_menu())


@dp.message(F.photo)
async def msg_photo_no_catalog(msg: Message):
    await msg.answer(f"Avval katalogni tanlang:\n\n{MENU_TEXT}", reply_markup=kb_menu())


# ================= STARTUP =================
async def start_health_server():
    """Render 'Web Service' turi port ochilishini talab qiladi. PORT bo'lmasa o'tkazib yuboriladi."""
    port = os.getenv("PORT")
    if not port:
        return None

    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server %s-portda ishga tushdi.", port)
    return runner


def check_config():
    required = {"BOT_TOKEN": BOT_TOKEN, "ADMIN_IDS": ADMIN_IDS, "STORAGE_URL": STORAGE_URL, "STORAGE_KEY": STORAGE_KEY}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(f"Quyidagi o'zgaruvchilar sozlanmagan: {', '.join(missing)}")


async def main():
    check_config()
    runner = await start_health_server()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
