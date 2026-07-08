# Dictato

Fast calorie and macro tracker — voice, photo, or manual entry. Self-hosted PWA.

## Quick start

```bash
# 1. Install dependencies (uv manages the venv from pyproject.toml)
uv sync

# 2. Configure environment
copy .env.example .env      # Windows
# cp .env.example .env      # Mac/Linux
# Then edit .env with your API keys and a random SECRET_KEY

# 3. Run
uv run uvicorn app.main:app --reload
```

The first run downloads the local Whisper speech-to-text model (~500MB for
`small`) — voice logging is transcribed on your own machine, no cloud STT.

Open http://localhost:8000 — register an account and start logging.

## Environment variables (`.env`)

| Variable | Description |
|---|---|
| `USDA_FOOD_DATA_API_KEY` | Free key from api.data.gov |
| `ANTHROPIC_API_KEY` | For the voice/photo logging agent |
| `SECRET_KEY` | Long random string for session signing |
| `DATABASE_PATH` | SQLite file path (default: `data/dictato.db`) |
| `AI_DAILY_LIMIT` | Max AI agent sessions per user per day (default: 20) |
| `SECURE_COOKIES` | Set `true` when behind HTTPS |
| `WHISPER_MODEL` | Local STT model size: tiny/base/small/medium (default: small) |
| `WHISPER_WARMUP` | Load the STT model at startup (default: true) |

## Run tests

```bash
pytest tests/ -v
```

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Login |
| POST | `/api/auth/logout` | Logout |
| GET | `/api/auth/me` | Current user |
| GET | `/api/foods/search?q=` | Search foods |
| GET | `/api/foods/{id}` | Get food by id |
| GET | `/api/log/today?date=&tz_offset=` | A local day's entries (date defaults to today) |
| POST | `/api/log/` | Log an entry |
| DELETE | `/api/log/{id}` | Delete an entry |
| GET | `/api/log/summary?days=&tz_offset=` | Per-day calorie/macro totals (1–90 days) |
| POST | `/api/agent/log` | Voice/photo/text logging agent (multipart `audio`, `image`, or `text`) — transcribes, grounds each item in the food DB, and logs it |
| GET | `/api/agent/usage` | Today's AI usage + daily limit |
| PUT | `/api/auth/goals` | Set calorie + macro goals |
| GET | `/api/push/vapid-key` | Public VAPID key for the browser |
| POST | `/api/push/subscribe` · `/unsubscribe` · `/test` | Manage/ test web-push subscriptions |
| GET/POST | `/api/reminders/` | List / add reminder times |
| PUT/DELETE | `/api/reminders/{id}` | Toggle / remove a reminder |
| POST | `/api/recipes/` | Create a recipe (ingredients) or custom food (macros) |
| GET/DELETE | `/api/recipes/{id}` | Recipe detail / delete (unless logged) |
| GET | `/api/foods/quick` | Favorites + recents for one-tap logging |
| GET | `/api/foods/mine` | Your saved recipes & custom foods |
| POST/DELETE | `/api/foods/{id}/favorite` | Star / unstar a food |

## FatSecret food source

A fallback DB lookup (rich in branded/restaurant foods), tried after USDA + Open
Food Facts and before the AI web lookup.

1. Create an app at [platform.fatsecret.com](https://platform.fatsecret.com), put the
   client ID + secret in `.env` (`FATSECRET_CLIENT_ID`, `FATSECRET_CLIENT_SECRET`).
2. **Allow-list your server's outbound IP** in the FatSecret console — the Basic tier
   rejects calls from unregistered IPs (`error 21`). From a home PC this is your
   public IP; update it if it changes.
3. Per the license, cached FatSecret results are **purged after 24h**
   (`FATSECRET_TTL_HOURS`). Foods you actually logged keep their snapshot — that's
   your own diary record.

## Web Push (Phase 5)

1. Generate a VAPID keypair: `uv run python scripts/gen_vapid.py` and paste the
   three lines into `.env`.
2. Push requires a **secure context** — works on `http://localhost`, otherwise needs
   HTTPS (the Cloudflare Tunnel below).
3. **iOS/iPadOS:** web push only works after the user does *Add to Home Screen* and
   opens the app from that icon.
4. Set reminder times in the dashboard → **Reminders**. A background scheduler fires
   "have you eaten?" prompts at those local times.

## Build phases

See [BUILD_PLAN.md](BUILD_PLAN.md).

- **Phase 1** ✅ Manual tracking
- **Phase 2** ✅ Voice entry (local Whisper STT)
- **Phase 3** ✅ Photo entry (Haiku vision)
- **Phase 4** ✅ Dashboard / weekly charts + goals
- **Phase 5** ✅ Push notifications + meal reminders
- **Phase 8** ✅ Recipes, custom foods & favorites (user-defined foods)
- **Phase 9** ✅ Agentic logging: one tool loop grounds each item in the food DB
  (cache → USDA → OFF → FatSecret → web), decomposes homemade meals, auto-logs
  with per-entry Undo/Adjust, and labels every entry's data source
- **Phase 6** Friend sharing
- **Phase 7** Micronutrient depth

## Deployment (home PC + Cloudflare Tunnel)

1. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
2. `cloudflared tunnel create dictato`
3. Point the tunnel to `localhost:8000`.
4. Set `SECURE_COOKIES=true` in `.env` and restart uvicorn.
