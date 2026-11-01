import os
import re
import json
import html
import uuid
import asyncio
import logging
import random
import time
from functools import partial

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

load_dotenv()

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
# 1-akkaunt: ma'lumotlar bazasi, 2-akkaunt: mahsulot rasmlari ombori
SUPABASE_URL = os.getenv("account_1_url", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("account_1_key", "").strip()
BOT_SECRET = os.getenv("BOT_SECRET", "").strip()
STORAGE_URL = os.getenv("account_2_url", "").strip().rstrip("/")
STORAGE_KEY = os.getenv("account_2_key", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()

# Model nomlari eskirib turadi, shuning uchun .env orqali almashtirish mumkin
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct").strip()
MISTRAL_MODEL = (os.getenv("MISTRAL_MODEL") or "pixtral-12b-latest").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poyabzal-bot")

# ================= CONSTANTS =================
COLORS = [
    {"uz": "Qizil", "ru": "Красный"}, {"uz": "Och qizil", "ru": "Светло-красный"}, {"uz": "To'q qizil", "ru": "Тёмно-красный"},
    {"uz": "Sariq", "ru": "Жёлтый"}, {"uz": "Och sariq", "ru": "Светло-жёлтый"}, {"uz": "To'q sariq", "ru": "Тёмно-жёлтый"},
    {"uz": "Ko'k", "ru": "Синий"}, {"uz": "Och ko'k", "ru": "Светло-синий"}, {"uz": "To'q ko'k", "ru": "Тёмно-синий"},
    {"uz": "Yashil", "ru": "Зелёный"}, {"uz": "Och yashil", "ru": "Светло-зелёный"}, {"uz": "To'q yashil", "ru": "Тёмно-зелёный"},
    {"uz": "Olovrang", "ru": "Оранжевый"}, {"uz": "Och olovrang", "ru": "Светло-оранжевый"}, {"uz": "To'q olovrang", "ru": "Тёмно-оранжевый"},
    {"uz": "Siyohrang", "ru": "Фиолетовый"}, {"uz": "Och siyohrang", "ru": "Светло-фиолетовый"}, {"uz": "To'q siyohrang", "ru": "Тёмно-фиолетовый"},
    {"uz": "Pushtirang", "ru": "Розовый"}, {"uz": "Och pushtirang", "ru": "Светло-розовый"}, {"uz": "To'q pushtirang", "ru": "Тёмно-розовый"},
    {"uz": "Jigarrang", "ru": "Коричневый"}, {"uz": "Och jigarrang", "ru": "Светло-коричневый"}, {"uz": "To'q jigarrang", "ru": "Тёмно-коричневый"},
    {"uz": "Kulrang", "ru": "Серый"}, {"uz": "Och kulrang", "ru": "Светло-серый"}, {"uz": "To'q kulrang", "ru": "Тёмно-серый"},
    {"uz": "Qora", "ru": "Чёрный"}, {"uz": "Oq", "ru": "Белый"},
    {"uz": "Qaymoqrang", "ru": "Кремовый"}, {"uz": "Och qaymoqrang", "ru": "Светло-кремовый"}, {"uz": "To'q qaymoqrang", "ru": "Тёмно-кремовый"},
]
COLOR_UZ_TO_RU = {c["uz"]: c["ru"] for c in COLORS}
COLOR_UZ_SET = set(COLOR_UZ_TO_RU)
SIZE_MIN, SIZE_MAX = 25, 50

SEASONS = [("warm", "☀️ Issiq fasl"), ("cold", "❄️ Sovuq fasl")]
GENDERS = [("men", "👨 Erkaklar"), ("women", "👩 Ayollar")]
TYPES_BY_SG = {
    ("warm", "men"): [("shippak", "Shippaklar"), ("bosonojka", "Bosonojkalar"), ("setkali", "Setkali krasofkalar"), ("krasofka", "Krasofkalar"), ("keta", "Ketalar"), ("lofer", "Loferlar"), ("tufli", "Tuflilar")],
    ("cold", "men"): [("shippak", "Shippaklar"), ("krasofka", "Krasofkalar"), ("batinka", "Batinkalar"), ("etik", "Etiklar")],
    ("warm", "women"): [("shippak", "Shippaklar"), ("tufli", "Tuflilar"), ("setkali", "Setkali krasofkalar"), ("krasofka", "Krasofkalar"), ("keta", "Ketalar"), ("lofer", "Loferlar")],
    ("cold", "women"): [("shippak", "Shippaklar"), ("krasofka", "Krasofkalar"), ("etik", "Etiklar")],
}
PLACES = [("yard", "🏡 Hovlida kiyish uchun"), ("room", "🛏 Xonada kiyish uchun")]


def quiz_catalog_id(path: dict) -> int:
    key = f"{path.get('season','')}|{path.get('gender','')}|{path.get('type','')}|{path.get('place') or ''}"
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return 1000 + (h % 30000)


def path_label(path: dict) -> str:
    labels = []
    for k, opts in [("season", SEASONS), ("gender", GENDERS)]:
        v = path.get(k)
        for val, lbl in opts:
            if v == val:
                labels.append(lbl.split(" ", 1)[1])
                break
    types = TYPES_BY_SG.get((path.get("season"), path.get("gender")), [])
    for val, lbl in types:
        if path.get("type") == val:
            labels.append(lbl)
            break
    if path.get("place"):
        for val, lbl in PLACES:
            if path["place"] == val:
                labels.append(lbl.split(" ", 1)[1])
                break
    return ", ".join(labels)


AI_SYSTEM_PROMPT = """You are a strict data extractor for an Uzbek shoe marketplace.
TASK: Read a product variant image (usually Chinese seller's card). Return ONLY a JSON object matching the schema below.
SCHEMA:
{
  "colors":   [string],
  "sizes":    [int],
  "priceYuan": number,
  "rating":   number,
  "likes":    string,
  "views":    string,
  "sales":    string,
  "comments": string,
  "commentList": [ { "text": string, "textRu": string, "rating": int } ]
}
RULES:
1. ALLOWED_COLORS: {allowed_colors}. NEVER invent new color names. Map closest.
2. sizes: extract only INTEGER numbers from 25 to 50 (EU sizes). Convert mm to EU roughly if needed. Deduplicate, sort ascending.
3. priceYuan: must be > 0. Ignore crossed-out old prices.
4. commentList: generate 5 realistic comments in natural Uzbek. textRu must be faithful.
5. numbers (likes/views/sales/comments): human-readable K/M format. views > sales > comments > likes.
"""
AI_USER_PROMPT = "Extract all fields from this product variant image per the schema. Return JSON only."


# ================= STATES & BOT =================
class ProductAdd(StatesGroup):
    main_images = State()
    comment_images = State()
    variant_image = State()
    main_cover = State()


class Catalog(StatesGroup):
    season = State()
    gender = State()
    type = State()
    place = State()


bot = Bot(BOT_TOKEN or "0:invalid", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
ai_tasks: dict[int, asyncio.Task] = {}
advance_tasks: dict[int, tuple[str, asyncio.Task]] = {}   # avtomatik o'tish vazifalari: user_id -> (bosqich, task)
last_photo_at: dict[int, float] = {}                      # oxirgi qabul qilingan rasm vaqti (debounce uchun)
user_locks: dict[int, asyncio.Lock] = {}                  # albomdagi rasmlar bir vaqtda kelganda ro'yxat buzilmasligi uchun
busy: set[int] = set()                                    # variant yuklanayotgan / mahsulot yaratilayotgan foydalanuvchilar
ignored_groups: dict[int, str] = {}                       # tugagan bosqichdagi albomning kech kelgan rasmlarini jim o'tkazish
PHOTO_PAUSE = 2.0                                         # rasm qabul qilingach, keyingi bosqichgacha kutish (soniya)


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


# ================= AI FALLBACK =================
PROVIDERS = []
if GEMINI_API_KEY:
    PROVIDERS.append({"name": "Gemini", "client": AsyncOpenAI(api_key=GEMINI_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"), "model": GEMINI_MODEL})
if GROQ_API_KEY:
    PROVIDERS.append({"name": "Groq", "client": AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1"), "model": GROQ_MODEL})
if MISTRAL_API_KEY:
    PROVIDERS.append({"name": "Mistral", "client": AsyncOpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1"), "model": MISTRAL_MODEL})


def _system_prompt() -> str:
    return AI_SYSTEM_PROMPT.replace("{allowed_colors}", ", ".join(c["uz"] for c in COLORS))


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return json.loads(text)


async def analyze_variant(image_url: str) -> dict:
    if not PROVIDERS:
        raise ValueError("Hech qanday AI API kaliti sozlanmagan.")
    last_error = ""
    for provider in PROVIDERS:
        try:
            log.info("[AI] %s orqali tahlil qilinmoqda...", provider["name"])
            resp = await provider["client"].chat.completions.create(
                model=provider["model"],
                messages=[
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": [
                        {"type": "text", "text": AI_USER_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=2000,
            )
            data = _parse_json(resp.choices[0].message.content)
            result = _validate(data)
            log.info("[AI] %s muvaffaqiyatli.", provider["name"])
            return result
        except Exception as e:
            log.warning("[AI XATO] %s ishlamadi: %s", provider["name"], e)
            last_error = str(e)
    raise ValueError(f"Barcha AI provayderlar ishlamadi. Oxirgi xato: {last_error}")


def _validate(d: dict) -> dict:
    colors_uz = [c for c in (d.get("colors") or []) if c in COLOR_UZ_SET]
    colors_uz = list(dict.fromkeys(colors_uz))  # dublikatlarsiz, tartib saqlanadi
    if not colors_uz:
        raise ValueError("Ranglar tayyor ro'yxatga mos kelmadi.")

    sizes = set()
    for s in d.get("sizes") or []:
        try:
            n = int(round(float(s)))
        except (TypeError, ValueError):
            continue
        if SIZE_MIN <= n <= SIZE_MAX:
            sizes.add(n)
    if not sizes:
        raise ValueError("O'lchamlar topilmadi.")

    try:
        price = float(d.get("priceYuan", 0))
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        raise ValueError("Narx topilmadi.")

    try:
        rating = round(float(d.get("rating", 4.9)), 1)
    except (TypeError, ValueError):
        rating = 4.9

    comments = []
    for c in d.get("commentList") or []:
        if not isinstance(c, dict) or not c.get("text"):
            continue
        try:
            r = max(1, min(5, int(c.get("rating", 5))))
        except (TypeError, ValueError):
            r = 5
        comments.append({"text": str(c["text"]), "textRu": str(c.get("textRu", "")), "rating": r})
    if not comments:
        raise ValueError("Komentariyalar yaratilmadi.")

    return {
        "colors": colors_uz,
        "sizes": sorted(sizes),
        "priceYuan": price,
        "rating": rating,
        "likes": str(d.get("likes", "0")),
        "views": str(d.get("views", "0")),
        "sales": str(d.get("sales", "0")),
        "comments": str(d.get("comments", "0")),
        "commentList": comments[:8],
    }


# ================= SUPABASE STORAGE & DB =================
async def upload_image(data: bytes) -> str:
    """Rasmni Supabase Storage'dagi 'products' bucket'iga yuklaydi va ommaviy havolani qaytaradi."""
    file_name = f"{uuid.uuid4().hex}.jpg"
    base_url = STORAGE_URL.replace("/rest/v1", "").rstrip("/")   # manzilda xato bilan /rest/v1 qolib ketgan bo'lsa tozalanadi
    upload_url = f"{base_url}/storage/v1/object/products/{file_name}"
    headers = {
        "apikey": STORAGE_KEY,
        "Authorization": f"Bearer {STORAGE_KEY}",
        "Content-Type": "image/jpeg",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                upload_url,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status >= 400:
                    body = (await r.text())[:200]
                    raise RuntimeError(f"Storage xatosi (HTTP {r.status}): {body}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise RuntimeError(f"Storage ulanish xatosi: {e}") from e
    return f"{base_url}/storage/v1/object/public/products/{file_name}"


async def db_add_product(row: dict) -> dict:
    base_url = SUPABASE_URL.replace("/rest/v1", "").rstrip("/")   # /rest/v1 ikki marta qo'shilib qolmasligi uchun
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{base_url}/rest/v1/rpc/app_bot_add_product",
            json={"p_secret": BOT_SECRET, "p_row": row},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            body = await r.json(content_type=None)
            if r.status >= 400:
                msg = body.get("message") if isinstance(body, dict) else body
                raise RuntimeError(f"Supabase HTTP {r.status}: {msg}")
            return body


async def save_photo(msg: Message) -> str:
    buf = await bot.download(msg.photo[-1])
    return await upload_image(buf.read())


def err(e: Exception) -> str:
    return html.escape(str(e))[:300]


# ================= TEXTS & BUTTONS =================
SEP = "-" * 58
ACCEPTED = "Rasimlar qabul qilindi"
START_TEXT = 'Mahsulot qo\'shishni boshlash uchun\n"Boshlash" tugmasini bosing.'
SEASON_TEXT = "1️⃣ Faslni tanlang:"
GENDER_TEXT = "2️⃣ Kim uchun?"
TYPE_TEXT = "3️⃣ Qanday poyabzal?"
PLACE_TEXT = "4️⃣ Qayerda kiyiladi?"

BTN_PICK = "📁 Katalog tanlash"
BTN_CHANGE = "🔄 Katalogni o'zgartirish"
BTN_CONTINUE = "➡️ Davom etish"
BTN_BACK = "⬅️ Orqaga"
BTN_HOME = "🏠 Bosh sahifa"

PROMPT_MAIN = "Asosiy rasmlar rasmlarni yuboring"
PROMPT_COMMENT = "Komentariya rasmlarini yuboring"
PROMPT_VARIANT = "Variantlar rasmini yuboring"
PROMPT_COVER = "Asosiy rasmini yuboring"


def stage_text(head: str | None, catalog: str, stage: int, prompt: str) -> str:
    """Bosqich xabari: [head] / Katalog / Status / prompt — har biri chiziq bilan ajratilgan."""
    parts = ([head] if head else []) + [f"Katalog: {html.escape(catalog)}", f"Status: {stage}-bosqich", prompt]
    return ("\n" + SEP + "\n").join(parts) + "\n" + SEP


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def kb_start():
    return _kb([[BTN_PICK]])


def kb_process():
    return _kb([[BTN_CHANGE]])


def kb_after_success():
    return _kb([[BTN_CHANGE, BTN_CONTINUE]])


def kb_options(labels: list[str]):
    """Katalog tanlash oynasi: variantlar + Orqaga / Bosh sahifa (pastki klaviaturada)."""
    return _kb([[l] for l in labels] + [[BTN_BACK, BTN_HOME]])


# ================= HELPERS =================
def drop_ai_task(user_id: int) -> None:
    """Foydalanuvchining ishlayotgan (yoki tugagan) AI vazifasini to'xtatib, ro'yxatdan olib tashlaydi."""
    task = ai_tasks.pop(user_id, None)
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()   # "Task exception was never retrieved" ogohlantirishi chiqmasligi uchun


def drop_advance_task(user_id: int) -> None:
    """Kutayotgan avtomatik o'tishni bekor qiladi."""
    item = advance_tasks.pop(user_id, None)
    if item and not item[1].done():
        item[1].cancel()


def reset_flow(user_id: int) -> None:
    """Faqat hozirgi jarayonni to'xtatadi (AI vazifasi + kutayotgan o'tish). Bazadagi mahsulotlarga tegmaydi."""
    drop_ai_task(user_id)
    drop_advance_task(user_id)


def get_lock(user_id: int) -> asyncio.Lock:
    return user_locks.setdefault(user_id, asyncio.Lock())


async def _auto_advance(user_id: int, action) -> None:
    """Oxirgi rasmdan keyin PHOTO_PAUSE soniya jim turilsa, keyingi bosqichga o'tadi (albom uchun debounce)."""
    me = asyncio.current_task()
    try:
        while True:
            wait = PHOTO_PAUSE - (time.monotonic() - last_photo_at.get(user_id, 0.0))
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        await action()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Avtomatik o'tishda xato")
    finally:
        cur = advance_tasks.get(user_id)
        if cur and cur[1] is me:
            advance_tasks.pop(user_id, None)


def ensure_advance(user_id: int, stage_key: str, action) -> None:
    """Rasm qabul qilindi: taymerni yangilaydi va (agar yo'q bo'lsa) shu bosqich uchun avtomatik o'tishni ishga tushiradi."""
    last_photo_at[user_id] = time.monotonic()
    cur = advance_tasks.get(user_id)
    if cur and cur[0] == stage_key and not cur[1].done():
        return
    advance_tasks[user_id] = (stage_key, asyncio.create_task(_auto_advance(user_id, action)))


def ordered_urls(items) -> list[str]:
    """[[message_id, url], ...] ni yuborilish tartibida url'larga aylantiradi (albom tartibi saqlanadi)."""
    return [u for _, u in sorted(items or [], key=lambda x: x[0])]


async def catalog_name(state: FSMContext) -> str:
    return path_label((await state.get_data()).get("catalog") or {})


async def drop_msg(m: Message) -> None:
    try:
        await m.delete()
    except TelegramBadRequest:
        pass


async def show(status: Message, text: str) -> None:
    """Holat xabarini tahrirlab yangi matnga aylantiradi; bo'lmasa o'chirib, yangisini yuboradi."""
    try:
        await status.edit_text(text)
    except TelegramBadRequest:
        await drop_msg(status)
        await status.answer(text)


# ================= HANDLERS: START / HOME / CATALOG BUTTONS =================
async def go_home(msg: Message, state: FSMContext):
    """Hozirgi jarayon to'xtaydi, holat tozalanadi, boshlang'ich oyna chiqadi."""
    reset_flow(msg.from_user.id)
    await state.clear()
    await msg.answer(START_TEXT, reply_markup=kb_start())


@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await go_home(msg, state)


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await go_home(msg, state)


@dp.message(F.text == BTN_HOME)
async def btn_home(msg: Message, state: FSMContext):
    await go_home(msg, state)


async def ask_season(msg: Message, state: FSMContext):
    await state.set_state(Catalog.season)
    await msg.answer(SEASON_TEXT, reply_markup=kb_options([l for _, l in SEASONS]))


async def ask_gender(msg: Message, state: FSMContext):
    await state.set_state(Catalog.gender)
    await msg.answer(GENDER_TEXT, reply_markup=kb_options([l for _, l in GENDERS]))


async def ask_type(msg: Message, state: FSMContext, draft: dict):
    await state.set_state(Catalog.type)
    labels = [l for _, l in TYPES_BY_SG.get((draft.get("season"), draft.get("gender")), [])]
    await msg.answer(TYPE_TEXT, reply_markup=kb_options(labels))


async def ask_place(msg: Message, state: FSMContext):
    await state.set_state(Catalog.place)
    await msg.answer(PLACE_TEXT, reply_markup=kb_options([l for _, l in PLACES]))


async def get_draft(state: FSMContext) -> dict:
    return (await state.get_data()).get("catalog_draft") or {}


@dp.message(F.text.in_({BTN_PICK, BTN_CHANGE}))
async def btn_pick_catalog(msg: Message, state: FSMContext):
    """«Katalog tanlash» / «Katalogni o'zgartirish»: davom etayotgan jarayon bekor qilinadi, katalog tanlash ochiladi."""
    reset_flow(msg.from_user.id)
    await state.clear()
    await state.update_data(catalog_draft={})
    await ask_season(msg, state)


@dp.message(StateFilter(None), F.text == BTN_CONTINUE)
async def btn_continue(msg: Message, state: FSMContext):
    """«Davom etish»: katalog qayta so'ralmaydi, saqlangan katalog bilan to'g'ridan-to'g'ri 1-bosqich."""
    path = (await state.get_data()).get("catalog")
    if not path:
        return await go_home(msg, state)
    await begin_product(msg, state, path)


@dp.message(StateFilter(Catalog.season, Catalog.gender, Catalog.type, Catalog.place), F.text == BTN_BACK)
async def btn_back(msg: Message, state: FSMContext):
    cur = await state.get_state()
    draft = await get_draft(state)
    if cur == Catalog.gender.state:
        await state.update_data(catalog_draft={})
        return await ask_season(msg, state)
    if cur == Catalog.type.state:
        for k in ("gender", "type", "place"):
            draft.pop(k, None)
        await state.update_data(catalog_draft=draft)
        return await ask_gender(msg, state)
    if cur == Catalog.place.state:
        for k in ("type", "place"):
            draft.pop(k, None)
        await state.update_data(catalog_draft=draft)
        return await ask_type(msg, state, draft)
    await go_home(msg, state)   # Catalog.season


# ================= HANDLERS: CATALOG CHOICE =================
@dp.message(Catalog.season, F.text)
async def pick_season(msg: Message, state: FSMContext):
    v = {l: v for v, l in SEASONS}.get(msg.text)
    if not v:
        return await ask_season(msg, state)
    await state.update_data(catalog_draft={"season": v})
    await ask_gender(msg, state)


@dp.message(Catalog.gender, F.text)
async def pick_gender(msg: Message, state: FSMContext):
    draft = await get_draft(state)
    if "season" not in draft:
        return await ask_season(msg, state)
    v = {l: v for v, l in GENDERS}.get(msg.text)
    if not v:
        return await ask_gender(msg, state)
    draft["gender"] = v
    await state.update_data(catalog_draft=draft)
    await ask_type(msg, state, draft)


@dp.message(Catalog.type, F.text)
async def pick_type(msg: Message, state: FSMContext):
    draft = await get_draft(state)
    if "season" not in draft or "gender" not in draft:
        return await ask_season(msg, state)
    v = {l: v for v, l in TYPES_BY_SG.get((draft["season"], draft["gender"]), [])}.get(msg.text)
    if not v:
        return await ask_type(msg, state, draft)
    draft["type"] = v
    draft.pop("place", None)
    await state.update_data(catalog_draft=draft)
    if v == "shippak":
        await ask_place(msg, state)
    else:
        await begin_product(msg, state, draft)   # katalog tayyor: to'g'ridan-to'g'ri 1-bosqich


@dp.message(Catalog.place, F.text)
async def pick_place(msg: Message, state: FSMContext):
    draft = await get_draft(state)
    if "type" not in draft:
        return await ask_season(msg, state)
    v = {l: v for v, l in PLACES}.get(msg.text)
    if not v:
        return await ask_place(msg, state)
    draft["place"] = v
    await state.update_data(catalog_draft=draft)
    await begin_product(msg, state, draft)


# ================= HANDLERS: ADD PRODUCT (rasm bosqichlari) =================
async def begin_product(msg: Message, state: FSMContext, draft: dict):
    """Katalog tayyor: u state'ga saqlanadi va 1-bosqich xabari yuboriladi."""
    path = {k: draft.get(k) for k in ("season", "gender", "type", "place")}
    reset_flow(msg.from_user.id)
    await state.set_state(ProductAdd.main_images)
    await state.update_data(catalog=path, catalog_draft=draft, main_images=[], comment_images=[], variant_image=None)
    await msg.answer(stage_text(None, path_label(path), 1, PROMPT_MAIN), reply_markup=kb_process())


async def go_comment_stage(chat_id: int, state: FSMContext) -> None:
    """1 → 2: bitta umumiy xabar."""
    if await state.get_state() != ProductAdd.main_images.state:
        return
    if not (await state.get_data()).get("main_images"):
        return
    await bot.send_message(chat_id, stage_text(ACCEPTED, await catalog_name(state), 2, PROMPT_COMMENT))
    await state.set_state(ProductAdd.comment_images)


async def go_variant_stage(chat_id: int, state: FSMContext) -> None:
    """2 → 3: bitta umumiy xabar."""
    if await state.get_state() != ProductAdd.comment_images.state:
        return
    if not (await state.get_data()).get("comment_images"):
        return
    await bot.send_message(chat_id, stage_text(ACCEPTED, await catalog_name(state), 3, PROMPT_VARIANT))
    await state.set_state(ProductAdd.variant_image)


async def go_cover_stage(chat_id: int, state: FSMContext) -> None:
    """3 → 4: bitta umumiy xabar (AI orqa fonda ishlayveradi)."""
    if await state.get_state() != ProductAdd.variant_image.state:
        return
    if not (await state.get_data()).get("variant_image"):
        return
    await bot.send_message(chat_id, stage_text(ACCEPTED, await catalog_name(state), 4, PROMPT_COVER))
    await state.set_state(ProductAdd.main_cover)


def upload_error_text(e: Exception, accepted: int) -> str:
    return (
        f"⚠️ Xato: {err(e)}\n"
        f"Qabul qilingan rasmlar: {accepted} ta.\n"
        f"Qayta urinib ko'rish uchun «{BTN_CHANGE}» tugmasini yoki /start ni bosing."
    )


async def _collect_photo(msg: Message, state: FSMContext, expected: State, key: str,
                         stage_key: str, go_next) -> None:
    """Albom/yakka rasmni jimgina saqlaydi; oxirgi rasmdan 2 soniya o'tgach bitta umumiy xabar bilan keyingi bosqichga o'tadi."""
    uid = msg.from_user.id
    try:
        url = await save_photo(msg)
    except Exception as e:
        have = len((await state.get_data()).get(key) or [])
        return await msg.answer(upload_error_text(e, have))
    async with get_lock(uid):
        if await state.get_state() != expected.state:
            return   # yuklash paytida jarayon bekor qilingan
        items = (await state.get_data()).get(key) or []
        items.append([msg.message_id, url])
        await state.update_data(**{key: items})
    ensure_advance(uid, stage_key, partial(go_next, msg.chat.id, state))


@dp.message(ProductAdd.main_images, F.photo)
async def msg_main_images(msg: Message, state: FSMContext):
    await _collect_photo(msg, state, ProductAdd.main_images, "main_images", "main", go_comment_stage)


@dp.message(ProductAdd.comment_images, F.photo)
async def msg_comment_images(msg: Message, state: FSMContext):
    await _collect_photo(msg, state, ProductAdd.comment_images, "comment_images", "comment", go_variant_stage)


@dp.message(ProductAdd.variant_image, F.photo)
async def msg_variant_image(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid in busy:
        return   # albomdagi ortiqcha rasm
    busy.add(uid)
    try:
        if (await state.get_data()).get("variant_image"):
            return   # variant allaqachon qabul qilingan
        try:
            url = await save_photo(msg)
        except Exception as e:
            return await msg.answer(upload_error_text(e, 0))
        if await state.get_state() != ProductAdd.variant_image.state:
            return   # yuklash paytida jarayon bekor qilingan
        await state.update_data(variant_image=url)
        drop_ai_task(uid)
        ai_tasks[uid] = asyncio.create_task(analyze_variant(url))   # AI orqa fonda ishlaydi
    finally:
        busy.discard(uid)
    ensure_advance(uid, "variant", partial(go_cover_stage, msg.chat.id, state))


@dp.message(ProductAdd.main_cover, F.photo)
async def msg_main_cover(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if msg.media_group_id:
        ignored_groups[uid] = msg.media_group_id
    if uid in busy:
        return   # mahsulot yaratilmoqda / albomdagi ortiqcha rasm
    busy.add(uid)
    try:
        await _create_product(msg, state)
    finally:
        busy.discard(uid)


async def _back_to_variant(status: Message, state: FSMContext, reason: str) -> None:
    """AI yoki baza xatosi: 3-bosqichga qaytariladi (yangi variant rasm so'raladi). Jarayon bekor qilingan bo'lsa — jim."""
    if await state.get_state() != ProductAdd.main_cover.state:
        return await drop_msg(status)
    await state.update_data(variant_image=None)
    await state.set_state(ProductAdd.variant_image)
    await show(status, stage_text(reason, await catalog_name(state), 3, "Variantlar rasmini qayta yuboring"))


async def _create_product(msg: Message, state: FSMContext) -> None:
    uid = msg.from_user.id
    try:
        cover_url = await save_photo(msg)
    except Exception as e:
        data = await state.get_data()
        accepted = len(data.get("main_images") or [])
        await msg.answer(upload_error_text(e, accepted))
        return

    status = await msg.answer("Mahsulot yaratilmoqda...")   # keyin o'chiriladi / xato matniga almashadi

    ai_task = ai_tasks.pop(uid, None)
    if not ai_task:
        return await _back_to_variant(status, state, "❌ AI natijasi topilmadi.")
    try:
        ai_result = await ai_task
    except asyncio.CancelledError:
        if not ai_task.cancelled():
            raise
        return await drop_msg(status)   # jarayon bekor qilingan, AI to'xtatilgan
    except Exception as e:
        return await _back_to_variant(status, state, f"❌ AI xatosi: {err(e)}")

    data = await state.get_data()
    path = data.get("catalog")
    if not path or await state.get_state() != ProductAdd.main_cover.state:
        return await drop_msg(status)   # foydalanuvchi jarayonni bekor qilgan
    images = [cover_url] + ordered_urls(data.get("main_images"))
    comment_images = ordered_urls(data.get("comment_images"))

    comment_list = [
        {
            "userId": "ID:" + str(random.randint(100000, 999999)),
            "text": c["text"],
            "textRu": c["textRu"],
            "rating": c["rating"],
            "images": comment_images if i == 0 else [],
        }
        for i, c in enumerate(ai_result["commentList"])
    ]

    row = {
        "name": "", "name_ru": "", "price": 0,
        "price_yuan": ai_result["priceYuan"],
        "catalog": quiz_catalog_id(path),
        "rating": ai_result["rating"],
        "likes": ai_result["likes"], "views": ai_result["views"],
        "sales": ai_result["sales"], "comments": ai_result["comments"],
        "images": images,
        "description": "", "description_ru": "",
        "comment_list": comment_list,
        "added_at": int(time.time() * 1000),
        "variants": {
            "colors": ai_result["colors"],
            "colorsRu": [COLOR_UZ_TO_RU[c] for c in ai_result["colors"]],
            "sizes": ai_result["sizes"],
            "catPath": path,
        },
    }
    try:
        res = await db_add_product(row)
    except Exception as e:
        return await _back_to_variant(status, state, f"❌ Baza xatosi: {err(e)}")
    if not isinstance(res, dict) or not res.get("ok"):
        detail = res.get("error") if isinstance(res, dict) else res
        return await _back_to_variant(status, state, f"❌ Xato: {html.escape(str(detail))}")

    final = (f"\n{SEP}\n").join([
        "Mahsulot qo'shildi",
        f"Katalog: {html.escape(path_label(path))}",
        f"Narx: {ai_result['priceYuan']} ¥",
        f"Rang: {html.escape(', '.join(ai_result['colors']))}",
        f"O'lcham: {', '.join(map(str, ai_result['sizes']))}",
        f"ID: {res.get('product_id')}",
    ])
    await drop_msg(status)   # «Mahsulot yaratilmoqda...» o'chiriladi: faqat natija qoladi
    if await state.get_state() == ProductAdd.main_cover.state:
        # katalog saqlanadi («Davom etish» uchun), faqat shu mahsulot ma'lumotlari tozalanadi
        await state.set_state(None)
        await state.update_data(main_images=[], comment_images=[], variant_image=None)
        await msg.answer(final, reply_markup=kb_after_success())
    else:
        await msg.answer(final)   # mahsulot bazaga qo'shildi, lekin foydalanuvchi allaqachon boshqa oynada


# ================= FALLBACKS (har doim eng oxirida) =================
@dp.message(F.text, ~F.text.startswith("/"))
async def msg_fallback(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await msg.answer("📷 Iltimos, rasm yuboring (matn emas).")
    else:
        data = await state.get_data()
        if data.get("catalog"):
            await msg.answer(f"Davom etish uchun «{BTN_CONTINUE}» tugmasini bosing.")
        else:
            await msg.answer(START_TEXT, reply_markup=kb_start())


@dp.message(F.photo)
async def msg_photo_fallback(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if msg.media_group_id and ignored_groups.get(uid) == msg.media_group_id:
        return   # tugagan bosqichdagi albomning kech kelgan rasmi
    current = await state.get_state() or ""
    if current.startswith("Catalog:"):
        return await msg.answer("Avval katalogni tanlang.")
    if (await state.get_data()).get("catalog"):
        return await msg.answer(f"Davom etish uchun «{BTN_CONTINUE}» tugmasini bosing.")
    await msg.answer(f"Avval «{BTN_PICK}» tugmasini bosing.", reply_markup=kb_start())


@dp.callback_query()
async def cb_stale(cb: CallbackQuery):
    """Eski xabarlardagi inline tugmalar: bot endi faqat pastki klaviaturadan foydalanadi."""
    await cb.answer("Bu tugma eskirgan. /start ni bosing.", show_alert=True)


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
    required = {
        "BOT_TOKEN": BOT_TOKEN, "ADMIN_IDS": ADMIN_IDS, "BOT_SECRET": BOT_SECRET,
        "account_1_url": SUPABASE_URL, "account_1_key": SUPABASE_KEY,
        "account_2_url": STORAGE_URL, "account_2_key": STORAGE_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(f"Quyidagi o'zgaruvchilar sozlanmagan: {', '.join(missing)}")
    if not PROVIDERS:
        raise SystemExit("Kamida bitta AI kaliti kerak: GEMINI_API_KEY, GROQ_API_KEY yoki MISTRAL_API_KEY")


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
