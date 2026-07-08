"""Local speech-to-text via faster-whisper.

Replaces the browser's Web Speech API: the frontend records real audio with
MediaRecorder and uploads the blob; we transcribe it here on the host PC.
Free, private, and works identically on every phone/browser.

The model is loaded once per process (lazily, ~8s) and kept as a singleton.
Transcription itself is CPU-bound sync code, so callers go through the async
`transcribe()` wrapper which pushes it onto a worker thread.
"""
import asyncio
import io
import threading

from app.config import WHISPER_MODEL

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def _transcribe_sync(audio_bytes: bytes) -> str:
    # faster-whisper decodes via PyAV, so webm/opus (Chrome/Android) and
    # mp4/AAC (iOS Safari) both work straight from the upload bytes.
    segments, _info = _get_model().transcribe(io.BytesIO(audio_bytes), language="en")
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe an uploaded audio blob to text. Raises on undecodable input."""
    return await asyncio.to_thread(_transcribe_sync, audio_bytes)


def warm_up() -> None:
    """Optionally called at startup so the first voice log isn't slow."""
    threading.Thread(target=_get_model, daemon=True).start()
