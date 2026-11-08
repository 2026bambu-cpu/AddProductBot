"""
Poyabzal Telegram Bot
=====================
Admin uchun mahsulot qo'shuvchi bot: rasmlar -> AI (Gemini -> Groq -> Mistral) -> Supabase.

Tuzilma:
    1. CONFIG & TUNABLES      - sozlamalar va vaqt/limit konstantalari
    2. CONSTANTS              - ranglar, katalog (quiz) ma'lumotlari, AI prompt
    3. RUNTIME STATE          - foydalanuvchi bo'yicha vaqtinchalik lug'atlar + avtomatik tozalash
    4. RETRY / BACKOFF        - umumiy qayta urinish (retry with backoff) mexanizmi
    5. TELEGRAM HELPERS       - FloodWait-xavfsiz yuborish/tahrirlash/o'chirish
    6. AI                     - provayderlar zanjiri (fallback) + validatsiya
    7. SUPABASE               - Storage (rasm) va DB (RPC)
    8. TEXTS & KEYBOARDS      - barcha matnlar va tugmalar
    9. HANDLERS               - quiz, bosqichlar, retry, fallback'lar
   10. STARTUP                - health-server, janitor, polling
"""
import asyncio
import contextlib
import html
import json
import logging
import math
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from urllib.parse import unquote

import aiohttp
import openai
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

# 1K dan 45K gacha, 0.1K qadam bilan ("1K", "1.1K", ... "3.9K", "4K", ... "45K").
STATS_POOL = [
    f"{t // 10}K" if t % 10 == 0 else f"{t // 10}.{t % 10}K"
    for t in range(10, 451)
]

# =============================================================================
# 1. CONFIG & TUNABLES
# =============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SUPABASE_URL = os.getenv("account_1_url", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("account_1_key", "").strip()
BOT_SECRET = os.getenv("BOT_SECRET", "").strip()
STORAGE_URL = os.getenv("account_2_url", "").strip().rstrip("/")
STORAGE_KEY = os.getenv("account_2_key", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()

GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct").strip()
MISTRAL_MODEL = (os.getenv("MISTRAL_MODEL") or "pixtral-12b-latest").strip()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poyabzal-bot")

# --- Debounce ---
BULK_PAUSE = 5.0                 # oxirgi rasmdan keyin shuncha kutib, albom tahlili boshlanadi

# --- Retry / timeout ---
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
AI_ATTEMPTS = 3                  # har bir AI provayderga urinishlar soni
AI_REQUEST_TIMEOUT = 60.0        # bitta AI so'rovi uchun (soniya)
AI_MAX_RETRY_WAIT = 12.0         # 429 da server "kut" desa va bundan uzoq bo'lsa -> keyingi provayderga o'tamiz
NET_ATTEMPTS = 3                 # Supabase (storage/DB) uchun urinishlar soni
NET_MAX_RETRY_WAIT = 15.0
UPLOAD_CONCURRENCY = 4           # bir vaqtda nechta rasm yuklanishi mumkin
TG_ATTEMPTS = 4                  # Telegram so'rovlari uchun urinishlar
TG_MAX_RETRY_WAIT = 30.0         # FloodWait shundan uzoq bo'lsa kutib o'tirmaymiz

# --- O'chirilgan mahsulotlar rasmini tozalash (Storage "products" bucket) ---
ORPHAN_SWEEP_ENABLED = os.getenv("ORPHAN_SWEEP", "1").strip() != "0"   # ORPHAN_SWEEP=0 -> o'chirib qo'yiladi
ORPHAN_SWEEP_FIRST_DELAY = 120.0  # ishga tushgandan keyin birinchi tozalashgacha
ORPHAN_SWEEP_INTERVAL = 6 * 3600  # keyingi tozalashlar oralig'i
ORPHAN_MIN_AGE = 24 * 3600        # bundan yangi fayllarga tegilmaydi (hali bazaga yozilmagan bo'lishi mumkin)
PRODUCTS_URL_MARK = "/storage/v1/object/public/products/"

# --- Flood control ---
CHAT_MIN_INTERVAL = 0.5          # bitta chatga ketma-ket xabarlar orasidagi minimal masofa (soniya)
NOTIFY_COOLDOWN = 3.0            # bir xil ogohlantirishni qayta yuborish oralig'i (soniya)
DENY_COOLDOWN = 30.0             # ruxsatsiz foydalanuvchiga javob oralig'i

# --- Cleanup (memory leak oldini olish) ---
CLEANUP_INTERVAL = 60.0
LOCK_TTL = 15 * 60               # ishlatilmagan lock shuncha vaqtdan keyin o'chadi
LAST_PHOTO_TTL = 5 * 60
GROUP_TTL = 10 * 60
MISC_TTL = 5 * 60

# =============================================================================
# 2. CONSTANTS
# =============================================================================
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


AI_SYSTEM_PROMPT = """You are an expert, strict data extractor for an Uzbek shoe marketplace.
TASK: You receive ONE image from a product album (usually a Chinese seller's listing). Decide whether it is a VARIANT/PRICE image and extract what it shows. Return ONLY a JSON object matching the schema below.
SCHEMA:
{
  "is_variant_image": boolean,
  "colors":    [string],
  "sizes":     [int],
  "priceYuan": number | null
}
RULES:
0. is_variant_image = true ONLY if the image shows selectable product options or the price: color swatches/color names, size selectors (shoe size numbers), or the sale price in yuan (¥ / 元).
   Product photos, model/lifestyle photos, measurement charts (cm), customer reviews, chat screenshots and any other images are NOT variant images -> return {"is_variant_image": false, "colors": [], "sizes": [], "priceYuan": null}.
   Colors, sizes and the price may be split across several images, so extract ONLY what is visible in THIS image: use [] / null for anything not shown. Never guess.
1. COLORS: ALLOWED_COLORS: {allowed_colors}.
   - ONLY include colors that are actively selectable in the image.
   - Ignore unavailable colors (they usually appear grayed out, faded, transparent, or crossed out).
   - Map colors accurately to the closest allowed color (e.g., if you see "rose" or "light red", map it to "Qizil" or "Och qizil"). NEVER mix up colors or invent new ones.
2. SIZES: Extract only INTEGER EU sizes from 25 to 50. Ignore crossed-out or unavailable sizes. Deduplicate, sort ascending.
3. PRICE (priceYuan): Must be > 0, otherwise null. If the image shows several prices (different variants), ALWAYS pick the HIGHEST price. Ignore crossed-out old prices.
"""
AI_USER_PROMPT = "Analyze this image per the schema. Return JSON only."
AI_SYSTEM_PROMPT_RENDERED = AI_SYSTEM_PROMPT.replace("{allowed_colors}", ", ".join(c["uz"] for c in COLORS))


# =============================================================================
# 3. RUNTIME STATE (+ avtomatik tozalash)
# =============================================================================
class ProductAdd(StatesGroup):
    all_images = State()   # 1-bosqich: barcha rasmlar bitta albomda
    main_cover = State()   # 2-bosqich: muqova rasmi


bot = Bot(BOT_TOKEN or "0:invalid", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# user_id bo'yicha vaqtinchalik holat. Hammasi janitor (cleanup_once) tomonidan tozalanadi.
advance_tasks: dict[int, tuple[str, asyncio.Task]] = {}
last_photo_at: dict[int, float] = {}
user_locks: dict[int, asyncio.Lock] = {}
lock_used_at: dict[int, float] = {}
ignored_groups: dict[int, tuple[str, float]] = {}
busy: set[int] = set()                                   # har doim finally'da tozalanadi
chat_next_slot: dict[int, float] = {}                    # chat bo'yicha keyingi bo'sh "yuborish vaqti"
notified_at: dict[tuple[int, str], float] = {}           # ogohlantirishlar cooldown'i

UPLOAD_SEMAPHORE = asyncio.Semaphore(UPLOAD_CONCURRENCY)


def _consume_result(task: asyncio.Task) -> None:
    """Hech kim kutmagan task xatosi "never retrieved" ogohlantirishini bermasligi uchun."""
    if not task.cancelled() and task.exception() is not None:
        log.debug("Fon vazifasi xato bilan tugadi: %r", task.exception())


def drop_advance_task(user_id: int) -> None:
    item = advance_tasks.pop(user_id, None)
    if item and not item[1].done():
        item[1].cancel()
    last_photo_at.pop(user_id, None)


def reset_flow(user_id: int) -> None:
    drop_advance_task(user_id)


def get_lock(user_id: int) -> asyncio.Lock:
    lock_used_at[user_id] = time.monotonic()
    lock = user_locks.get(user_id)
    if lock is None:
        lock = user_locks[user_id] = asyncio.Lock()
    return lock


def should_notify(user_id: int, key: str, cooldown: float = NOTIFY_COOLDOWN) -> bool:
    """Bir xil ogohlantirish (masalan, albomdagi har bir rasm uchun xato) spam bo'lib ketmasligi uchun."""
    now = time.monotonic()
    k = (user_id, key)
    if now - notified_at.get(k, float("-inf")) < cooldown:
        return False
    notified_at[k] = now
    return True


def cleanup_once() -> None:
    """Muddati o'tgan yoki yetim qolgan yozuvlarni o'chiradi."""
    now = time.monotonic()

    for uid, (_, task) in list(advance_tasks.items()):
        if task.done():
            advance_tasks.pop(uid, None)

    for uid, ts in list(last_photo_at.items()):
        if uid not in advance_tasks and now - ts > LAST_PHOTO_TTL:
            last_photo_at.pop(uid, None)

    for uid, ts in list(lock_used_at.items()):
        lock = user_locks.get(uid)
        if (lock is None or not lock.locked()) and now - ts > LOCK_TTL:
            lock_used_at.pop(uid, None)
            user_locks.pop(uid, None)

    for uid, (_, ts) in list(ignored_groups.items()):
        if now - ts > GROUP_TTL:
            ignored_groups.pop(uid, None)

    for chat_id, slot in list(chat_next_slot.items()):
        if now - slot > MISC_TTL:
            chat_next_slot.pop(chat_id, None)

    for key, ts in list(notified_at.items()):
        if now - ts > MISC_TTL:
            notified_at.pop(key, None)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_once()
        except Exception:
            log.exception("Janitor xatosi")


def shutdown_user_tasks() -> None:
    for uid in list(advance_tasks):
        drop_advance_task(uid)


# =============================================================================
# 4. RETRY / BACKOFF
# =============================================================================
def backoff_delay(attempt: int, base: float = 1.0, cap: float = 20.0) -> float:
    """Eksponensial kutish + jitter: ~1s, ~2s, ~4s ..."""
    return min(cap, base * 2 ** (attempt - 1)) * random.uniform(0.7, 1.3)


def parse_retry_after(value) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


async def retry(op, *, attempts: int, label: str, retry_wait):
    """
    op()                 -> coroutine
    retry_wait(e, n)     -> necha soniya kutib qayta urinish (None bo'lsa - qayta urinilmaydi)
    CancelledError (BaseException) bu yerda ushlanmaydi - bekor qilish to'g'ri ishlaydi.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except Exception as e:
            delay = retry_wait(e, attempt)
            if delay is None or attempt >= attempts:
                raise
            log.warning("[%s] %d/%d urinish muvaffaqiyatsiz (%s). %.1fs dan keyin qayta uriniladi.",
                        label, attempt, attempts, describe_error(e), delay)
            await asyncio.sleep(delay)


def describe_error(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "kutish vaqti tugadi"
    return str(e).strip() or type(e).__name__


def err(e: BaseException) -> str:
    # Avval qirqamiz, keyin escape qilamiz - HTML entity o'rtasidan kesilib qolmasligi uchun.
    return html.escape(describe_error(e)[:300])


class ServiceError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def service_retry_wait(e: Exception, attempt: int) -> float | None:
    if not isinstance(e, ServiceError) or not e.retryable:
        return None
    if e.retry_after is not None:
        return e.retry_after if e.retry_after <= NET_MAX_RETRY_WAIT else None
    return backoff_delay(attempt)


def ai_retry_wait(e: Exception, attempt: int) -> float | None:
    if isinstance(e, (openai.APITimeoutError, openai.APIConnectionError, asyncio.TimeoutError)):
        return backoff_delay(attempt)
    if isinstance(e, openai.APIStatusError) and e.status_code in RETRYABLE_STATUS:
        hinted = parse_retry_after(e.response.headers.get("retry-after"))
        if hinted is not None:
            # Server uzoq kutishni so'rasa (kvota tugagan) - boshqa provayderga o'tgan ma'qul.
            return hinted if hinted <= AI_MAX_RETRY_WAIT else None
        return backoff_delay(attempt)
    return None


# =============================================================================
# 5. TELEGRAM HELPERS (FloodWait-xavfsiz)
# =============================================================================
async def tg_call(func, *args, **kwargs):
    """Telegram so'rovi: FloodWait (429) va tarmoq/5xx xatolarida qayta uriniladi."""
    for attempt in range(1, TG_ATTEMPTS + 1):
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as e:
            if attempt >= TG_ATTEMPTS or e.retry_after > TG_MAX_RETRY_WAIT:
                raise
            wait = float(e.retry_after) + random.uniform(0.2, 0.8)
            log.warning("Telegram FloodWait: %.1fs kutamiz (%d/%d)", wait, attempt, TG_ATTEMPTS)
            await asyncio.sleep(wait)
        except (TelegramNetworkError, TelegramServerError) as e:
            if attempt >= TG_ATTEMPTS:
                raise
            delay = backoff_delay(attempt, base=0.8, cap=8.0)
            log.warning("Telegram tarmoq xatosi (%s). %.1fs dan keyin qayta uriniladi.", type(e).__name__, delay)
            await asyncio.sleep(delay)


async def throttle(chat_id: int) -> None:
    """Bitta chatga xabarlar orasida CHAT_MIN_INTERVAL masofa saqlaydi (await'siz slot band qilinadi - race yo'q)."""
    now = time.monotonic()
    slot = max(now, chat_next_slot.get(chat_id, 0.0))
    chat_next_slot[chat_id] = slot + CHAT_MIN_INTERVAL
    if slot > now:
        await asyncio.sleep(slot - now)


async def send(chat_id: int, text: str, **kwargs) -> Message:
    await throttle(chat_id)
    return await tg_call(bot.send_message, chat_id, text, **kwargs)


async def reply(msg: Message, text: str, **kwargs) -> Message:
    await throttle(msg.chat.id)
    return await tg_call(msg.answer, text, **kwargs)


async def reply_once(msg: Message, key: str, text: str, **kwargs) -> None:
    if should_notify(msg.from_user.id, key):
        await reply(msg, text, **kwargs)


async def drop_msg(m: Message) -> None:
    try:
        await tg_call(m.delete)
    except TelegramAPIError as e:
        log.debug("Xabarni o'chirib bo'lmadi: %s", e)


async def show(status: Message, text: str) -> None:
    try:
        await tg_call(status.edit_text, text)
    except TelegramBadRequest:
        await drop_msg(status)
        await reply(status, text)


async def save_photo(msg: Message) -> str:
    buf = await tg_call(bot.download, msg.photo[-1])
    return await upload_image(buf.read())


# =============================================================================
# 6. AI (fallback zanjiri)
# =============================================================================
@dataclass
class Provider:
    name: str
    client: AsyncOpenAI
    model: str


def _make_provider(name: str, key: str, base_url: str, model: str) -> Provider:
    # max_retries=0: qayta urinishni o'zimiz (retry + backoff) boshqaramiz.
    client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=AI_REQUEST_TIMEOUT, max_retries=0)
    return Provider(name, client, model)


PROVIDERS: list[Provider] = []
if GEMINI_API_KEY:
    PROVIDERS.append(_make_provider("Gemini", GEMINI_API_KEY, "https://generativelanguage.googleapis.com/v1beta/openai/", GEMINI_MODEL))
if GROQ_API_KEY:
    PROVIDERS.append(_make_provider("Groq", GROQ_API_KEY, "https://api.groq.com/openai/v1", GROQ_MODEL))
if MISTRAL_API_KEY:
    PROVIDERS.append(_make_provider("Mistral", MISTRAL_API_KEY, "https://api.mistral.ai/v1", MISTRAL_MODEL))


class AIError(RuntimeError):
    pass


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AI javobi JSON obyekt emas.")
    return data


def _validate(d: dict) -> dict:
    """Bitta rasm natijasi. Qisman bo'lishi mumkin (masalan, faqat narx) - to'liqlik _merge_parts'da tekshiriladi."""
    colors = list(dict.fromkeys(c for c in (d.get("colors") or []) if isinstance(c, str) and c in COLOR_UZ_SET))

    sizes = set()
    for x in d.get("sizes") or []:
        try:
            n = int(round(float(x)))
        except (TypeError, ValueError, OverflowError):
            continue
        if SIZE_MIN <= n <= SIZE_MAX:
            sizes.add(n)

    try:
        price = float(d.get("priceYuan") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if not math.isfinite(price) or price <= 0:
        price = 0.0

    return {"is_variant": d.get("is_variant_image") in (True, "true", "True"), "colors": colors, "sizes": sorted(sizes), "price": price}


def _merge_parts(parts: list[dict]) -> dict:
    """Ketma-ket variant rasmlari natijasini birlashtiradi: ranglar/o'lchamlar - birlashma, narx - eng qimmati."""
    colors = list(dict.fromkeys(c for p in parts for c in p["colors"]))
    sizes = sorted({n for p in parts for n in p["sizes"]})
    prices = [p["price"] for p in parts if p["price"] > 0]
    missing = [name for name, found in (("rang", colors), ("o'lcham", sizes), ("narx", prices)) if not found]
    if missing:
        raise ValueError(", ".join(missing))
    return {"colors": colors, "sizes": sizes, "priceYuan": max(prices)}


async def _ask_provider(provider: Provider, image_url: str) -> dict:
    resp = await asyncio.wait_for(
        provider.client.chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": AI_SYSTEM_PROMPT_RENDERED},
                {"role": "user", "content": [
                    {"type": "text", "text": AI_USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2000,
        ),
        timeout=AI_REQUEST_TIMEOUT + 5,
    )
    return _validate(_parse_json(resp.choices[0].message.content))


async def analyze_image(image_url: str) -> dict:
    """Bitta so'rovda: rasm variant/narx rasmimi (is_variant) va undagi ranglar, o'lchamlar, narx."""
    if not PROVIDERS:
        raise AIError("Hech qanday AI API kaliti sozlanmagan.")
    last_error = ""
    for provider in PROVIDERS:
        try:
            return await retry(
                partial(_ask_provider, provider, image_url),
                attempts=AI_ATTEMPTS, label=f"AI:{provider.name}", retry_wait=ai_retry_wait,
            )
        except Exception as e:
            log.warning("[AI XATO] %s ishlamadi: %s", provider.name, describe_error(e))
            last_error = describe_error(e)
    raise AIError(f"Barcha AI provayderlar ishlamadi. Oxirgi xato: {last_error}")


# =============================================================================
# 7. SUPABASE (Storage + DB)
# =============================================================================
_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    """Bitta umumiy sessiya (har so'rovga yangi ulanish ochmaymiz)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _error_text(body: str) -> str:
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or body)[:200]
    except ValueError:
        pass
    return body[:200]


async def _http_post(service: str, url: str, *, timeout: float, retry_statuses: set[int],
                     retry_on_network: bool, **kwargs) -> str:
    try:
        async with get_session().post(url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs) as r:
            status, body = r.status, await r.text()
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
    except asyncio.TimeoutError as e:
        raise ServiceError(f"{service}: javob kutish vaqti tugadi", retryable=retry_on_network) from e
    except aiohttp.ClientConnectorError as e:
        # Ulanish umuman o'rnatilmadi -> so'rov serverga yetmagan, qayta urinish xavfsiz.
        raise ServiceError(f"{service}: ulanib bo'lmadi", retryable=True) from e
    except aiohttp.ClientError as e:
        raise ServiceError(f"{service}: tarmoq xatosi ({type(e).__name__})", retryable=retry_on_network) from e
    if status >= 400:
        raise ServiceError(f"{service} xatosi (HTTP {status}): {_error_text(body)}",
                           retryable=status in retry_statuses, retry_after=retry_after)
    return body


async def upload_image(data: bytes) -> str:
    file_name = f"{uuid.uuid4().hex}.jpg"
    base_url = STORAGE_URL.replace("/rest/v1", "").rstrip("/")
    upload_url = f"{base_url}/storage/v1/object/products/{file_name}"
    headers = {
        "apikey": STORAGE_KEY,
        "Authorization": f"Bearer {STORAGE_KEY}",
        "Content-Type": "image/jpeg",
        "x-upsert": "true",   # bir xil nom bilan qayta yuklash xavfsiz (retry idempotent)
    }
    op = partial(_http_post, "Storage", upload_url, timeout=30, retry_statuses=RETRYABLE_STATUS,
                 retry_on_network=True, data=data, headers=headers)
    async with UPLOAD_SEMAPHORE:
        await retry(op, attempts=NET_ATTEMPTS, label="Storage", retry_wait=service_retry_wait)
    return f"{base_url}/storage/v1/object/public/products/{file_name}"


async def db_add_product(row: dict) -> dict:
    base_url = SUPABASE_URL.replace("/rest/v1", "").rstrip("/")
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    # DIQQAT: bu so'rov idempotent emas. Taym-autda dublikat yaratmaslik uchun faqat 429 va
    # "ulanib bo'lmadi" holatlarida avtomatik qayta uriniladi; qolganini foydalanuvchi "Qayta urinish" bilan hal qiladi.
    op = partial(_http_post, "Supabase", f"{base_url}/rest/v1/rpc/app_bot_add_product", timeout=20,
                 retry_statuses={429}, retry_on_network=False,
                 json={"p_secret": BOT_SECRET, "p_row": row}, headers=headers)
    body = await retry(op, attempts=NET_ATTEMPTS, label="Supabase", retry_wait=service_retry_wait)
    try:
        return json.loads(body)
    except ValueError:
        return {"ok": False, "error": body[:200]}


async def save_product(row: dict) -> dict:
    res = await db_add_product(row)
    if not isinstance(res, dict) or not res.get("ok"):
        detail = res.get("error") if isinstance(res, dict) else res
        raise ServiceError(f"Bazadan xato javob: {detail}")
    return res


# ---------- Storage tozalash (o'chirilgan mahsulotlar rasmi) ----------
async def _http_call(method: str, service: str, url: str, *, timeout: float, retry_statuses: set[int], **kwargs) -> str:
    """Idempotent so'rovlar (ro'yxat, o'qish, o'chirish) uchun umumiy HTTP chaqiruv."""
    try:
        async with get_session().request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs) as r:
            status, body = r.status, await r.text()
            retry_after = parse_retry_after(r.headers.get("Retry-After"))
    except asyncio.TimeoutError as e:
        raise ServiceError(f"{service}: javob kutish vaqti tugadi", retryable=True) from e
    except aiohttp.ClientError as e:
        raise ServiceError(f"{service}: tarmoq xatosi ({type(e).__name__})", retryable=True) from e
    if status >= 400:
        raise ServiceError(f"{service} xatosi (HTTP {status}): {_error_text(body)}",
                           retryable=status in retry_statuses, retry_after=retry_after)
    return body


def _storage_base() -> str:
    return STORAGE_URL.replace("/rest/v1", "").rstrip("/")


def _storage_headers() -> dict:
    return {"apikey": STORAGE_KEY, "Authorization": f"Bearer {STORAGE_KEY}"}


def _collect_image_names(obj, out: set[str]) -> None:
    """JSON ichidagi barcha \"products\" bucket havolalaridan fayl nomlarini yig'adi."""
    if isinstance(obj, str):
        i = obj.find(PRODUCTS_URL_MARK)
        if i >= 0:
            name = unquote(obj[i + len(PRODUCTS_URL_MARK):].split("?", 1)[0].split("#", 1)[0])
            if name:
                out.add(name)
    elif isinstance(obj, list):
        for x in obj:
            _collect_image_names(x, out)
    elif isinstance(obj, dict):
        for x in obj.values():
            _collect_image_names(x, out)


async def delete_storage_objects(names: list[str]) -> None:
    base = _storage_base()
    for i in range(0, len(names), 100):
        op = partial(_http_call, "DELETE", "Storage", f"{base}/storage/v1/object/products", timeout=30,
                     retry_statuses=RETRYABLE_STATUS, json={"prefixes": names[i:i + 100]}, headers=_storage_headers())
        await retry(op, attempts=NET_ATTEMPTS, label="Storage-delete", retry_wait=service_retry_wait)


async def _list_bucket_objects() -> list[dict]:
    base, page_size, offset, out = _storage_base(), 1000, 0, []
    while True:
        op = partial(_http_call, "POST", "Storage", f"{base}/storage/v1/object/list/products", timeout=30,
                     retry_statuses=RETRYABLE_STATUS, headers=_storage_headers(),
                     json={"prefix": "", "limit": page_size, "offset": offset,
                           "sortBy": {"column": "name", "order": "asc"}})
        page = json.loads(await retry(op, attempts=NET_ATTEMPTS, label="Storage-list", retry_wait=service_retry_wait))
        if not isinstance(page, list):
            raise ServiceError("Storage ro'yxati kutilmagan formatda keldi.")
        out.extend(page)
        if len(page) < page_size:
            return out
        offset += page_size


async def _referenced_image_names() -> set[str]:
    """Bazadagi barcha mahsulotlar (asosiy + komentariya rasmlari) ishlatayotgan fayl nomlari."""
    base = SUPABASE_URL.replace("/rest/v1", "").rstrip("/")
    headers = {"apikey": SUPABASE_KEY}
    if SUPABASE_KEY.startswith("eyJ"):       # eski (JWT) kalit; yangi "sb_..." kalitda faqat apikey kerak
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    names: set[str] = set()
    page_size, offset = 500, 0
    while True:
        url = f"{base}/rest/v1/products?select=images,comment_list&order=id&limit={page_size}&offset={offset}"
        op = partial(_http_call, "GET", "Supabase", url, timeout=30, retry_statuses=RETRYABLE_STATUS, headers=headers)
        rows = json.loads(await retry(op, attempts=NET_ATTEMPTS, label="Supabase-products", retry_wait=service_retry_wait))
        if not isinstance(rows, list):
            raise ServiceError("Mahsulotlar ro'yxati kutilmagan formatda keldi.")
        _collect_image_names(rows, names)
        if len(rows) < page_size:
            return names
        offset += page_size


def _parse_ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def sweep_orphan_images() -> int:
    """Bazadagi hech bir mahsulot ishlatmayotgan (masalan, admin panelidan o'chirilgan) rasmlarni Storage'dan o'chiradi."""
    referenced = await _referenced_image_names()     # xato bo'lsa - hech narsa o'chirilmaydi
    if not referenced:
        log.info("Rasm tozalash: bazada rasm havolalari topilmadi - xavfsizlik uchun o'tkazib yuborildi.")
        return 0
    now = time.time()
    orphans = []
    for obj in await _list_bucket_objects():
        name = obj.get("name")
        if not name or not obj.get("id") or name in referenced:     # id yo'q = papka
            continue
        created = _parse_ts(obj.get("created_at"))
        if created is None or now - created < ORPHAN_MIN_AGE:
            continue
        orphans.append(name)
    if orphans:
        await delete_storage_objects(orphans)
    return len(orphans)


async def orphan_sweep_loop() -> None:
    await asyncio.sleep(ORPHAN_SWEEP_FIRST_DELAY)
    while True:
        try:
            removed = await sweep_orphan_images()
            log.info("Rasm tozalash tugadi: %d ta ishlatilmayotgan rasm o'chirildi.", removed)
        except Exception:
            log.exception("Rasm tozalashda xato (keyingi urinish %.0f soatdan keyin)", ORPHAN_SWEEP_INTERVAL / 3600)
        await asyncio.sleep(ORPHAN_SWEEP_INTERVAL)


# =============================================================================
# 8. TEXTS & KEYBOARDS
# =============================================================================
SEP = "─" * 16   # Telegram pufagida bitta qatorga sig'adi (uzun bo'lsa 2 qatorga o'tib ketadi)
STAGE_TOTAL = 2

BTN_CHANGE = "🔄 Katalogni o'zgartirish"
BTN_CONTINUE = "➡️ Davom etish"
BTN_RETRY = "🔁 Qayta urinish"

PROMPT_ALL_IMAGES = (
    "🖼 <b>Barcha rasmlarni bir yo'la yuboring</b>\n"
    f"{SEP}\n"
    "1️⃣ <b>Asosiy rasmlar</b>\n"
    "2️⃣ <b>Variant va narx rasmi</b> — rang, o'lcham va narx ko'rinadigan (bitta yoki bir nechta, ketma-ket)\n"
    "3️⃣ <b>Komentariya rasmlari</b>\n"
    f"{SEP}\n"
    "<i>Bot variant rasmini o'zi topadi. Variantlar va narx alohida rasmlarda bo'lsa ham mayli — faqat ularni ketma-ket yuboring.</i>"
)
PROMPT_COVER = "🌟 Mahsulotning <b>asosiy (muqova) rasmini</b> yuboring"

STATE_PROMPTS = {
    ProductAdd.all_images.state: PROMPT_ALL_IMAGES,
    ProductAdd.main_cover.state: PROMPT_COVER,
}

Q_SEASON = "🌦 Qaysi fasl uchun?"
Q_GENDER = "👫 Kimlar uchun?"
Q_TYPE = "👟 Poyabzal turini tanlang:"
Q_PLACE = "📍 Qayerda kiyiladi?"


def block(*parts: str) -> str:
    return f"\n{SEP}\n".join(parts)


def progress_bar(stage: int) -> str:
    return "▰" * stage + "▱" * (STAGE_TOTAL - stage)


def stage_text(head: str | None, catalog: str, stage: int, prompt: str) -> str:
    parts = ([head] if head else []) + [
        f"📚 <b>Katalog:</b> {html.escape(catalog)}",
        f"⏳ <b>Status:</b> {stage}-bosqich  {progress_bar(stage)}",
        prompt,
    ]
    return block(*parts) + "\n" + SEP


def quiz_text(draft: dict, question: str) -> str:
    chosen = path_label(draft)
    head = "📚 <b>Katalogni tanlang</b>" + (f"\n🧭 {html.escape(chosen)}" if chosen else "")
    return block(head, question)


def fmt_price(p) -> str:
    p = float(p)
    return str(int(p)) if p.is_integer() else f"{p:.2f}".rstrip("0").rstrip(".")


def success_text(path: dict, price, colors: list[str], sizes: list[int], product_id) -> str:
    details = "\n".join([
        f"📚 <b>Katalog:</b> {html.escape(path_label(path or {}))}",
        f"💰 <b>Narx:</b> {fmt_price(price)} ¥",
        f"🎨 <b>Rang:</b> {html.escape(', '.join(colors))}",
        f"📏 <b>O'lcham:</b> {', '.join(map(str, sizes))}",
        f"🆔 <b>ID:</b> {html.escape(str(product_id))}",
    ])
    return block("🎉 <b>Mahsulot muvaffaqiyatli qo'shildi!</b>", details) + "\n" + SEP


def upload_error_text(e: Exception, accepted: int) -> str:
    return (
        f"⚠️ <b>Rasmni yuklab bo'lmadi</b>\n"
        f"<i>{err(e)}</i>\n"
        f"{SEP}\n"
        f"📥 Qabul qilingan rasmlar: <b>{accepted}</b> ta\n"
        f"🔄 Iltimos, ushbu bosqich rasmlarini qaytadan yuboring yoki «{BTN_CHANGE}» tugmasini bosing."
    )


def save_error_text(e: Exception, again: bool = False) -> str:
    head = "⚠️ <b>Yana xatolik yuz berdi</b>" if again else "⚠️ <b>Mahsulotni bazaga saqlab bo'lmadi</b>"
    return f"{head}\n<i>{err(e)}</i>\n{SEP}\n🔁 Pastdagi «{BTN_RETRY}» tugmasini bosing."


def scan_error_text(errors: int) -> str:
    return (f"⚠️ <b>AI rasmlarni tekshira olmadi</b> ({errors} ta xato)\n{SEP}\n"
            f"🔁 «{BTN_RETRY}» tugmasini bosing — rasmlarni qayta yuborish shart emas.")


def _kb_reply(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def kb_process():
    return _kb_reply([[BTN_CHANGE]])


def kb_after_success():
    return _kb_reply([[BTN_CHANGE, BTN_CONTINUE]])


def kb_error_retry():
    return _kb_reply([[BTN_RETRY], [BTN_CHANGE]])


def _kb_inline(prefix: str, options: list[tuple[str, str]], back: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"{prefix}:{value}")] for value, label in options]
    if back:
        rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"qb:{back}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_quiz_season():
    return _kb_inline("qs", SEASONS)


def kb_quiz_gender():
    return _kb_inline("qg", GENDERS, back="season")


def kb_quiz_type(season, gender):
    return _kb_inline("qt", TYPES_BY_SG.get((season, gender), []), back="gender")


def kb_quiz_place():
    return _kb_inline("qp", PLACES, back="type")


# =============================================================================
# 9. HANDLERS
# =============================================================================
class AdminOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id in ADMIN_IDS:
            return await handler(event, data)
        try:
            if isinstance(event, Message):
                if should_notify(user.id if user else 0, "denied", DENY_COOLDOWN):
                    await reply(event, "⛔️ <b>Ruxsat yo'q.</b>\nBu bot faqat administratorlar uchun.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Ruxsat yo'q.", show_alert=True)
        except TelegramAPIError as e:
            log.debug("Ruxsat yo'q javobini yuborib bo'lmadi: %s", e)
        return None


dp.message.outer_middleware(AdminOnly())
dp.callback_query.outer_middleware(AdminOnly())


# ---------- helpers ----------
def ordered_urls(items) -> list[str]:
    return [u for _, u in sorted(items or [], key=lambda x: x[0])]


async def catalog_name(state: FSMContext) -> str:
    return path_label((await state.get_data()).get("catalog") or {})


async def _auto_advance(user_id: int, action, pause: float) -> None:
    """Debounce: oxirgi rasmdan `pause` soniya o'tgach action() ishga tushadi."""
    me = asyncio.current_task()
    try:
        while True:
            wait = pause - (time.monotonic() - last_photo_at.get(user_id, 0.0))
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
            last_photo_at.pop(user_id, None)


def ensure_advance(user_id: int, stage_key: str, action, pause: float = BULK_PAUSE) -> None:
    last_photo_at[user_id] = time.monotonic()
    cur = advance_tasks.get(user_id)
    if cur and cur[0] == stage_key and not cur[1].done():
        return
    task = asyncio.create_task(_auto_advance(user_id, action, pause), name=f"advance-{user_id}-{stage_key}")
    task.add_done_callback(_consume_result)
    advance_tasks[user_id] = (stage_key, task)


# ---------- start / home ----------
async def send_start_quiz(msg: Message, state: FSMContext):
    reset_flow(msg.from_user.id)
    await state.clear()
    await reply(msg, quiz_text({}, Q_SEASON), reply_markup=kb_quiz_season())
    # Pastki klaviaturani yo'qotish uchun vaqtinchalik xabar yuborilib, darhol o'chiriladi.
    cleaner = await reply(msg, "🧹", reply_markup=ReplyKeyboardRemove())
    await drop_msg(cleaner)


@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await send_start_quiz(msg, state)


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await send_start_quiz(msg, state)


@dp.message(F.text == BTN_CHANGE)
async def btn_change(msg: Message, state: FSMContext):
    await send_start_quiz(msg, state)


# ---------- quiz (Fasl -> Jins -> Tur [-> Joy]) ----------
def _cb_arg(cb: CallbackQuery) -> str:
    return (cb.data or "").split(":", 1)[1]


async def _stale(cb: CallbackQuery) -> None:
    await cb.answer("⌛️ Bu tugma eskirgan. Iltimos, /start ni bosing.", show_alert=True)


async def _edit_quiz(message: Message, draft: dict, question: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await tg_call(message.edit_text, quiz_text(draft, question), reply_markup=markup)
    except TelegramBadRequest:
        pass  # "message is not modified" va shunga o'xshash zararsiz holatlar


@dp.callback_query(F.data.startswith("qs:"))
async def cb_qs(cb: CallbackQuery, state: FSMContext):
    season = _cb_arg(cb)
    if season not in dict(SEASONS) or not isinstance(cb.message, Message):
        return await _stale(cb)
    await cb.answer()
    draft = {"season": season}
    await state.update_data(catalog_draft=draft)
    await _edit_quiz(cb.message, draft, Q_GENDER, kb_quiz_gender())


@dp.callback_query(F.data.startswith("qg:"))
async def cb_qg(cb: CallbackQuery, state: FSMContext):
    gender = _cb_arg(cb)
    draft = (await state.get_data()).get("catalog_draft") or {}
    season = draft.get("season")
    if gender not in dict(GENDERS) or (season, gender) not in TYPES_BY_SG or not isinstance(cb.message, Message):
        return await _stale(cb)
    await cb.answer()
    draft = {"season": season, "gender": gender}
    await state.update_data(catalog_draft=draft)
    await _edit_quiz(cb.message, draft, Q_TYPE, kb_quiz_type(season, gender))


@dp.callback_query(F.data.startswith("qt:"))
async def cb_qt(cb: CallbackQuery, state: FSMContext):
    ptype = _cb_arg(cb)
    draft = (await state.get_data()).get("catalog_draft") or {}
    valid_types = {v for v, _ in TYPES_BY_SG.get((draft.get("season"), draft.get("gender")), [])}
    if ptype not in valid_types or not isinstance(cb.message, Message):
        return await _stale(cb)
    await cb.answer()
    draft = {k: v for k, v in draft.items() if k != "place"}
    draft["type"] = ptype
    await state.update_data(catalog_draft=draft)
    if ptype == "shippak":
        await _edit_quiz(cb.message, draft, Q_PLACE, kb_quiz_place())
    else:
        await finish_quiz(cb.message, state, draft)


@dp.callback_query(F.data.startswith("qp:"))
async def cb_qp(cb: CallbackQuery, state: FSMContext):
    place = _cb_arg(cb)
    draft = (await state.get_data()).get("catalog_draft") or {}
    if draft.get("type") != "shippak" or place not in dict(PLACES) or not isinstance(cb.message, Message):
        return await _stale(cb)
    await cb.answer()
    draft = {**draft, "place": place}
    await state.update_data(catalog_draft=draft)
    await finish_quiz(cb.message, state, draft)


@dp.callback_query(F.data.startswith("qb:"))
async def cb_qb(cb: CallbackQuery, state: FSMContext):
    """Quizda bir qadam orqaga qaytish."""
    target = _cb_arg(cb)
    draft = (await state.get_data()).get("catalog_draft") or {}
    season, gender = draft.get("season"), draft.get("gender")
    if not isinstance(cb.message, Message):
        return await _stale(cb)
    if target == "season":
        new, question, markup = {}, Q_SEASON, kb_quiz_season()
    elif target == "gender" and season:
        new, question, markup = {"season": season}, Q_GENDER, kb_quiz_gender()
    elif target == "type" and season and gender:
        new, question, markup = {"season": season, "gender": gender}, Q_TYPE, kb_quiz_type(season, gender)
    else:
        return await _stale(cb)
    await cb.answer()
    await state.update_data(catalog_draft=new)
    await _edit_quiz(cb.message, new, question, markup)


async def finish_quiz(message: Message, state: FSMContext, draft: dict):
    """Katalog tayyor: quiz xabarini o'chirib, 1-bosqichni boshlaymiz."""
    path = {k: draft.get(k) for k in ("season", "gender", "type", "place")}
    await state.set_state(ProductAdd.all_images)
    await state.update_data(catalog=path, catalog_draft=draft, all_images=[], main_images=[], comment_images=[],
                            ai_result=None, failed_row=None)
    await drop_msg(message)
    await reply(message, stage_text(None, path_label(path), 1, PROMPT_ALL_IMAGES), reply_markup=kb_process())


# ---------- bosqichlar ----------
@dp.message(F.text == BTN_CONTINUE)
async def btn_continue(msg: Message, state: FSMContext):
    path = (await state.get_data()).get("catalog")
    if not path:
        return await send_start_quiz(msg, state)
    reset_flow(msg.from_user.id)
    await state.set_state(ProductAdd.all_images)
    await state.update_data(all_images=[], main_images=[], comment_images=[], ai_result=None, failed_row=None)
    await reply(msg, stage_text(None, path_label(path), 1, PROMPT_ALL_IMAGES), reply_markup=kb_process())


@dp.message(ProductAdd.all_images, F.photo)
async def msg_all_images(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid in busy:
        # Tahlil ketyapti: kech kelgan rasmlar e'tiborga olinmaydi (albom qoldiqlari keyin ham jim o'tkaziladi).
        if msg.media_group_id:
            ignored_groups[uid] = (msg.media_group_id, time.monotonic())
        return
    try:
        url = await save_photo(msg)
    except Exception as e:
        have = len((await state.get_data()).get("all_images") or [])
        return await reply_once(msg, "upload_err", upload_error_text(e, have))
    async with get_lock(uid):
        if await state.get_state() != ProductAdd.all_images.state or uid in busy:
            return
        items = (await state.get_data()).get("all_images") or []
        items.append([msg.message_id, url])
        await state.update_data(all_images=items)
    ensure_advance(uid, "bulk", partial(_process_bulk, uid, msg.chat.id, state))


async def _scan_images(images: list) -> tuple[int, int, list[dict], int, bool]:
    """
    images: [[msg_id, url], ...] (xabar tartibida). Har bir rasm BITTA AI so'rovida tekshiriladi va o'qiladi.
    Birinchi variant rasmi - milestone. Undan keyingi rasmlar ham variant rasmi bo'lsa qo'shiladi;
    birinchi variant bo'lmagan rasmda to'xtaymiz (keyingilari tekshirilmaydi).
    Qaytaradi: (start, end, parts, errors, interrupted); variant rasmlari = images[start:end], start=-1 - topilmadi.
    interrupted - variant topilgandan keyin AI xato berib, qator oxirigacha tekshirib bo'lmadi.
    """
    start, parts, errors = -1, [], 0
    for i, (_, url) in enumerate(images):
        try:
            res = await analyze_image(url)
        except Exception as e:
            errors += 1
            log.warning("Rasm #%d tekshirilmadi: %s", i + 1, describe_error(e))
            if start != -1:
                return start, i, parts, errors, True
            continue
        if res["is_variant"]:
            if start == -1:
                start = i
            parts.append(res)
        elif start != -1:
            return start, i, parts, errors, False
    return start, (len(images) if start != -1 else -1), parts, errors, False


async def _process_bulk(user_id: int, chat_id: int, state: FSMContext) -> None:
    if await state.get_state() != ProductAdd.all_images.state or user_id in busy:
        return
    # Rasmlar parallel yuklanadi, shuning uchun tartib xabar ID bo'yicha tiklanadi.
    images = sorted((await state.get_data()).get("all_images") or [], key=lambda x: x[0])
    if not images:
        return
    busy.add(user_id)
    try:
        status = await send(chat_id, f"🔍 <b>Tahlil qilinmoqda...</b>\n📸 {len(images)} ta rasm")
        start, end, parts, errors, interrupted = await _scan_images(images)
        if await state.get_state() != ProductAdd.all_images.state:
            return await drop_msg(status)

        if interrupted or (start == -1 and errors):
            # AI xatosi: rasmlar saqlanadi, «Qayta urinish» ularni qayta yuklamasdan qayta tahlil qiladi.
            await drop_msg(status)
            return await send(chat_id, scan_error_text(errors), reply_markup=kb_error_retry())

        problem = None
        if start == -1:
            problem = ("🤷 <b>Variant rasmi topilmadi</b>\n"
                       "Rang, o'lcham va narx ko'rinadigan rasm bo'lishi kerak. Rasmlarni qayta yuboring.")
        else:
            try:
                result = _merge_parts(parts)
            except ValueError as e:
                problem = (f"🤷 <b>Variant rasmlaridan topilmadi:</b> {html.escape(str(e))}\n"
                           f"Variant va narx rasmlari albomda ketma-ket turishi kerak. Rasmlarni qayta yuboring.")
        if problem:
            await state.update_data(all_images=[])
            return await show(status, f"{problem}\n{SEP}")

        await state.update_data(main_images=images[:start], comment_images=images[end:], ai_result=result)
        await state.set_state(ProductAdd.main_cover)
        head = (f"✅ <b>Variant o'qildi</b> ({end - start} ta rasm, {start + 1}-rasmdan)\n"
                f"💰 {fmt_price(result['priceYuan'])} ¥ · 🎨 {html.escape(', '.join(result['colors']))}\n"
                f"📏 {', '.join(map(str, result['sizes']))}")
        if errors:
            head += f"\n⚠️ {errors} ta rasm tekshirilmadi — ma'lumotlarni tekshirib ko'ring."
        await show(status, stage_text(head, await catalog_name(state), 2, PROMPT_COVER))
    finally:
        busy.discard(user_id)


STATS_MIN_GAP = 5   # sales views'dan, comments sales'dan kamida shuncha pog'ona (indeks) orqada


def generate_stats() -> dict:
    """
    Statistikani STATS_POOL indekslari bo'yicha tanlaydi:
        views  - tasodifiy (sales va comments uchun joy qoladigan oraliqdan)
        likes  < views
        sales  <= views - 5 pog'ona
        comments <= sales - 5 pog'ona
        rating - 4.4 .. 5.0 oralig'ida tasodifiy kasr son
    """
    min_views = 2 * STATS_MIN_GAP                     # sales va comments sig'ishi uchun
    views_i = random.randint(min_views, len(STATS_POOL) - 1)
    likes_i = random.randint(0, views_i - 1)
    sales_i = random.randint(STATS_MIN_GAP, views_i - STATS_MIN_GAP)
    comments_i = random.randint(0, sales_i - STATS_MIN_GAP)
    return {
        "likes": STATS_POOL[likes_i],
        "views": STATS_POOL[views_i],
        "sales": STATS_POOL[sales_i],
        "comments": STATS_POOL[comments_i],
        "rating": round(random.uniform(4.4, 5.0), 1),
    }


def build_row(ai_result: dict, path: dict, cover_url: str, data: dict) -> dict:
    images = [cover_url] + ordered_urls(data.get("main_images"))
    # Kommentariyalar HTML tomonda qo'shiladi. Bot faqat rasm havolalarini uzatadi.
    comment_images = ordered_urls(data.get("comment_images"))
    stats = generate_stats()
    return {
        "name": "", "name_ru": "", "price": 0,
        "price_yuan": ai_result["priceYuan"],
        "catalog": quiz_catalog_id(path),
        "rating": stats["rating"],
        "likes": stats["likes"], "views": stats["views"],
        "sales": stats["sales"], "comments": stats["comments"],
        "images": images,
        "comment_images": comment_images, # HTML o'zi sharh matnlari bilan bog'laydi
        "description": "", "description_ru": "",
        "added_at": int(time.time() * 1000),
        "variants": {
            "colors": ai_result["colors"],
            "colorsRu": [COLOR_UZ_TO_RU[c] for c in ai_result["colors"]],
            "sizes": ai_result["sizes"],
            "catPath": path,
        },
    }


@dp.message(ProductAdd.main_cover, F.photo)
async def msg_main_cover(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if msg.media_group_id:
        ignored_groups[uid] = (msg.media_group_id, time.monotonic())   # albomdagi qolgan rasmlar e'tiborga olinmaydi
    if uid in busy:
        return
    busy.add(uid)
    try:
        await _create_product(msg, state)
    finally:
        busy.discard(uid)


async def _create_product(msg: Message, state: FSMContext) -> None:
    try:
        cover_url = await save_photo(msg)
    except Exception as e:
        accepted = len((await state.get_data()).get("main_images") or [])
        await reply(msg, upload_error_text(e, accepted))
        return

    status = await reply(msg, "⏳ <b>Mahsulot yaratilmoqda...</b>")

    data = await state.get_data()
    path, ai_result = data.get("catalog"), data.get("ai_result")
    if not path or not ai_result or await state.get_state() != ProductAdd.main_cover.state:
        return await drop_msg(status)

    row = build_row(ai_result, path, cover_url, data)
    try:
        res = await save_product(row)
    except Exception as e:
        await drop_msg(status)
        await state.set_state(None)
        await state.update_data(failed_row=row, all_images=[])
        return await reply(msg, save_error_text(e), reply_markup=kb_error_retry())

    final = success_text(path, ai_result["priceYuan"], ai_result["colors"], ai_result["sizes"], res.get("product_id"))
    await drop_msg(status)
    if await state.get_state() == ProductAdd.main_cover.state:
        await state.set_state(None)
        await state.update_data(all_images=[], main_images=[], comment_images=[], ai_result=None, failed_row=None)
        await reply(msg, final, reply_markup=kb_after_success())
    else:
        await reply(msg, final)


@dp.message(F.text == BTN_RETRY)
async def btn_retry(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    data = await state.get_data()
    row = data.get("failed_row")
    if not row:
        if data.get("all_images") and await state.get_state() == ProductAdd.all_images.state:
            return await _process_bulk(uid, msg.chat.id, state)   # AI xatosi: saqlangan rasmlarni qayta tahlil qilish
        return await reply(msg, "🤔 Xato ma'lumotlari topilmadi. Iltimos, qaytadan boshlang.", reply_markup=kb_process())
    if uid in busy:
        return
    busy.add(uid)
    try:
        status = await reply(msg, "⏳ <b>Mahsulot qayta yuklanmoqda...</b>")
        try:
            res = await save_product(row)
        except Exception as e:
            await drop_msg(status)
            return await reply(msg, save_error_text(e, again=True), reply_markup=kb_error_retry())

        variants = row["variants"]
        final = success_text(data.get("catalog"), row["price_yuan"], variants["colors"], variants["sizes"], res.get("product_id"))
        await drop_msg(status)
        await state.set_state(None)
        await state.update_data(failed_row=None, all_images=[], main_images=[], comment_images=[], ai_result=None)
        await reply(msg, final, reply_markup=kb_after_success())
    finally:
        busy.discard(uid)


# ---------- fallback'lar ----------
@dp.message(F.text, ~F.text.startswith("/"))
async def msg_fallback(msg: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        hint = STATE_PROMPTS.get(current, "")
        text = "📷 <b>Hozir rasm kutyapman</b>, matn qabul qilmayman." + (f"\n{SEP}\n{hint}" if hint else "")
        return await reply_once(msg, "text_in_state", text)
    data = await state.get_data()
    if data.get("failed_row"):
        await reply_once(msg, "text_idle", f"⚠️ Mahsulotni saqlashda xato bo'lgan.\n«{BTN_RETRY}» tugmasini bosing yoki «{BTN_CHANGE}» orqali yangidan boshlang.", reply_markup=kb_error_retry())
    elif data.get("catalog"):
        await reply_once(msg, "text_idle", f"➡️ Davom etish uchun «{BTN_CONTINUE}» tugmasini bosing.", reply_markup=kb_after_success())
    else:
        await send_start_quiz(msg, state)


@dp.message(F.photo)
async def msg_photo_fallback(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    group = ignored_groups.get(uid)
    if msg.media_group_id and group and group[0] == msg.media_group_id:
        return
    if (await state.get_data()).get("catalog"):
        return await reply_once(msg, "photo_idle", f"➡️ Davom etish uchun «{BTN_CONTINUE}» tugmasini bosing.")
    await send_start_quiz(msg, state)


@dp.message(~F.text & ~F.photo)
async def msg_other_fallback(msg: Message, state: FSMContext):
    """Fayl (document), video, ovozli xabar, stiker va boshqa turlar."""
    if msg.document:
        text = ("📎 <b>Fayl qabul qilinmaydi.</b>\n"
                "Rasmni <b>fayl sifatida emas</b>, oddiy <b>rasm (photo)</b> sifatida yuboring — "
                "galereyadan tanlaganda «Fayl sifatida yuborish» ni o'chirib qo'ying.")
    else:
        text = "🤷 <b>Bu turdagi xabarni qabul qila olmayman.</b>\nIltimos, faqat rasm yuboring."
    hint = STATE_PROMPTS.get(await state.get_state() or "")
    if hint:
        text += f"\n{SEP}\n{hint}"
    await reply_once(msg, "other_type", text)


@dp.callback_query()
async def cb_stale(cb: CallbackQuery):
    await cb.answer("⌛️ Bu tugma eskirgan yoki noto'g'ri. /start ni bosing.", show_alert=True)


@dp.error()
async def on_error(event: ErrorEvent):
    log.error("Handlerda kutilmagan xato: %r", event.exception, exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("⚠️ Kutilmagan xatolik. Iltimos, qayta urinib ko'ring.", show_alert=True)
        elif update.message and should_notify(update.message.from_user.id, "unexpected", 5.0):
            await reply(update.message, "⚠️ <b>Kutilmagan xatolik yuz berdi.</b>\nIltimos, birozdan so'ng qayta urinib ko'ring yoki /start ni bosing.")
    except Exception as e:
        log.debug("Xato haqida xabar berib bo'lmadi: %s", e)
    return True


# =============================================================================
# 10. STARTUP
# =============================================================================
async def start_health_server():
    port = os.getenv("PORT")
    if not port:
        return None

    async def health(_request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
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
        raise SystemExit("Kamida bitta AI kaliti kerak.")


async def main():
    check_config()
    runner = await start_health_server()
    janitor = asyncio.create_task(cleanup_loop(), name="janitor")
    sweeper = asyncio.create_task(orphan_sweep_loop(), name="orphan-sweep") if ORPHAN_SWEEP_ENABLED else None
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        if sweeper:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
        shutdown_user_tasks()
        if runner:
            await runner.cleanup()
        if _session and not _session.closed:
            await _session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())