import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.database import init_db
from app.routers import admin, agent, auth, coach, foods, issues, log, push, reminders, recipes
from app.services.scheduler import reminder_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data", exist_ok=True)
    os.makedirs("uploads", exist_ok=True)
    init_db()
    if os.getenv("WHISPER_WARMUP", "true").lower() == "true":
        from app.services import stt
        stt.warm_up()   # load the model now so the first voice log isn't slow
    task = asyncio.create_task(reminder_loop())   # fire meal reminders on schedule
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Dictato", lifespan=lifespan)


@app.middleware("http")
async def record_unhandled_errors(request, call_next):
    """Log unhandled exceptions to app_errors so production failures are visible
    in the admin dashboard. Best-effort; the error still propagates as a 500."""
    try:
        return await call_next(request)
    except Exception as e:
        try:
            from app.database import get_conn
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO app_errors (method, path, error) VALUES (?,?,?)",
                    (request.method, request.url.path, repr(e)[:1000]),
                )
        except Exception:
            pass
        raise

app.include_router(admin.router)
app.include_router(agent.router)
app.include_router(auth.router)
app.include_router(coach.router)
app.include_router(foods.router)
app.include_router(issues.router)
app.include_router(log.router)
app.include_router(push.router)
app.include_router(reminders.router)
app.include_router(recipes.router)


# The app shell must never be served from a browser's heuristic cache: with only
# an ETag and no Cache-Control, browsers may reuse old JS for hours WITHOUT
# revalidating, so a shipped fix quietly fails to reach anyone. "no-cache" still
# allows a cheap 304 — it just forces the check.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that always revalidates code/markup (cheap 304s), while
    letting genuinely immutable assets (icons) stay cached for a day."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(getattr(resp, "path", ""))
        if path.endswith((".js", ".css", ".html", ".json")):
            resp.headers.update(_NO_CACHE)
        else:
            resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        return resp


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript",
                        headers=_NO_CACHE)


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json",
                        media_type="application/manifest+json", headers=_NO_CACHE)


app.mount("/static", _RevalidatingStatic(directory="static"), name="static")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    return FileResponse("static/index.html", headers=_NO_CACHE)
