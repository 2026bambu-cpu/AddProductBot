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
from dataclasses import dataclass, field
from functools import partial

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
PHOTO_PAUSE = 2.0                # oxirgi rasmdan keyin shuncha kutib, keyingi bosqichga o'tiladi

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

# --- Flood control ---
CHAT_MIN_INTERVAL = 0.5          # bitta chatga ketma-ket xabarlar orasidagi minimal masofa (soniya)
NOTIFY_COOLDOWN = 3.0            # bir xil ogohlantirishni qayta yuborish oralig'i (soniya)
DENY_COOLDOWN = 30.0             # ruxsatsiz foydalanuvchiga javob oralig'i

# --- Cleanup (memory leak oldini olish) ---
CLEANUP_INTERVAL = 60.0
AI_TASK_TTL = 30 * 60            # tugamagan/olinmagan AI natijasi shuncha turadi
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


# 200 ta tayyor sharh. Har mahsulotga random.sample bilan 10 tasi (takrorlanmasdan) tanlanadi.
REVIEWS_POOL = [
    "Sifati juda zo'r ekan, kiyishga juda qulay.", "Отличное качество, очень доволен покупкой.",
    "Narxi ham arzon, puliga to'liq arziydi.", "Yengil ekan, oyoqni umuman charchatmaydi.",
    "Удобные, размер подошел идеально.", "O'lchami standart bo'yicha to'g'ri keldi, siqmaydi ham.",
    "Dizayni juda chiroyli, rasmda ko'rsatilganidek keldi.", "Стоят своих денег на все 100%.",
    "Materiali sifatli, chidamli ko'rinadi.", "Tez yetib keldi, qadoqlash ham yaxshi ekan.",
    "Быстрая доставка, товар полностью соответствует описанию.", "Hidi yo'q, bu juda muhim ekan.",
    "Do'stlarimga ham tavsiya qildim.", "Oyoqqa juda yumshoq, kiyib yurganda bilinmaydi.",
    "Легкие, ноги совсем не устают.", "Tikuvlari puxta ekan, hech qayeridan choklari chiqmayapti.",
    "Kundalik kiyish uchun juda mos keladi.", "Симпатичный дизайн, выглядят стильно.",
    "O'lcham olishda sotuvchining maslahati to'g'ri chiqdi.", "Shu narxga bunday sifatli mahsulot topish qiyin.",
    "Материал качественный, никакого химического запаха нет.", "Oilam a'zolariga ham sotib olaman.",
    "Kiyib ko'rdim, oyog'imga xuddi o'lchab tikilgandek tushdi.", "За такие деньги - просто супер!",
    "Sotuvchi juda muloyim ekan, savollarga tez javob berdi.", "Rangi va fasoni juda zamonaviy.",
    "Namlikni o'tkazmaydi, yomg'irda ham kiyish mumkin ekan.", "Искренне рекомендую к покупке.",
    "Yurish uchun juda qulay, tovon og'rimaydi.", "Yetkazib berish xizmati juda tez ishladi.",
    "Упаковка отличная, все пришло в целости.", "Tashqi ko'rinishi juda toza va ozoda tikilgan.",
    "Qishki kiyimlar uchun ham xuddi shu do'kondan olaman.", "Очень мягкие и удобные, не натирают.",
    "Oyoq terlamaydigan material ekan, juda yaxshi.", "Kutilganidan ham yaxshiroq chiqdi.",
    "Narxi bozorlardagidan ancha arzon ekan.", "Швы ровные и аккуратные, сделано качественно.",
    "Bolamga oldim, juda xursand bo'ldi, oyog'idan tushirmayapti.", "Sirpanmaydi, bu juda muhim.",
    "Идеально подходят для повседневной носки.", "Moslashuvchan tagcharmi bor, yurganda oyoq bukilishiga xalaqit bermaydi.",
    "Xaridimdan juda mamnunman, hammaga tavsiya qilaman.", "Birinchi marta shu do'kondan xarid qildim, omadim keldi.",
    "Продавец быстро отвечает на сообщения и дает дельные советы.", "O'lchami aniq keldi, kattaroq yoki kichikroq olmang.",
    "Ko'rinishi qimmatbaho, lekin narxi juda hamyonbop.", "Цвет и фасон очень современные.",
    "Oyoqni isitmaydi, yoz uchun ham kiyish mumkin.", "Ichki qismi yumshoq materialdan qilingan ekan.",
    "Kuryer uyga chaqqonlik bilan yetkazib berdi.", "Подошва гибкая, при ходьбе не мешает.",
    "Qutisi ham juda chiroyli, sovg'a qilishga ham bo'ladi.", "Uzoq vaqt kiysam ham oyoq charchamadi.",
    "Ноги не потеют, материал дышащий.", "Krossovkalarim uchun ideal.",
    "Mahsulot haqiqiy, sifati a'lo darajada.", "O'lchamlar jadvali to'g'ri ishlangan, xato qilmadim.",
    "Прекрасное соотношение цены и качества.", "Charmi yumshoq va silliq ekan.",
    "Kiyish va yechish juda oson.", "Взял(а) себе и родным, все очень довольны.",
    "Hech qanday nuqsoni yo'q, hammasi joyida.", "Arzon narxda ajoyib xarid qildim.",
    "Sotuvchiga katta rahmat, sifatli mahsulot ekan.", "Не скользят, что очень важно.",
    "Oyoq shaklini chiroyli ko'rsatadi.", "Yomg'irli kunlar uchun juda mos.",
    "Внешний вид дороже, чем реальная цена.", "Bolalar bog'chasi uchun almashtirishga oldim, zo'r.",
    "Tashqi ko'rinishi rasmdagi bilan bir xil.", "Курьер доставил быстро и вежливо.",
    "Iplari ham silliq, hech qanday g'adir-buduri yo'q.", "Yengilligi tufayli sport uchun ham ishlatsa bo'ladi.",
    "Внутри очень мягкая подкладка.", "Kechqurun sayrga chiqishda juda qulay.",
    "Narxiga qarab sifatini kutmagan edim, juda xursandman.", "Tovoni yumshoq, yurganda taqillamaydi.",
    "Смотрятся аккуратно на ноге.", "Boshqa ranglarini ham buyurtma qilaman.",
    "Xaridorlarga ishonch bilan tavsiya qilaman.", "Отличный вариант на каждый день.",
    "O'lchami to'g'ri kelmadi deb qo'rqqan edim, lekin hammasi yaxshi.", "Yetkazib berish uchun katta rahmat, juda tez keldi.",
    "Materiali gigiyenik talablarga javob beradi.", "Брал(а) на сменку, очень удобно.",
    "Bahorgi va kuzgi mavsum uchun eng yaxshi tanlov.", "Kiyganda oyoqni siqmaydi va ishqalamaydi.",
    "Никаких дефектов, все как на картинке.", "Sotib olganimga rosa bir hafta bo'ldi, hech qanday muammo yo'q.",
    "Do'konning xizmat ko'rsatishi a'lo darajada.", "Pulim bekorga ketmadi, rosa arziydi.",
    "Покупка превзошла все мои ожидания.", "Oyoq panjasi keng bo'lsa ham bemalol kiyaveradi.",
    "Dizayni ham, sifati ham meni xursand qildi.", "Спасибо продавцу за качественный товар!",
    "Bu mahsulotni sotib olishdan oldin ikkilanib turgan edim, behuda ekan.", "Yana shu sotuvchidan xarid qilishni rejalashtirmoqdaman.",
    "Обязательно закажу еще другие модели у этого магазина.", "Buyurtma berganimga afsuslanmadim, a'lo mahsulot.",
    "Narxiga qarab juda zo'r.", "Отличная покупка, рекомендую.",
    "Oyoqqa juda yaxshi tushdi.", "Do'stlarga ham tavsiya qildim, hamma maqtadi. Kiyishga juda qulay ekan.",
    "Супер, беру уже вторую пару.", "Sifati zo'r, hech qanday choklari chiqib ketmagan. Bir hafta kiyib ko'rdim, hammasi joyida.",
    "Juda yengil ekan.", "Удобные, но цвет немного отличается от фото.",
    "O'lchami standart, o'zimga to'g'ri keldi.", "Отличный вариант за свои деньги.",
    "Kutilganidan yaxshiroq chiqdi.", "Oyoq terlamaydi, yaxshi material ekan.",
    "Отличное качество пошива.", "Bolamga oldim, juda xursand.",
    "Хорошие, мягкие. Немного пахли при открытии, но выветрились.", "Narxi juda arzon, sifati a'lo.",
    "Kiyib yurganda oyoq charchamaydi.", "Спасибо, всё подошло!",
    "Tez yetib keldi, kuryerga rahmat.", "Идеально сидят на ноге, беру на каждый день.",
    "Tikuvi puxta, uzoqqa chidaydi.", "Ko'rinishi ham chiroyli, sifati ham.",
    "Отличная модель, очень удобная колодка.", "Qutisi shikastlanmagan holda keldi.",
    "Хорошая цена, быстрая доставка.", "Sotuvchi savollarga tez javob berdi. Mahsulotga kelsak, juda yumshoq va qulay.",
    "Tagcharmi yumshoq, yurganda juda qulay.", "Просто супер, ничего лишнего.",
    "Rasmda ko'rsatilgan rang bilan bir xil.", "Удобная колодка, не жмут.",
    "Sifati juda zo'r, oyog'imga to'g'ri keldi.", "Брал мужу, он в восторге.",
    "Yomg'irda sirpanmaydi.", "Отличные, дышащие.",
    "Do'konga katta rahmat, sifatli mahsulot ekan.", "Har kuni kiyish uchun ideal.",
    "Хорошее качество, соответствует картинке.", "O'lcham olishda sotuvchining maslahatiga amal qildim, to'g'ri keldi.",
    "Очень легкие и удобные.", "Bahorda kiyish uchun zo'r.",
    "Покупкой остался доволен.", "Qishda ham kiyish mumkin ekan, issiq.",
    "Симпатичные, аккуратные.", "Sotib olganimga afsuslanmayman.",
    "Быстро пришло, размер в размер.", "Tashqi ko'rinishi juda toza.",
    "Отлично, спасибо продавцу.", "Uzoq vaqt kiysam ham hech qanday noqulaylik sezmadim.",
    "Не натирают, очень комфортные.", "O'lchami aniq keldi.",
    "Цена-качество супер.", "Materiali yumshoq, hech qanday qattiq joyi yo'q.",
    "Отличная покупка!", "Kundalik kiyish uchun juda mos.",
    "Легкие, как перышко.", "Narxi bozordan arzon, sifati undan ham yaxshi. Oilamga ham olaman.",
    "Просто класс.", "Yurish uchun juda qulay.",
    "Хорошее качество, беру не первый раз.", "Hidi yo'q, bu juda muhim.",
    "Отличный внешний вид.", "Sport uchun ham, sayr uchun ham mos keladi.",
    "Доволен как слон.", "Oyoqqa kiyganda juda yumshoq.",
    "Подошва мягкая, не устают ноги.", "Sifati a'lo, puliga arziydi.",
    "Суперские!", "Chidamli ekan, ko'p kiyishga to'g'ri keladi.",
    "Хорошо смотрятся с любой одеждой.", "O'lchami kichikroq olindi, lekin sifati yaxshi.",
    "Отличные, беру для всей семьи.", "Rangi o'chmaydi, artib ko'rdim.",
    "Очень удобные, не скользят.", "Tez keldi, o'ralgani ham yaxshi.",
    "Качество понравилось, рекомендую.", "Oyog'im 40 o'lcham, 40 oldim, rosa bo'ldi.",
    "Мягкие и удобные.", "Kiyishga oson, yechishga ham.",
    "Внешне выглядят дорого.", "Sotuvchiga rahmat, sifatli.",
    "Просто бомба за такие деньги.", "Har kuni kiyaman, juda yoqdi.",
    "Не жмут, не натирают.", "Oyoqning shaklini chiroyli ko'rsatadi.",
    "Отлично, рекомендую.", "Kutilganidek keldi.",
    "Всё супер, спасибо!", "Sifati juda yaxshi, hech qanday shikoyatim yo'q.",
    "Очень довольна покупкой, размер точный.", "Yengil va qulay.",
    "Крутые, беру еще.", "Yozda kiyish uchun ham mos.",
    "Сделано качественно, нитки не торчат.", "Oyoqni charchatmaydi.",
    "Хорошая пара, пришла быстро.", "Rangi juda chiroyli.",
    "Удобные и стильные.", "Boshqalarga ham tavsiya qilaman, arzon va sifatli.",
    "Покупкой полностью доволен.", "Xaridimdan juda mamnunman!"
]


AI_SYSTEM_PROMPT = """You are an expert, strict data extractor for an Uzbek shoe marketplace.
TASK: Read a product variant image (usually a Chinese seller's card). Return ONLY a JSON object matching the schema below.
SCHEMA:
{
  "colors":   [string],
  "sizes":    [int],
  "priceYuan": number,
  "rating":   number,
  "likes":    string,
  "views":    string,
  "sales":    string,
  "comments": string
}
RULES:
1. COLORS: ALLOWED_COLORS: {allowed_colors}. 
   - ONLY include colors that are actively selectable in the image. 
   - Ignore unavailable colors (they usually appear grayed out, faded, transparent, or crossed out). 
   - Map colors accurately to the closest allowed color (e.g., if you see "rose" or "light red", map it to "Qizil" or "Och qizil"). NEVER mix up colors or invent new ones.
2. SIZES: Extract only INTEGER EU sizes from 25 to 50. Ignore crossed-out or unavailable sizes. Deduplicate, sort ascending.
3. PRICE (priceYuan): Must be > 0. If you see multiple prices for different variants/images, ALWAYS pick the HIGHEST price. Ignore crossed-out old prices.
4. STATS (likes, views, sales, comments):
   - `likes` must be >= "1K" (e.g., "1.2K", "3K").
   - `sales` must be >= "1K" (e.g., "1K", "2.5K").
   - `views` must be GREATER THAN `likes` (e.g., "5K", "12K").
   - `comments` can be any realistic number (e.g., "540", "1.2K").
"""
AI_USER_PROMPT = "Extract all fields from this product variant image per the schema. Return JSON only."
AI_SYSTEM_PROMPT_RENDERED = AI_SYSTEM_PROMPT.replace("{allowed_colors}", ", ".join(c["uz"] for c in COLORS))


# =============================================================================
# 3. RUNTIME STATE (+ avtomatik tozalash)
# =============================================================================
class ProductAdd(StatesGroup):
    main_images = State()
    comment_images = State()
    variant_image = State()
    main_cover = State()


@dataclass
class AITask:
    task: asyncio.Task
    created: float = field(default_factory=time.monotonic)


bot = Bot(BOT_TOKEN or "0:invalid", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# user_id bo'yicha vaqtinchalik holat. Hammasi janitor (cleanup_once) tomonidan tozalanadi.
ai_tasks: dict[int, AITask] = {}
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


def drop_ai_task(user_id: int) -> None:
    entry = ai_tasks.pop(user_id, None)
    if entry and not entry.task.done():
        entry.task.cancel()


def drop_advance_task(user_id: int) -> None:
    item = advance_tasks.pop(user_id, None)
    if item and not item[1].done():
        item[1].cancel()
    last_photo_at.pop(user_id, None)


def reset_flow(user_id: int) -> None:
    drop_ai_task(user_id)
    drop_advance_task(user_id)


def start_ai_task(user_id: int, image_url: str) -> asyncio.Task:
    drop_ai_task(user_id)
    task = asyncio.create_task(analyze_variant(image_url), name=f"ai-{user_id}")
    task.add_done_callback(_consume_result)
    ai_tasks[user_id] = AITask(task)
    return task


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

    for uid, entry in list(ai_tasks.items()):
        if now - entry.created > AI_TASK_TTL:
            log.info("AI task muddati o'tdi, tozalandi (user=%s)", uid)
            drop_ai_task(uid)

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
    for uid in list(ai_tasks):
        drop_ai_task(uid)
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
    colors_uz = [c for c in (d.get("colors") or []) if isinstance(c, str) and c in COLOR_UZ_SET]
    colors_uz = list(dict.fromkeys(colors_uz))
    if not colors_uz:
        raise ValueError("Ranglar tayyor ro'yxatga mos kelmadi.")

    sizes = set()
    for s in d.get("sizes") or []:
        try:
            n = int(round(float(s)))
        except (TypeError, ValueError, OverflowError):
            continue
        if SIZE_MIN <= n <= SIZE_MAX:
            sizes.add(n)
    if not sizes:
        raise ValueError("O'lchamlar topilmadi.")

    try:
        price = float(d.get("priceYuan", 0))
    except (TypeError, ValueError):
        price = 0
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Narx topilmadi.")

    try:
        rating = round(float(d.get("rating", 4.9)), 1)
        if not math.isfinite(rating):
            rating = 4.9
    except (TypeError, ValueError):
        rating = 4.9

    return {
        "colors": colors_uz,
        "sizes": sorted(sizes),
        "priceYuan": price,
        "rating": rating,
        "likes": str(d.get("likes", "0")),
        "views": str(d.get("views", "0")),
        "sales": str(d.get("sales", "0")),
        "comments": str(d.get("comments", "0")),
    }


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


async def analyze_variant(image_url: str) -> dict:
    if not PROVIDERS:
        raise AIError("Hech qanday AI API kaliti sozlanmagan.")
    last_error = ""
    for provider in PROVIDERS:
        log.info("[AI] %s orqali tahlil qilinmoqda...", provider.name)
        try:
            result = await retry(
                partial(_ask_provider, provider, image_url),
                attempts=AI_ATTEMPTS, label=f"AI:{provider.name}", retry_wait=ai_retry_wait,
            )
            log.info("[AI] %s muvaffaqiyatli.", provider.name)
            return result
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


# =============================================================================
# 8. TEXTS & KEYBOARDS
# =============================================================================
SEP = "─" * 26
STAGE_TOTAL = 4

BTN_CHANGE = "🔄 Katalogni o'zgartirish"
BTN_CONTINUE = "➡️ Davom etish"
BTN_RETRY = "🔁 Qayta urinish"

ACCEPTED = "✅ <b>Rasm qabul qilindi!</b>"
PROMPT_MAIN = "🖼 <b>Asosiy rasmlarni</b> yuboring\n<i>Bir nechtasini birdaniga yuborishingiz mumkin.</i>"
PROMPT_COMMENT = "💬 <b>Komentariya rasmlarini</b> yuboring\n<i>Bir nechtasini birdaniga yuborishingiz mumkin.</i>"
PROMPT_VARIANT = "🎨 <b>Variantlar rasmini</b> yuboring\n<i>Rang, o'lcham va narx ko'rinadigan bitta rasm.</i>"
PROMPT_VARIANT_RETRY = "🎨 <b>Variantlar rasmini</b> qayta yuboring"
PROMPT_COVER = "🌟 Mahsulotning <b>asosiy (muqova) rasmini</b> yuboring"

STATE_PROMPTS = {
    ProductAdd.main_images.state: PROMPT_MAIN,
    ProductAdd.comment_images.state: PROMPT_COMMENT,
    ProductAdd.variant_image.state: PROMPT_VARIANT,
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


def _kb_reply(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def kb_process():
    return _kb_reply([[BTN_CHANGE]])


def kb_after_success():
    return _kb_reply([[BTN_CHANGE, BTN_CONTINUE]])


def kb_error_retry():
    return _kb_reply([[BTN_RETRY], [BTN_CHANGE]])


def _kb_inline(prefix: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{value}")] for value, label in options
    ])


def kb_quiz_season():
    return _kb_inline("qs", SEASONS)


def kb_quiz_gender():
    return _kb_inline("qg", GENDERS)


def kb_quiz_type(season, gender):
    return _kb_inline("qt", TYPES_BY_SG.get((season, gender), []))


def kb_quiz_place():
    return _kb_inline("qp", PLACES)


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


async def _auto_advance(user_id: int, action) -> None:
    """Debounce: oxirgi rasmdan PHOTO_PAUSE soniya o'tgach keyingi bosqichga o'tadi."""
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
            last_photo_at.pop(user_id, None)


def ensure_advance(user_id: int, stage_key: str, action) -> None:
    last_photo_at[user_id] = time.monotonic()
    cur = advance_tasks.get(user_id)
    if cur and cur[0] == stage_key and not cur[1].done():
        return
    task = asyncio.create_task(_auto_advance(user_id, action), name=f"advance-{user_id}-{stage_key}")
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


async def finish_quiz(message: Message, state: FSMContext, draft: dict):
    """Katalog tayyor: quiz xabarini o'chirib, 1-bosqichni boshlaymiz."""
    path = {k: draft.get(k) for k in ("season", "gender", "type", "place")}
    await state.set_state(ProductAdd.main_images)
    await state.update_data(catalog=path, catalog_draft=draft, main_images=[], comment_images=[],
                            variant_image=None, failed_row=None)
    await drop_msg(message)
    await reply(message, stage_text(None, path_label(path), 1, PROMPT_MAIN), reply_markup=kb_process())


# ---------- bosqichlar ----------
@dp.message(F.text == BTN_CONTINUE)
async def btn_continue(msg: Message, state: FSMContext):
    path = (await state.get_data()).get("catalog")
    if not path:
        return await send_start_quiz(msg, state)
    reset_flow(msg.from_user.id)
    await state.set_state(ProductAdd.main_images)
    await state.update_data(main_images=[], comment_images=[], variant_image=None, failed_row=None)
    await reply(msg, stage_text(None, path_label(path), 1, PROMPT_MAIN), reply_markup=kb_process())


def _stage_advancer(expect: State, required_key: str, nxt: State, stage: int, prompt: str):
    """Bosqich o'tkazgich yaratadi (go_comment_stage / go_variant_stage / go_cover_stage)."""
    async def go(chat_id: int, state: FSMContext) -> None:
        if await state.get_state() != expect.state:
            return
        if not (await state.get_data()).get(required_key):
            return
        await send(chat_id, stage_text(ACCEPTED, await catalog_name(state), stage, prompt))
        await state.set_state(nxt)
    return go


go_comment_stage = _stage_advancer(ProductAdd.main_images, "main_images", ProductAdd.comment_images, 2, PROMPT_COMMENT)
go_variant_stage = _stage_advancer(ProductAdd.comment_images, "comment_images", ProductAdd.variant_image, 3, PROMPT_VARIANT)
go_cover_stage = _stage_advancer(ProductAdd.variant_image, "variant_image", ProductAdd.main_cover, 4, PROMPT_COVER)


async def _collect_photo(msg: Message, state: FSMContext, expected: State, key: str, stage_key: str, go_next) -> None:
    uid = msg.from_user.id
    try:
        url = await save_photo(msg)
    except Exception as e:
        have = len((await state.get_data()).get(key) or [])
        return await reply_once(msg, "upload_err", upload_error_text(e, have))
    async with get_lock(uid):
        if await state.get_state() != expected.state:
            return
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
        return
    busy.add(uid)
    try:
        if (await state.get_data()).get("variant_image"):
            return
        try:
            url = await save_photo(msg)
        except Exception as e:
            return await reply_once(msg, "upload_err", upload_error_text(e, 0))
        if await state.get_state() != ProductAdd.variant_image.state:
            return
        await state.update_data(variant_image=url)
        start_ai_task(uid, url)   # AI tahlili fonda boshlanadi - foydalanuvchi keyingi rasmni yuborguncha tayyor bo'ladi
    finally:
        busy.discard(uid)
    ensure_advance(uid, "variant", partial(go_cover_stage, msg.chat.id, state))


@dp.message(ProductAdd.main_cover, F.photo)
async def msg_main_cover(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if msg.media_group_id:
        ignored_groups[uid] = (msg.media_group_id, time.monotonic())
    if uid in busy:
        return
    busy.add(uid)
    try:
        await _create_product(msg, state)
    finally:
        busy.discard(uid)


async def _back_to_variant(status: Message, state: FSMContext, reason: str) -> None:
    if await state.get_state() != ProductAdd.main_cover.state:
        return await drop_msg(status)
    await state.update_data(variant_image=None)
    await state.set_state(ProductAdd.variant_image)
    await show(status, stage_text(reason, await catalog_name(state), 3, PROMPT_VARIANT_RETRY))


async def _await_ai_result(uid: int, state: FSMContext) -> tuple[asyncio.Task | None, dict | None]:
    """
    AI task'ini topadi (yo'qolgan bo'lsa saqlangan variant rasmidan qayta ishga tushiradi) va natijasini kutadi.
    Task kutilayotganda ham ai_tasks'da turadi - shuning uchun "Katalogni o'zgartirish" uni bekor qila oladi.
    """
    entry = ai_tasks.get(uid)
    task = entry.task if entry else None
    if task is None:
        url = (await state.get_data()).get("variant_image")
        if not url:
            return None, None
        task = start_ai_task(uid, url)
    try:
        return task, await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise                      # bekor qilingan AI task emas, handlerning o'zi
        return task, None              # AI task bekor qilingan (masalan, katalog o'zgartirildi)
    finally:
        cur = ai_tasks.get(uid)
        if cur and cur.task is task:
            ai_tasks.pop(uid, None)


def build_comment_list(comment_images: list[str]) -> list[dict]:
    """REVIEWS_POOL dan 10 ta takrorlanmaydigan sharh; rasmlar faqat birinchi sharhga qo'shiladi."""
    comment_list = []
    for c in random.sample(REVIEWS_POOL, 10):
        # Matn kirill alifbosida bo'lsa - ruscha, aks holda o'zbekcha deb olinadi.
        is_russian = any("\u0400" <= char <= "\u04FF" for char in c)
        text_uz = c if not is_russian else "Sifati zo'r ekan, tavsiya qilaman."
        text_ru = c if is_russian else "Отличное качество, рекомендую."
        comment_list.append({
            "userId": "ID:" + str(random.randint(100000, 999999)),
            "text": text_uz,
            "textRu": text_ru,
            "rating": random.choice([4, 5, 5, 5, 5]),  # ko'pincha 5, ba'zida 4
            "images": comment_images if len(comment_list) == 0 else [],
        })
    return comment_list


def build_row(ai_result: dict, path: dict, cover_url: str, data: dict) -> dict:
    images = [cover_url] + ordered_urls(data.get("main_images"))
    comment_list = build_comment_list(ordered_urls(data.get("comment_images")))
    return {
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


async def _create_product(msg: Message, state: FSMContext) -> None:
    uid = msg.from_user.id
    try:
        cover_url = await save_photo(msg)
    except Exception as e:
        accepted = len((await state.get_data()).get("main_images") or [])
        await reply(msg, upload_error_text(e, accepted))
        return

    status = await reply(msg, "⏳ <b>Mahsulot yaratilmoqda...</b>\n🤖 AI ma'lumotlarni tayyorlamoqda, biroz kuting.")

    try:
        ai_task, ai_result = await _await_ai_result(uid, state)
    except Exception as e:
        return await _back_to_variant(status, state, f"❌ <b>AI xatosi:</b> {err(e)}")
    if ai_task is None:
        return await _back_to_variant(status, state, "❌ <b>AI natijasi topilmadi.</b>")
    if ai_result is None:
        return await drop_msg(status)  # AI task bekor qilingan - jimgina chiqamiz

    data = await state.get_data()
    path = data.get("catalog")
    if not path or await state.get_state() != ProductAdd.main_cover.state:
        return await drop_msg(status)

    row = build_row(ai_result, path, cover_url, data)
    try:
        res = await save_product(row)
    except Exception as e:
        await drop_msg(status)
        await state.update_data(failed_row=row)
        return await reply(msg, save_error_text(e), reply_markup=kb_error_retry())

    final = success_text(path, ai_result["priceYuan"], ai_result["colors"], ai_result["sizes"], res.get("product_id"))
    await drop_msg(status)
    if await state.get_state() == ProductAdd.main_cover.state:
        await state.set_state(None)
        await state.update_data(main_images=[], comment_images=[], variant_image=None, failed_row=None)
        await reply(msg, final, reply_markup=kb_after_success())
    else:
        await reply(msg, final)


@dp.message(F.text == BTN_RETRY)
async def btn_retry(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    data = await state.get_data()
    row = data.get("failed_row")
    if not row:
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
        await state.update_data(failed_row=None, main_images=[], comment_images=[], variant_image=None)
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
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        shutdown_user_tasks()
        if runner:
            await runner.cleanup()
        if _session and not _session.closed:
            await _session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
