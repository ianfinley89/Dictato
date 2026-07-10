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
