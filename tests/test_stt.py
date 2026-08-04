"""The Whisper silence-hallucination guard: on silent/near-silent audio (a
phone mic that barely picks up the voice) Whisper emits caption sign-offs like
"Thank you." or "Bye bye." instead of nothing. These tests cover the pure
filtering layer — no model is loaded."""
from types import SimpleNamespace

from app.services.stt import _clean_transcript, _is_hallucination


def seg(text, no_speech=0.05, logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob)


def test_real_speech_passes_through():
    segs = [seg(" I had two rice cakes "), seg("and a coffee")]
    assert _clean_transcript(segs) == "I had two rice cakes and a coffee"


def test_silence_signoffs_are_discarded():
    for phrase in ("Thank you.", "Bye bye.", "Thanks for watching!",
                   "Thank you. Bye bye.", "you", "Thank you very much."):
        assert _clean_transcript([seg(phrase)]) == "", phrase


def test_whisper_flagged_segment_is_dropped():
    # High no_speech_prob + low decode confidence = Whisper's own "probably
    # not speech" signature; the real food note around it survives.
    segs = [seg("I ate a bowl of chili"),
            seg("Thanks for watching!", no_speech=0.92, logprob=-1.4)]
    assert _clean_transcript(segs) == "I ate a bowl of chili"


def test_signoff_words_inside_real_speech_are_kept():
    text = "I had the thank you mints from the restaurant"
    assert _clean_transcript([seg(text)]) == text


def test_empty_segments_mean_no_speech():
    assert _clean_transcript([]) == ""
    assert _is_hallucination("")


# ── Thresholds calibrated on real production audio ───────────────────────────
# Every capture whose audio we kept, replayed: 26 that produced a real log
# peaked at no_speech_prob 0.087; 6 that logged nothing bottomed out at 0.195.
REAL_WORST = 0.087        # the least confident recording that WAS real speech
FAKE_BEST = 0.195         # the most confident recording that was NOT

def test_hallucinations_from_production_are_caught():
    """The exact transcripts one user got over five days before they left. None
    are in the sign-off word list, and none could be — Whisper's hallucination
    vocabulary is open-ended, which is why the signal has to be acoustic."""
    for text in ("Have a good day. Have a good one.", "It's", "of an",
                 "I have fun.", "I try to meet.",
                 "You're three-foot-seven-eight-eight-eight"):
        assert _clean_transcript([seg(text, no_speech=FAKE_BEST)]) == "", text


def test_quiet_but_real_speech_is_kept():
    """The other side of the same threshold: a real log from a poor mic must
    survive, or the guard trades one silent failure for another."""
    got = _clean_transcript([seg("I had Trader Joe's lamb vindaloo",
                                 no_speech=REAL_WORST, logprob=-0.590)])
    assert got == "I had Trader Joe's lamb vindaloo"


def test_guard_uses_or_not_and():
    """Requiring BOTH signals is what let this through in production: fluent
    hallucinated caption text decodes cleanly, so its logprob looks healthy
    while no_speech_prob is the only tell."""
    assert _clean_transcript([seg("Have a good day.", no_speech=0.34, logprob=-0.3)]) == ""
    assert _clean_transcript([seg("mumbled food words", no_speech=0.02, logprob=-1.2)]) == ""
