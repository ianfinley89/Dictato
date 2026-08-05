"""Screenshot the PWA's real UI states, so a layout claim can be looked at.

Two UI changes shipped without anyone seeing them rendered: the result card got
squeezed when a photo was attached, and the mic level meter. Both were reasoned
about from CSS and handed to the user to eyeball. This closes that.

It drives the ACTUAL page — real index.html, real style.css, real app.js render
functions called with controlled data — rather than a mock, because a mock only
proves the mock is fine. `showResultCard` and `openConfirm` are globals (app.js
is a classic script), so a state can be rendered without spending an API call to
produce a capture.

Runs its own uvicorn on a scratch database and a spare port. It NEVER touches
the live DB or the production server, and creates no users in production.

    uv run --with playwright python scripts/screenshot_ui.py
    uv run --with playwright python -m playwright install chromium   # once

Writes PNGs to data/evals/ui/.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, ".")

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
OUT_DIR = os.path.join("data", "evals", "ui")
DB = os.path.join("data", "evals", "ui_scratch.db")
# iPhone-ish. The app is capped at 508px, so phone width is the case that
# actually goes wrong; desktop just adds margin.
VIEWPORT = {"width": 390, "height": 900}

ENTRIES = [
    {"id": 1, "food_name": "Egg, Whole, Cooked, Scrambled", "food_brand": None,
     "quantity_g": 142, "calories": 212, "food_source": "USDA",
     "food_source_raw": "usda", "serving_g": 61, "serving_desc": "1 large",
     "portion_basis": "count", "portion_confidence": "high"},
    {"id": 2, "food_name": "Bibimbap, Korean mixed rice bowl with vegetables",
     "food_brand": None, "quantity_g": 450, "calories": 612,
     "food_source": "AI estimate", "food_source_raw": "estimate",
     "serving_g": None, "serving_desc": None,
     "portion_basis": "estimate", "portion_confidence": "low"},
    {"id": 3, "food_name": "Coffee, Brewed", "food_brand": None,
     "quantity_g": 240, "calories": 2, "food_source": "USDA",
     "food_source_raw": "usda", "serving_g": 240, "serving_desc": "1 cup",
     "portion_basis": "history", "portion_confidence": "high"},
]
# 1x1 JPEG, enough for the polaroid to take its real width and float.
PHOTO = ("data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJ"
         "CQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
         "wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAA"
         "AAD/2gAIAQEAAD8AKp//2Q==")


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server():
    for suffix in ("", "-wal", "-shm"):
        p = DB + suffix
        if os.path.exists(p):
            os.remove(p)
    env = {**os.environ, "DATABASE_PATH": DB, "WHISPER_WARMUP": "false",
           "SECURE_COOKIES": "false", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT),
         "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/api/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("server did not come up")


def main() -> None:
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT_DIR, exist_ok=True)
    proc = start_server()
    shots = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)

            page.goto(BASE, wait_until="networkidle")
            page.evaluate("""async () => {
                await fetch('/api/auth/register', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: 'shot@example.com',
                                          password: 'password123',
                                          display_name: 'Shot'}),
                });
            }""")
            page.reload(wait_until="networkidle")

            def shot(name: str, script: str):
                page.evaluate(script)
                page.wait_for_timeout(400)
                path = os.path.join(OUT_DIR, f"{name}.png")
                page.screenshot(path=path, full_page=True)
                shots.append(path)
                print(f"  wrote {path}")

            payload = {"capture_id": 1, "transcript":
                       "two scrambled eggs, a bowl of bibimbap and a coffee",
                       "summary": "Logged three items from your photo.",
                       "entries": ENTRIES, "annotation": {}, "fast_path": False,
                       "confidence": {"clarify": False}}
            import json as _json
            blob = _json.dumps(payload)

            shot("result_card_with_photo",
                 f"() => showResultCard({blob}, '{PHOTO}')")
            shot("result_card_no_photo",
                 f"() => showResultCard({blob}, '')")

            # A screenshot is only worth taking if something also READS it.
            # "1 large" (egg) pluralised to "2.3 larges" until a rendered card
            # was actually looked at.
            page.evaluate(f"() => showResultCard({blob}, '')")
            page.wait_for_timeout(300)
            metas = page.eval_on_selector_all(
                ".result-entry-meta", "els => els.map(e => e.textContent)")
            bad = [t for t in metas
                   if any(w in t for w in ("larges", "mediums", "smalls", "extras"))]
            print(f"  serving equivalents read naturally: {not bad}"
                  + (f"  BAD: {bad}" if bad else ""))
            for t in metas:
                print(f"      {t.strip()[:72]}")

            # The Adjust flow, driven for real. Setting _adjustingEntryId from
            # here would not work — app.js is a classic script and its top-level
            # `let` bindings are not properties of window, so an assignment
            # creates a DIFFERENT variable and the panel renders as a fresh log.
            # Click the actual button instead, which is the better test anyway.
            real = page.evaluate("""async () => {
                const f = await (await fetch('/api/recipes/', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Bibimbap, Korean', calories: 136,
                                          protein_g: 6, carbs_g: 18, fat_g: 4}),
                })).json();
                const e = await (await fetch('/api/log/', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({food_id: f.id, quantity_g: 450}),
                })).json();
                return {food_id: f.id, entry_id: e.id};
            }""")
            live = dict(ENTRIES[1], id=real["entry_id"], food_id=real["food_id"])
            payload_live = _json.dumps(dict(payload, entries=[live]))
            page.evaluate(f"() => showResultCard({payload_live}, '{PHOTO}')")
            page.wait_for_timeout(300)
            page.click(".result-adjust")
            page.wait_for_selector("#confirm-overlay:not(.hidden)", timeout=5000)
            page.wait_for_timeout(400)
            path = os.path.join(OUT_DIR, "confirm_panel_update.png")
            page.screenshot(path=path, full_page=True)
            shots.append(path)
            print(f"  wrote {path}")

            btn = page.text_content("#confirm-log-btn")
            print(f"\n  Adjust -> confirm button reads: {btn!r} "
                  f"({'correct' if btn == 'Update' else 'EXPECTED Update'})")

            # And Update must hand the card back, not dump you in the day view.
            page.click("#confirm-log-btn")
            page.wait_for_timeout(1200)
            back = page.evaluate(
                "() => !document.getElementById('result-card').classList.contains('hidden')")
            print(f"  Update returned to the result card: {back} "
                  f"({'correct' if back else 'EXPECTED True'})")
            if errors:
                print("\n  PAGE ERRORS:")
                for e in errors[:10]:
                    print(f"    {e}")
            else:
                print("  no page errors")
            browser.close()
    finally:
        proc.terminate()
    print(f"\n{len(shots)} screenshots in {OUT_DIR}")


if __name__ == "__main__":
    main()
