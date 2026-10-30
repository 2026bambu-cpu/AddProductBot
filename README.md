# Poyabzal Telegram Bot

Admin uchun Telegram bot: rasmlarni yuborasiz, AI (Gemini → Groq → Mistral fallback) rang, o'lcham va narxni o'qiydi, mahsulot Supabase bazasiga qo'shiladi.

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

## Render.com ga deploy

1. Kodni GitHub'ga yuklang (`.env` ni **yuklamang**, `.gitignore` ga qo'shing).
2. Render'da **New +** ni bosing va tur tanlang:
   - **Background Worker** (tavsiya) — polling bot uchun eng mos, lekin pullik.
   - **Web Service** — bepul reja bor. Bot `PORT` ni avtomatik taniydi va kichik health-server ochadi.
3. Sozlamalar:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
4. **Environment** bo'limiga `.env.example` dagi o'zgaruvchilarni kiriting. Python versiyasi uchun `PYTHON_VERSION=3.11.9` qo'shing.
5. **Deploy** ni bosing. Loglarda xatolik bo'lmasa, botga Telegram'da `/start` yuboring.

### Muhim eslatmalar

- Bepul Web Service ~15 daqiqa so'rov bo'lmasa uxlab qoladi va bot to'xtaydi. Uzluksiz ishlashi kerak bo'lsa Background Worker oling yoki servisni tashqi ping (masalan UptimeRobot) bilan uyg'oq tuting.
- Bir vaqtda faqat **bitta** nusxa ishlashi kerak (aks holda Telegram `Conflict` xatosi chiqadi). Lokal botni to'xtatib keyin deploy qiling.
- Faqat `ADMIN_IDS` dagi foydalanuvchilar botdan foydalana oladi.
- AI model nomlari tez eskiradi. Model xatosi chiqsa, `GEMINI_MODEL`, `GROQ_MODEL`, `MISTRAL_MODEL` o'zgaruvchilari orqali yangisini yozing.

## Supabase

Bot `rpc/app_bot_add_product` funksiyasini chaqiradi (`p_secret`, `p_row` parametrlari). `BOT_SECRET` shu funksiya ichida tekshiriladigan qiymat bilan bir xil bo'lishi kerak.
