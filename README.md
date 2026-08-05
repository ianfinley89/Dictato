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

# 3. Import USDA household measures (~13MB download, once)
uv run python scripts/import_usda_reference.py --download

# 4. Run
uv run uvicorn app.main:app --reload
```

Step 3 is optional but strongly recommended: it fills a local table with USDA's
published gram weights ("1 cup 158g", "1 cheeseburger 210g") for 15k foods, so a
food is anchored the moment it is cached instead of waiting on an API call.
Without it, portion anchoring drops from 88% of cached foods to 48% and counts
like "two eggs" have no gram weight to multiply. Safe to re-run.

The first run downloads the local Whisper speech-to-text model (~500MB for
`small`) — voice logging is transcribed on your own machine, no cloud STT.

Open http://localhost:8000 — register an account and start logging.

## Environment variables (`.env`)

**Core**

| Variable | Description |
|---|---|
| `USDA_FOOD_DATA_API_KEY` | Free key from api.data.gov |
| `ANTHROPIC_API_KEY` | Default model provider; also the nutrition web-lookup |
| `SECRET_KEY` | Long random string for session signing |
| `DATABASE_PATH` | SQLite file path (default: `data/dictato.db`) |
| `AI_DAILY_LIMIT` | Max AI agent sessions per user per day (default: 20) |
| `SECURE_COOKIES` | Set `true` when behind HTTPS |
| `ADMIN_EMAILS` | Comma-separated emails that can see the Admin pane |
| `WHISPER_MODEL` | Local STT size: tiny/base/small/medium (default: small) |
| `WHISPER_WARMUP` | Load the STT model at startup (default: true) |
| `CLARIFY_THRESHOLD` | Below this deterministic score a capture offers "say more" (default 0.5; ~0.1 = only failed captures, 0.7+ = noisy) |

**Model routing** (see below) — `VOICE_MODEL`, `PHOTO_MODEL`, `COACH_MODEL`,
`TRIAGE_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `LOCAL_BASE_URL`,
`LOCAL_API_KEY`. Legacy globals `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL` / `AGENT_MODEL` still work.

**Food sources / push** — `FATSECRET_CLIENT_ID`, `FATSECRET_CLIENT_SECRET`,
`FATSECRET_TTL_HOURS`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT`.

## Run tests

```bash
uv run pytest tests/ -q
```

## Swapping the LLM backend

The coach and the voice/photo agent run through one interface
([app/services/llm.py](app/services/llm.py)). Routing is **per feature**, so each
can use a different model — the photo path runs a cheap vision model while text
stays on Claude:

```bash
OPENROUTER_API_KEY=sk-or-...
PHOTO_MODEL=openrouter:google/gemini-3.1-flash-lite
# VOICE_MODEL=openrouter:qwen/qwen3.5-flash-02-23
# COACH_MODEL=openrouter:deepseek/deepseek-chat
# LOCAL_BASE_URL=http://localhost:11434/v1   # Ollama; LOCAL_API_KEY=ollama
```

Each value is `provider:model` (`anthropic` | `openrouter` | `local`) or a bare
model id. Unset falls back to the global default, **except `photo`**, which
defaults to Anthropic Haiku so vision never breaks when text features are pointed
at a text-only model.

Caveats learned by measuring, not guessing:

- **The agent is tool-driven** — the model must support tool calling. Gemini's
  `-image` variants do *not* (they're image-generation) and fail outright.
- **`web_search` works on every provider**, under that one name, but is backed
  differently: Anthropic's server tool, or OpenRouter's `openrouter:web_search`
  (billed by OpenRouter at ~$4/1000 results — capped here at 5 per search, 10
  per capture). An endpoint with neither, such as Ollama or vLLM, falls back to
  a client-side tool that calls the Anthropic-backed lookup in `ai.py`, so a
  purely local setup still needs a little Anthropic credit for this one thing.
  Before this, non-Anthropic providers had no web tool at all and the model's
  only remaining option was to invent a labelled estimate — that is exactly how
  one cached "multigrain cereal flakes" came to exist.
- **Cheap ≠ equivalent.** On the 41-dish Menu-Match eval, `gpt-oss-120b` (27×
  cheaper) logged one pizza order as three entries — 513% calorie error — and
  `qwen3.5-flash` matched Claude on mean error but invented nutrition 8× where
  Claude invented none. Re-run `scripts/eval_menumatch.py --tag <name> --model
  <id>` before switching anything.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Running commit + process start time — **the only proof a deploy actually took effect** (see Deployment) |
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Login |
| POST | `/api/auth/logout` | Logout |
| GET | `/api/auth/me` | Current user |
| DELETE | `/api/auth/account` | Delete account + all data (requires password in body) |
| GET | `/api/foods/search?q=` | Search foods |
| GET | `/api/foods/{id}` | Get food by id |
| GET | `/api/log/today?date=&tz_offset=` | A local day's entries (date defaults to today) |
| POST | `/api/log/` | Log an entry |
| DELETE | `/api/log/{id}` | Delete an entry |
| GET | `/api/log/summary?days=&tz_offset=` | Per-day calorie/macro totals (1–90 days) |
| GET | `/api/log/range?start=&end=` | Entries across a date range |
| GET/POST | `/api/log/water` | Water glasses for a local day |
| GET | `/api/log/{id}/portions` | Real portion choices for an entry ("1 spear 30g", "1 can (12 fl oz) 354g", "your usual") |
| PUT | `/api/log/{id}/portion` | Apply a chosen portion; marks it user-set so future logs of that food stop being guessed |
| POST | `/api/agent/log` | Voice/photo/text logging agent (multipart `audio`, `image`, or `text`) — transcribes, grounds each item in the food DB, and logs it |
| DELETE | `/api/agent/capture/{id}` | Discard a whole capture and everything it logged |
| GET | `/api/agent/usage` | Today's AI usage + daily limit |
| PUT | `/api/foods/{id}` | Correct an AI-created food (name/brand/macros); shared USDA/OFF rows are not editable |
| POST | `/api/foods/weblookup` | AI web lookup for a food no database has |
| GET/POST | `/api/auth/weight` | Weigh-in history / add one (mostly captured passively from speech) |
| DELETE | `/api/auth/weight/{id}` | Remove a weigh-in |
| GET | `/api/coach/history` | Coach chat history + accumulated profile |
| POST | `/api/coach/chat` | Ask the coach (reads your logs/notes/goals/profile; remembers facts) |
| GET | `/api/admin/stats?days=` | Usage/eval aggregates (ADMIN_EMAILS only) |
| GET | `/api/admin/failures?days=` | Captures that logged nothing, with transcripts (ADMIN_EMAILS only) |
| GET | `/api/admin/traces` | Per-model-call traces: latency, tokens, response, errors (admin) |
| GET | `/api/admin/issues` · `/errors` | User issue reports · unhandled server errors (admin) |
| POST | `/api/issues/` | File an issue report (any signed-in user; context auto-attached) |
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
2. **Allow-list your server's outbound IP** in the FatSecret console — calls from
   unregistered IPs are rejected with `error 21` (returned as HTTP **200** with an
   error body, which is why it can fail silently). A dynamic home IP does **not**
   need a static address: the PREMIER/PREMIER-Free tiers accept **CIDR ranges**, so
   allow-listing your ISP's block survives reassignment. Admin → *Food source
   health* shows the current state and the exact IP FatSecret saw.
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
- **Phase 3** ✅ Photo entry (vision model; Gemini 3.1 flash-lite in production —
  it beat Haiku 2.5× on ingredient recall at ~4× lower cost)
- **Phase 4** ✅ Dashboard / weekly charts + goals
- **Phase 5** ✅ Push notifications + meal reminders
- **Phase 8** ✅ Recipes, custom foods & favorites (user-defined foods)
- **Phase 9** ✅ Agentic logging: one tool loop grounds each item in the food DB
  (cache → USDA → OFF → FatSecret → web), decomposes homemade meals, auto-logs
  with per-entry Undo/Adjust, and labels every entry's data source
- **Phase 6** Friend sharing
- **Phase 7** Micronutrient depth

## Deployment (home PC + Cloudflare Tunnel)

Permanent HTTPS URL (**https://dictato.levelup-ai.com**) from the home PC, no
open ports, home IP hidden — via a named Cloudflare Tunnel. Full step-by-step
(DNS move, tunnel setup, auto-start on reboot, and how to reverse it) in
**[HOSTING.md](HOSTING.md)**; the tunnel config template is
[deploy/cloudflared/config.example.yml](deploy/cloudflared/config.example.yml).

Short version:

1. Move `levelup-ai.com`'s **nameservers** (not the registration) to Cloudflare.
2. `cloudflared tunnel create dictato`, add the config, then
   `cloudflared tunnel route dns dictato dictato.levelup-ai.com`.
3. Install cloudflared + the app as services so they survive reboots.
4. Set `SECURE_COOKIES=true` in `.env`.

### Verifying a deploy

**HTTP 200 does not mean your code is live.** `Stop-ScheduledTask` kills the
wrapper but not the uvicorn process it spawned, so a new instance can fail to bind
port 8000, exit, and leave the old build serving — while still answering 200,
still serving fresh static files (they're read from disk per request), and still
applying DB migrations. That combination hid a ten-day-old build in production.

Always check the commit instead:

```bash
curl -s http://127.0.0.1:8000/api/health   # {"commit": "...", "started_at": "..."}
git rev-parse --short HEAD                 # must match
```

`deploy/run-dictato.ps1` now claims port 8000 before starting, so the stale
process is stopped rather than winning. If it ever survives with *Access is
denied*, it needs an elevated shell — the task runs S4U.

## Scripts

```bash
uv run python scripts/import_usda_reference.py  # USDA household measures + recipe composition
uv run python scripts/gen_vapid.py              # generate Web Push keys
uv run python scripts/reset_password.py <email> <pw>
uv run python scripts/export_dataset.py         # JSONL training examples from captures
uv run python scripts/refetch_usda_nutrition.py  # re-read cached USDA foods (dry run; --apply to write)
uv run python scripts/repair_entry_snapshots.py # re-freeze entry nutrition after a food row is corrected
```

Evaluation harnesses (all write to `data/evals/`, never the live DB; each takes
`--tag <name>` and `--compare` so variants can be A/B'd):

```bash
uv run python scripts/eval_menumatch.py --tag base       # 41 restaurant dishes, dietitian calories
uv run python scripts/eval_grounding.py  --tag base      # Nutrition5k: does DB grounding beat a raw guess?
uv run python scripts/eval_photo.py      --tag base      # vision-model A/B on Nutrition5k images
uv run python scripts/calibrate_confidence.py            # is the confidence flag honest? (it wasn't)
uv run python scripts/backtest_portion_prior.py          # would the personal portion prior have fired?
uv run python scripts/eval_search_ranking.py             # does a plain food name return a number you could eat?
uv run python scripts/eval_stt_guard.py                  # replay saved voice notes: is silence caught, is real speech kept?
```

Read the docstrings before quoting numbers — several record what they
**cannot** measure. In particular, captures the user merely *accepted* are not
ground truth, so models must never be scored against them.
