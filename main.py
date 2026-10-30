import os
import re
import json
import html
import base64
import asyncio
import logging
import random
import time

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from openai import AsyncOpenAI
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
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
BOT_SECRET = os.getenv("BOT_SECRET", "").strip()
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "").strip()
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


bot = Bot(BOT_TOKEN or "0:invalid", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
ai_tasks: dict[int, asyncio.Task] = {}


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


# ================= IMGBB & SUPABASE =================
async def upload_image(data: bytes) -> str:
    b64 = base64.b64encode(data).decode()
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": b64},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            body = await r.json(content_type=None)
            if not body.get("success"):
                raise RuntimeError(f"imgbb xatosi (HTTP {r.status})")
            return body["data"]["url"]


async def db_add_product(row: dict) -> dict:
    headers = {"apikey": SUPABASE_KEY}
    # Eski (JWT) kalitlar uchun Authorization ham kerak; yangi sb_... kalitlarda kerak emas
    if SUPABASE_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{SUPABASE_URL}/rest/v1/rpc/app_bot_add_product",
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


async def edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ================= KEYBOARDS =================
def kb_catalog_season():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=lbl, callback_data=f"cat:season:{v}")] for v, lbl in SEASONS] + [[InlineKeyboardButton(text="❌ Bekor", callback_data="cat:cancel")]])


def kb_catalog_gender(s):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=lbl, callback_data=f"cat:gender:{v}")] for v, lbl in GENDERS] + [[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cat:back:season"), InlineKeyboardButton(text="❌ Bekor", callback_data="cat:cancel")]])


def kb_catalog_type(s, g):
    rows = [[InlineKeyboardButton(text=lbl, callback_data=f"cat:type:{v}")] for v, lbl in TYPES_BY_SG.get((s, g), [])]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cat:back:gender"), InlineKeyboardButton(text="❌ Bekor", callback_data="cat:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_catalog_place(s, g):
    rows = [[InlineKeyboardButton(text=lbl, callback_data=f"cat:place:{v}")] for v, lbl in PLACES]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cat:back:type"), InlineKeyboardButton(text="❌ Bekor", callback_data="cat:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_catalog_confirm():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Boshlash", callback_data="cat:done")], [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="cat:back:last"), InlineKeyboardButton(text="❌ Bekor", callback_data="cat:cancel")]])


def kb_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Mahsulot qo'shish", callback_data="add:new")], [InlineKeyboardButton(text="📚 Katalog almashtirish", callback_data="cat:start")]])


def kb_next_step(next_cb, back_cb=None):
    nav = [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_cb)] if back_cb else []
    nav.append(InlineKeyboardButton(text="❌ Bekor", callback_data="add:cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Keyingi", callback_data=next_cb)], nav])


def kb_step_nav(back_cb):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_cb), InlineKeyboardButton(text="❌ Bekor", callback_data="add:cancel")]])


def kb_after_success():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Yana qo'shish", callback_data="add:new")], [InlineKeyboardButton(text="📚 Katalog", callback_data="cat:start")], [InlineKeyboardButton(text="🏠 Menyu", callback_data="menu")]])


def kb_retry_variant():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Rasmni qayta yuborish", callback_data="retry:variant")], [InlineKeyboardButton(text="❌ Bekor", callback_data="add:cancel")]])


# ================= HANDLERS: COMMANDS =================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("👋 Salom, admin!\n\nAvval katalog tanlaymiz.", reply_markup=kb_catalog_season())


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    old = ai_tasks.pop(msg.from_user.id, None)
    if old and not old.done():
        old.cancel()
    data = await state.get_data()
    catalog = data.get("catalog")
    await state.clear()
    if catalog:  # tanlangan katalog saqlanib qoladi
        await state.update_data(catalog=catalog)
    await msg.answer("❌ Bekor qilindi.", reply_markup=kb_main_menu())


# ================= HANDLERS: CATALOG =================
@dp.callback_query(F.data == "cat:start")
async def cb_cat_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(catalog=None, catalog_draft={})
    await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())


@dp.callback_query(F.data == "cat:cancel")
async def cb_cat_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await edit(cb, "❌ Katalog tanlash bekor qilindi.")
    await cb.message.answer("🏠 Menyu:", reply_markup=kb_catalog_season())


@dp.callback_query(F.data.startswith("cat:season:"))
async def cb_cat_season(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    season = cb.data.split(":")[2]
    await state.update_data(catalog_draft={"season": season})
    await edit(cb, "2️⃣ Kim uchun?", kb_catalog_gender(season))


@dp.callback_query(F.data.startswith("cat:gender:"))
async def cb_cat_gender(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    gender = cb.data.split(":")[2]
    draft = (await state.get_data()).get("catalog_draft") or {}
    if "season" not in draft:
        return await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())
    draft["gender"] = gender
    await state.update_data(catalog_draft=draft)
    await edit(cb, "3️⃣ Qanday poyabzal?", kb_catalog_type(draft["season"], gender))


@dp.callback_query(F.data.startswith("cat:type:"))
async def cb_cat_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    ptype = cb.data.split(":")[2]
    draft = (await state.get_data()).get("catalog_draft") or {}
    if "season" not in draft or "gender" not in draft:
        return await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())
    draft["type"] = ptype
    draft.pop("place", None)
    await state.update_data(catalog_draft=draft)
    if ptype == "shippak":
        await edit(cb, "4️⃣ Qayerda kiyiladi?", kb_catalog_place(draft["season"], draft["gender"]))
    else:
        await _finalize_catalog(cb, state, draft)


@dp.callback_query(F.data.startswith("cat:place:"))
async def cb_cat_place(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    place = cb.data.split(":")[2]
    draft = (await state.get_data()).get("catalog_draft") or {}
    if "type" not in draft:
        return await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())
    draft["place"] = place
    await state.update_data(catalog_draft=draft)
    await _finalize_catalog(cb, state, draft)


async def _finalize_catalog(cb: CallbackQuery, state: FSMContext, draft: dict):
    path = {k: draft.get(k) for k in ("season", "gender", "type", "place")}
    # draft saqlanadi, shunda "Orqaga" tugmasi ishlaydi
    await state.update_data(catalog=path, catalog_draft=draft)
    await edit(cb, f"✅ Katalog: <b>{path_label(path)}</b>\n\nMahsulot qo'shamizmi?", kb_catalog_confirm())


@dp.callback_query(F.data.startswith("cat:back:"))
async def cb_cat_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    target = cb.data.split(":")[2]
    draft = (await state.get_data()).get("catalog_draft") or {}
    if target == "season":
        draft = {}
        await state.update_data(catalog_draft=draft)
        await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())
    elif target == "gender":
        for k in ("gender", "type", "place"):
            draft.pop(k, None)
        await state.update_data(catalog_draft=draft)
        await edit(cb, "2️⃣ Kim uchun?", kb_catalog_gender(draft.get("season")))
    elif target == "type":
        for k in ("type", "place"):
            draft.pop(k, None)
        await state.update_data(catalog_draft=draft)
        await edit(cb, "3️⃣ Qanday poyabzal?", kb_catalog_type(draft.get("season"), draft.get("gender")))
    elif target == "last":
        if draft.get("type") == "shippak":
            await edit(cb, "4️⃣ Qayerda kiyiladi?", kb_catalog_place(draft.get("season"), draft.get("gender")))
        else:
            await edit(cb, "3️⃣ Qanday poyabzal?", kb_catalog_type(draft.get("season"), draft.get("gender")))


@dp.callback_query(F.data == "cat:done")
async def cb_cat_done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    path = (await state.get_data()).get("catalog")
    if not path:
        return await edit(cb, "1️⃣ Faslni tanlang:", kb_catalog_season())
    await edit(cb, f"✅ Katalog: <b>{path_label(path)}</b>", kb_main_menu())


@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await edit(cb, "🏠 Menyu:", kb_main_menu())


# ================= HANDLERS: ADD PRODUCT =================
@dp.callback_query(F.data == "add:new")
async def cb_add_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    path = (await state.get_data()).get("catalog")
    if not path:
        return await edit(cb, "Avval katalog tanlang:", kb_catalog_season())
    old = ai_tasks.pop(cb.from_user.id, None)
    if old and not old.done():
        old.cancel()
    await state.update_data(main_images=[], comment_images=[], variant_image=None)
    await state.set_state(ProductAdd.main_images)
    await edit(
        cb,
        f"📚 Katalog: <b>{path_label(path)}</b>\n\n📷 <b>1-bosqich: Asosiy rasmlar</b>\nYuboring va <b>➡️ Keyingi</b> ni bosing.",
        kb_next_step("next:comments"),
    )


@dp.callback_query(F.data == "add:cancel")
async def cb_add_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    old = ai_tasks.pop(cb.from_user.id, None)
    if old and not old.done():
        old.cancel()
    catalog = (await state.get_data()).get("catalog")
    await state.clear()
    if catalog:
        await state.update_data(catalog=catalog)
    await edit(cb, "❌ Bekor qilindi.")
    await cb.message.answer("🏠 Menyu:", reply_markup=kb_main_menu())


@dp.message(ProductAdd.main_images, F.photo)
async def msg_main_images(msg: Message, state: FSMContext):
    try:
        url = await save_photo(msg)
    except Exception as e:
        return await msg.answer(f"⚠️ Xato: {err(e)}")
    imgs = (await state.get_data()).get("main_images") or []
    imgs.append(url)
    await state.update_data(main_images=imgs)
    await msg.answer(f"✅ Rasm #{len(imgs)} qabul qilindi.")


@dp.callback_query(F.data == "next:comments")
async def cb_next_comments(cb: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("main_images"):
        return await cb.answer("Kamida 1 ta rasm yuboring!", show_alert=True)
    await cb.answer()
    await state.set_state(ProductAdd.comment_images)
    await edit(cb, "📷 <b>2-bosqich: Komentariya rasmlari</b>\nYuboring va <b>➡️ Keyingi</b> ni bosing.", kb_next_step("next:variant", "back:main"))


@dp.callback_query(F.data == "back:main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(ProductAdd.main_images)
    await edit(cb, "📷 <b>1-bosqich: Asosiy rasmlar</b>", kb_next_step("next:comments"))


@dp.message(ProductAdd.comment_images, F.photo)
async def msg_comment_images(msg: Message, state: FSMContext):
    try:
        url = await save_photo(msg)
    except Exception as e:
        return await msg.answer(f"⚠️ Xato: {err(e)}")
    imgs = (await state.get_data()).get("comment_images") or []
    imgs.append(url)
    await state.update_data(comment_images=imgs)
    await msg.answer(f"✅ Komment rasm #{len(imgs)} qabul qilindi.")


@dp.callback_query(F.data == "back:comments")
async def cb_back_comments(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(ProductAdd.comment_images)
    await edit(cb, "📷 <b>2-bosqich: Komentariya rasmlari</b>", kb_next_step("next:variant", "back:main"))


@dp.callback_query(F.data == "next:variant")
async def cb_next_variant(cb: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("comment_images"):
        return await cb.answer("Kamida 1 ta rasm yuboring!", show_alert=True)
    await cb.answer()
    await state.set_state(ProductAdd.variant_image)
    await edit(cb, "🎨 <b>3-bosqich: Variant rasmi</b>\nRang, o'lcham, narx bor rasmni yuboring. AI tahlil qiladi.", kb_step_nav("back:comments"))


@dp.message(ProductAdd.variant_image, F.photo)
async def msg_variant_image(msg: Message, state: FSMContext):
    try:
        url = await save_photo(msg)
    except Exception as e:
        return await msg.answer(f"⚠️ Xato: {err(e)}")
    await state.update_data(variant_image=url)
    old = ai_tasks.pop(msg.from_user.id, None)
    if old and not old.done():
        old.cancel()
    ai_tasks[msg.from_user.id] = asyncio.create_task(analyze_variant(url))
    await state.set_state(ProductAdd.main_cover)
    await msg.answer(
        "✅ Variant rasm qabul qilindi. AI ishlamoqda...\n\n📷 <b>4-bosqich: Main rasm</b>\nAsosiy muqova rasmini yuboring.",
        reply_markup=kb_step_nav("back:variant"),
    )


@dp.callback_query(F.data == "back:variant")
async def cb_back_variant(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    old = ai_tasks.pop(cb.from_user.id, None)
    if old and not old.done():
        old.cancel()
    await state.update_data(variant_image=None)
    await state.set_state(ProductAdd.variant_image)
    await edit(cb, "🎨 <b>3-bosqich: Variant rasmi</b>", kb_step_nav("back:comments"))


@dp.callback_query(F.data == "retry:variant")
async def cb_retry_variant(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(variant_image=None)
    await state.set_state(ProductAdd.variant_image)
    await edit(cb, "🎨 <b>3-bosqich: Variant rasmi</b>\nYangi rasm yuboring.", kb_step_nav("back:comments"))


@dp.message(ProductAdd.main_cover, F.photo)
async def msg_main_cover(msg: Message, state: FSMContext):
    try:
        cover_url = await save_photo(msg)
    except Exception as e:
        return await msg.answer(f"⚠️ Xato: {err(e)}")
    await msg.answer("⏳ Mahsulot yaratilmoqda...")

    ai_task = ai_tasks.pop(msg.from_user.id, None)
    if not ai_task:
        return await msg.answer("❌ AI natijasi topilmadi.", reply_markup=kb_retry_variant())
    try:
        ai_result = await ai_task
    except Exception as e:
        return await msg.answer(f"❌ AI xatosi: {err(e)}", reply_markup=kb_retry_variant())

    data = await state.get_data()
    path = data.get("catalog")
    if not path:
        return await msg.answer("❌ Katalog tanlanmagan.", reply_markup=kb_catalog_season())
    images = [cover_url] + (data.get("main_images") or [])
    comment_images = data.get("comment_images") or []

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
        return await msg.answer(f"❌ Baza xatosi: {err(e)}")
    if not isinstance(res, dict) or not res.get("ok"):
        detail = res.get("error") if isinstance(res, dict) else res
        return await msg.answer(f"❌ Xato: {html.escape(str(detail))}")

    await state.set_state(None)
    await msg.answer(
        f"✅ <b>Mahsulot qo'shildi!</b>\n\n"
        f"🆔 ID: <code>{res.get('product_id')}</code>\n"
        f"📚 {path_label(path)}\n"
        f"💰 {ai_result['priceYuan']} ¥\n"
        f"🎨 {', '.join(ai_result['colors'])}\n"
        f"📏 {', '.join(map(str, ai_result['sizes']))}",
        reply_markup=kb_after_success(),
    )


@dp.message(F.text, ~F.text.startswith("/"))
async def msg_fallback(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await msg.answer("📷 Iltimos, rasm yuboring (matn emas).")
    else:
        await msg.answer("🏠 Menyu:", reply_markup=kb_main_menu())


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
        "BOT_TOKEN": BOT_TOKEN, "ADMIN_IDS": ADMIN_IDS, "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY, "BOT_SECRET": BOT_SECRET, "IMGBB_API_KEY": IMGBB_API_KEY,
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
