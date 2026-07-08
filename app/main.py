import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.database import init_db
from app.routers import agent, auth, foods, log, push, reminders, recipes
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

app.include_router(agent.router)
app.include_router(auth.router)
app.include_router(foods.router)
app.include_router(log.router)
app.include_router(push.router)
app.include_router(reminders.router)
app.include_router(recipes.router)


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    return FileResponse("static/index.html")
