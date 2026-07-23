# Bol Monitor

Stock and new-product alerts for [bol.com](https://www.bol.com/nl/nl/) (Netherlands / Belgium).

The bot watches bol.com via **sitemap discovery** and **product page HTML scraping** (not Target Redsky). When a product matches one of your profiles, you get:

1. **NEW ONLINE** — page is live (may still be out of stock)
2. **IN STOCK** — add-to-cart / op voorraad

Each alert includes **product name**, **link**, and **price**. Discord is the default channel; Telegram is optional.

---

## Features

- Product profiles: title keywords, category keywords, exclude keywords, min/max price
- Sitemap scan every 5–15 minutes
- Product page visits every 5–15 seconds (random, configurable)
- Discord + optional Telegram alerts
- Residential proxy pool with rotation on block
- Bol login from the **Settings** UI (Chromium opens on your PC; cookies save to Neon)
- Runs on your PC or a VPS (Render + Vercel)

---

## Requirements

- Python 3.11+ (recommended)
- Node.js 18+
- Neon PostgreSQL (same `DATABASE_URL` on PC and server for session sync)
- Windows recommended for the Bol login browser (Playwright Chromium)

---

## Quick start (local)

### 1. Backend

```bat
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python scripts\ensure_playwright_chromium.py
copy .env.example .env
```

Edit `backend/.env`:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
SECRET_KEY=change-me-to-a-long-random-string
CORS_ORIGINS=http://localhost:5175,http://127.0.0.1:5175
ADMIN_EMAIL=you@email.com
ADMIN_PASSWORD=your-password
```

Start API:

```bat
python run.py
```

API: `http://127.0.0.1:8003`  
Health: `http://127.0.0.1:8003/health`

### 2. Frontend

```bat
cd frontend
npm install
npm run dev
```

Open: `http://localhost:5175`

Or use `setup.bat` / `startall.bat` from the repo root if present.

### 3. First login to bol.com

1. Open the dashboard → **Settings**
2. Click **Login to Bol**
3. Chromium opens — sign in until the header shows **Welkom**
4. Cookies save to Neon automatically
5. Dashboard → **Start monitoring**

Chromium is installed by `setup.bat` (no separate install script).

---

## How monitoring works

```
Start monitoring
    → Sitemap scan (find new bol.com product URLs)
    → Match against your product profiles
    → Log + track matching products
    → Alert 1: NEW ONLINE (title, URL, price)
    → Visit product pages every 5–15s
    → Alert 2: IN STOCK when stock appears
```

### Product profiles

| Fields set | Match rule |
|------------|------------|
| Category only | Category / breadcrumb must match |
| Title only | Title must match |
| Title + category | **Both** must match |
| Min / max price | Applied when price is present |
| Exclude keywords | Products with those words are skipped |

First run with an empty sitemap database can pick up currently listed matching products. Later scans only pick up **new** sitemap URLs.

---

## Proxies

Dashboard → **Proxies**:

1. Enable **Use proxies**
2. One line per proxy: `host:port:username:password`
3. Save (and optionally Test)

Proxies are used for sitemap + product page fetches, and for the login browser when enabled.

---

## Alerts

**Settings:**

- Discord webhook (default) — NEW ONLINE + IN STOCK
- Optional Telegram bot token + chat ID

Alert payload: product name, bol.com link, price, profile name.

---

## Deploy

### Backend (Render)

| Setting | Value |
|---------|--------|
| Root Directory | `backend` |
| Build | `pip install -r requirements.txt && python scripts/ensure_playwright_chromium.py` |
| Start | `python run.py` |

Environment variables:

```
DATABASE_URL=...
SECRET_KEY=...
CORS_ORIGINS=https://YOUR-FRONTEND.vercel.app
ADMIN_EMAIL=...
ADMIN_PASSWORD=...
RENDER=true
PLAYWRIGHT_HEADLESS=true
DISCORD_WEBHOOK_URL=...   (optional)
TELEGRAM_BOT_TOKEN=...    (optional)
TELEGRAM_CHAT_ID=...      (optional)
PYTHON_VERSION=3.11.0
```

Bol **login must be done on your PC** (Settings → Login to Bol) while the local backend uses the **same Neon `DATABASE_URL`**. Render then reuses that session.

### Frontend (Vercel)

| Setting | Value |
|---------|--------|
| Root Directory | `frontend` |
| Build | `npm run build` |
| Output | `dist` |

Environment:

```
VITE_API_URL=https://YOUR-RENDER-SERVICE.onrender.com/api
```

(` /api` at the end is required.)

---

## Project layout

```
backend/          FastAPI + monitor + Playwright session
frontend/         React dashboard
setup.bat         Local setup (Python, npm, Chromium)
startall.bat      Start backend + frontend
```

Folders such as `target/`, `lazada/`, and `amazon-mx/` are **not** part of this bot and are gitignored.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Login 401 on live | Set `ADMIN_EMAIL` / `ADMIN_PASSWORD` on Render; redeploy |
| Frontend 404 on `/auth/login` | Set `VITE_API_URL=.../api` and redeploy Vercel |
| IP blocked | Enable proxies; Login to Bol again if needed |
| Session expired | Settings → Clear Bol session → Login to Bol |
| Monitor won’t start | Need a valid Bol session in Neon |

---

## Notes

- Site focus: `https://www.bol.com/nl/nl`
- Scraping uses bol.com **HTML + embedded JSON**, not Redsky
- Keep `SECRET_KEY` and `.env` private — never commit them
