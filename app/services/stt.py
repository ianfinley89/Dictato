"""Local speech-to-text via faster-whisper.

Replaces the browser's Web Speech API: the frontend records real audio with
MediaRecorder and uploads the blob; we transcribe it here on the host PC.
Free, private, and works identically on every phone/browser.

The model is loaded once per process (lazily, ~8s) and kept as a singleton.
Transcription itself is CPU-bound sync code, so callers go through the async
`transcribe()` wrapper which pushes it onto a worker thread.

Silence handling: Whisper was trained on captioned video, so on silent or
near-silent audio (a phone mic that barely picks up the voice) it hallucinates
caption sign-offs — "Thank you.", "Bye bye.", "Thanks for watching!" — instead
of returning nothing. Three guards, in order: Silero VAD strips non-speech
before Whisper sees it; segments Whisper itself flags as probably-not-speech
are dropped; and a transcript made up ENTIRELY of sign-off words is discarded.
An empty result surfaces to the user as "didn't catch any speech, try again"
rather than a bogus food log.
"""
import asyncio
import io
import re
import threading

from app.config import WHISPER_MODEL

_model = None
_model_lock = threading.Lock()

# A voice note to a food tracker never consists solely of these words.
_JUNK_WORDS = {"thank", "thanks", "you", "bye", "goodbye", "for",
               "watching", "listening", "very", "much", "so"}
_JUNK_PHRASES = {"the end", "you're welcome", "okay", "ok", "amen",
                 "hmm", "mm", "uh", "um", "oh",
                 "subtitles by the amara org community"}


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def _is_hallucination(text: str) -> bool:
    """True when the whole transcript is caption-noise, not speech."""
    words = re.sub(r"[^a-z' ]+", " ", text.lower()).split()
    if not words:
        return True
    return set(words) <= _JUNK_WORDS or " ".join(words) in _JUNK_PHRASES


def _clean_transcript(segments) -> str:
    """Join segments, dropping the ones Whisper itself doubts — the classic
    hallucination signature is high no_speech_prob plus low decode confidence."""
    kept = [s.text.strip() for s in segments
            if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
    text = " ".join(t for t in kept if t).strip()
    return "" if _is_hallucination(text) else text


def _transcribe_sync(audio_bytes: bytes) -> str:
    # faster-whisper decodes via PyAV, so webm/opus (Chrome/Android) and
    # mp4/AAC (iOS Safari) both work straight from the upload bytes.
    segments, _info = _get_model().transcribe(
        io.BytesIO(audio_bytes), language="en",
        vad_filter=True,                                    # silence never reaches Whisper
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,                   # stops repetition loops
    )
    return _clean_transcript(segments)


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe an uploaded audio blob to text. Raises on undecodable input."""
    return await asyncio.to_thread(_transcribe_sync, audio_bytes)


def warm_up() -> None:
    """Optionally called at startup so the first voice log isn't slow."""
    threading.Thread(target=_get_model, daemon=True).start()
