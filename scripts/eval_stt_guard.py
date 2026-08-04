"""Replay every saved voice capture through the silence guard.

The guard exists because Whisper, trained on captioned video, answers silence
with caption sign-offs instead of nothing. Its thresholds were guesses, and the
guess was wrong by a wide margin: it required no_speech_prob > 0.6 while the
worst real hallucination reached 0.338, so it never fired once in production.
One user got nine straight "I'm not sure what you ate" replies over five days
and stopped using the app.

Ground truth here is what the capture DID: a capture that produced log entries
was real speech, one that produced none was not. That is not a label anybody
assigned after the fact, which is what makes it usable — unlike the entries
themselves, which users merely accepted.

Run this after changing a threshold, the Whisper model size, or the VAD
settings. Both error directions matter: dropping a real log is worse than
letting a hallucination through, because the user loses food they logged.

    uv run python scripts/eval_stt_guard.py
"""
import io
import os
import sqlite3
import sys

sys.path.insert(0, ".")

from app.config import get_db_path                                   # noqa: E402
from app.services.stt import (_clean_transcript, _get_model,         # noqa: E402
                              _MAX_NO_SPEECH, _MIN_LOGPROB)

SWEEP = (0.05, 0.10, 0.12, 0.15, 0.20, 0.30, 0.60)


def main() -> None:
    conn = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT user_id, transcript, audio_path,
                  (entries_json IN ('[]','') OR entries_json IS NULL) AS failed
           FROM capture_log WHERE audio_path IS NOT NULL"""
    ).fetchall()

    model = _get_model()
    real, fake = [], []
    for r in rows:
        path = r["audio_path"]
        if not path or not os.path.exists(path):
            continue
        try:
            segments = list(model.transcribe(
                io.BytesIO(open(path, "rb").read()), language="en", vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False)[0])
        except Exception as e:
            print(f"  undecodable: {os.path.basename(path)} ({type(e).__name__})")
            continue
        ns = max((s.no_speech_prob for s in segments), default=0.0)
        lp = min((s.avg_logprob for s in segments), default=0.0)
        (fake if r["failed"] else real).append((ns, lp, _clean_transcript(segments)))

    if not real or not fake:
        print(f"not enough saved audio to calibrate ({len(real)} real, {len(fake)} silent)")
        return

    print(f"{len(real)} captures that logged something, {len(fake)} that logged nothing\n")
    print(f"  real speech      no_speech_prob max {max(r[0] for r in real):.3f}"
          f"   avg_logprob min {min(r[1] for r in real):.3f}")
    print(f"  silent/garbage   no_speech_prob min {min(f[0] for f in fake):.3f}"
          f"   avg_logprob min {min(f[1] for f in fake):.3f}")

    # Captures Whisper returns no segments for are already empty and need no
    # threshold; counting them as "caught" would flatter every setting equally.
    scored = [f for f in fake if f[0] or f[1]]
    print(f"\nthreshold sweep over the {len(scored)} silent captures that produced "
          f"segments\n({len(fake) - len(scored)} more returned nothing at all and are "
          f"empty regardless)")
    for t in SWEEP:
        caught = sum(1 for f in scored if f[0] > t or f[1] < _MIN_LOGPROB)
        lost = sum(1 for r in real if r[0] > t or r[1] < _MIN_LOGPROB)
        flag = "  <- current" if abs(t - _MAX_NO_SPEECH) < 1e-9 else ""
        print(f"  no_speech > {t:4.2f}   catches {caught}/{len(scored)} silent"
              f"   DROPS {lost}/{len(real)} real{flag}")

    kept = sum(1 for r in real if r[2])
    leaked = sum(1 for f in fake if f[2])
    print(f"\nas configured: kept {kept}/{len(real)} real, "
          f"leaked {leaked}/{len(fake)} hallucinations")
    for f in fake:
        if f[2]:
            print(f"    leaked: {f[2][:60]!r}")


if __name__ == "__main__":
    main()
