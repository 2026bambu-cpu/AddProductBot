# Poyabzal Telegram Bot

Admin uchun Telegram bot: rasmlarni yuborasiz, AI (Gemini → Groq → Mistral fallback) rang, o'lcham va narxni o'qiydi, mahsulot Supabase bazasiga qo'shiladi. Rasmlar Supabase Storage'dagi `products` bucket'ida saqlanadi.

## Fayllar

- `main.py` — bot kodi
- `requirements.txt` — kutubxonalar
- `.env.example` — o'zgaruvchilar namunasi

## Lokal ishga tushirish

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # va qiymatlarni to'ldiring
python main.py
```

## Environment variables

`.env` faylida (Render'da **Environment** bo'limida) quyidagilar bo'lishi kerak:

| O'zgaruvchi | Tavsif |
|---|---|
| `BOT_TOKEN` | Telegram bot tokeni |
| `ADMIN_IDS` | Admin Telegram ID'lari, vergul bilan ajratilgan |
| `SUPABASE_URL` | Supabase loyiha manzili (`https://xxxx.supabase.co`) |
| `SUPABASE_KEY` | Supabase API kaliti (RPC chaqirish uchun) |
| `BOT_SECRET` | `app_bot_add_product` funksiyasida tekshiriladigan maxfiy qiymat |
| `STORAGE_URL` | Supabase Storage manzili. Odatda `SUPABASE_URL` bilan bir xil |
| `STORAGE_KEY` | Storage'ga yuklash uchun kalit (pastga qarang) |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `MISTRAL_API_KEY` | Kamida bittasi kerak |
| `GEMINI_MODEL` / `GROQ_MODEL` / `MISTRAL_MODEL` | Ixtiyoriy, model nomini almashtirish uchun |

> **Eslatma:** eski `IMGBB_API_KEY` o'zgaruvchisi endi ishlatilmaydi. Uni `.env` va Render sozlamalaridan o'chirib, o'rniga `STORAGE_URL` va `STORAGE_KEY` ni kiriting. Ular sozlanmasa bot ishga tushmaydi.

## Supabase Storage sozlash

1. Supabase Dashboard → **Storage** bo'limiga kiring.
2. **New bucket** ni bosib, nomini aynan `products` deb yozing.
3. **Public bucket** opsiyasini yoqing (aks holda rasm havolalari ochilmaydi).
4. Bot rasmni `POST {STORAGE_URL}/storage/v1/object/products/<uuid>.jpg` orqali yuklaydi va ommaviy havolani qaytaradi: `{STORAGE_URL}/storage/v1/object/public/products/<uuid>.jpg`.
5. `STORAGE_KEY` sifatida bucket'ga yozish huquqiga ega kalitni bering. Odatda `service_role` kaliti ishlatiladi (u RLS'ni chetlab o'tadi). Bu kalit **maxfiy** — uni faqat serverda (Render Environment) saqlang, kodga yoki GitHub'ga yozmang. Agar `anon` kalit ishlatsangiz, `products` bucket uchun `INSERT` ruxsat beruvchi Storage policy yaratishingiz kerak.

## Render.com ga deploy

1. Kodni GitHub'ga yuklang (`.env` ni **yuklamang**, `.gitignore` ga qo'shing).
2. Render'da **New +** ni bosing va tur tanlang:
   - **Background Worker** (tavsiya) — polling bot uchun eng mos, lekin pullik.
   - **Web Service** — bepul reja bor. Bot `PORT` ni avtomatik taniydi va kichik health-server ochadi.
3. Sozlamalar:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
4. **Environment** bo'limiga yuqoridagi o'zgaruvchilarni kiriting. Python versiyasi uchun `PYTHON_VERSION=3.11.9` qo'shing.
5. **Deploy** ni bosing. Loglarda xatolik bo'lmasa, botga Telegram'da `/start` yuboring.

### Muhim eslatmalar

- Bepul Web Service ~15 daqiqa so'rov bo'lmasa uxlab qoladi va bot to'xtaydi. Uzluksiz ishlashi kerak bo'lsa Background Worker oling yoki servisni tashqi ping (masalan UptimeRobot) bilan uyg'oq tuting.
- Bir vaqtda faqat **bitta** nusxa ishlashi kerak (aks holda Telegram `Conflict` xatosi chiqadi). Lokal botni to'xtatib keyin deploy qiling.
- Faqat `ADMIN_IDS` dagi foydalanuvchilar botdan foydalana oladi.
- AI model nomlari tez eskiradi. Model xatosi chiqsa, `GEMINI_MODEL`, `GROQ_MODEL`, `MISTRAL_MODEL` o'zgaruvchilari orqali yangisini yozing.

## Supabase

Bot `rpc/app_bot_add_product` funksiyasini chaqiradi (`p_secret`, `p_row` parametrlari). `BOT_SECRET` shu funksiya ichida tekshiriladigan qiymat bilan bir xil bo'lishi kerak.
