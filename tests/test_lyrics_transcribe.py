"""Tests for lib/lyrics_transcribe.py — WhisperX lyric transcription helpers.

The actual WhisperX inference is too heavy and non-deterministic to test
in CI (multi-GB model download, GPU optional, hallucinations on the
edges). These tests cover the parts that are deterministic and don't
need the model:

* `_whisperx_to_sloppak` — pure dict→dict mapping (score filter,
  line-break heuristic, duration clamp, rounding).
* `vocals_has_signal` — RMS gate over a synthesized WAV.
* `whisperx_available` — graceful False when the package isn't installed.

The end-to-end positive case (PSARC without lyrics → sloppak with
auto-transcribed lyrics) is intentionally a manual verification step;
see the plan's verification section.
"""

from __future__ import annotations

import importlib

import pytest

from lyrics_transcribe import (
    _whisperx_to_sloppak,
    vocals_has_signal,
    whisperx_available,
)


# ── Mapper: scores, line breaks, durations, rounding ────────────────────────

def test_mapper_passes_words_above_score_threshold():
    aligned = {
        "segments": [
            {"words": [
                {"word": "hello", "start": 1.0, "end": 1.3, "score": 0.9},
                {"word": "world", "start": 1.4, "end": 1.7, "score": 0.8},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    assert got == [
        {"t": 1.0, "d": 0.3, "w": "hello"},
        {"t": 1.4, "d": 0.3, "w": "world"},
    ]


def test_mapper_drops_words_below_score_threshold():
    aligned = {
        "segments": [
            {"words": [
                {"word": "good", "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "bad",  "start": 1.3, "end": 1.5, "score": 0.10},
                {"word": "ugly", "start": 1.6, "end": 1.8, "score": 0.05},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    assert [w["w"] for w in got] == ["good"]


def test_mapper_drops_words_with_missing_score():
    # WhisperX occasionally emits words it failed to localize without a
    # score field — those must drop, not pass-through as untrusted text.
    aligned = {
        "segments": [
            {"words": [
                {"word": "scored",   "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "unscored", "start": 1.3, "end": 1.5},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["scored"]


def test_mapper_inserts_line_break_on_segment_gap_above_threshold():
    # Two segments separated by > 1.5s — the mapper appends `+` to the
    # last word of the previous line. Matches static/highway.js which
    # parses `+` as a suffix marker (raw.endsWith('+')) and strips it
    # before rendering. A bare {"w": "+"} token would be rendered as an
    # empty syllable / blank slot in the overlay.
    aligned = {
        "segments": [
            {"words": [
                {"word": "first", "start": 1.0, "end": 1.2, "score": 0.9},
            ]},
            {"words": [
                {"word": "second", "start": 5.0, "end": 5.3, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got == [
        {"t": 1.0, "d": 0.2, "w": "first+"},
        {"t": 5.0, "d": 0.3, "w": "second"},
    ]


def test_mapper_does_not_insert_line_break_for_close_segments():
    # Gap of 0.5s — well under the threshold, no break syllable.
    aligned = {
        "segments": [
            {"words": [{"word": "a", "start": 1.0, "end": 1.2, "score": 0.9}]},
            {"words": [{"word": "b", "start": 1.7, "end": 1.9, "score": 0.9}]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["a", "b"]


def test_mapper_no_line_break_for_singer_breath_gap():
    # Gap of 2.0s — typical between-phrase singer breath on slow vocals.
    # Pinned behavior: must NOT trigger a `+` break (would fragment a
    # verse into one-line-per-phrase). The old 1.5s threshold would
    # have broken here; the current 3.0s default leaves it intact.
    aligned = {
        "segments": [
            {"words": [{"word": "phrase1", "start": 1.0, "end": 1.5, "score": 0.9}]},
            {"words": [{"word": "phrase2", "start": 3.5, "end": 4.0, "score": 0.9}]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["phrase1", "phrase2"]


def test_mapper_clamps_zero_duration_to_floor():
    # WhisperX sometimes emits start == end for ultra-short syllables.
    # The lyrics overlay's fade timing needs a non-zero `d`, so the
    # mapper clamps to a small floor.
    aligned = {
        "segments": [
            {"words": [
                {"word": "x", "start": 1.0, "end": 1.0, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got[0]["d"] == 0.05


def test_mapper_rounds_to_three_decimals():
    # Pin the rounding precision so an accidental change doesn't silently
    # shift every downstream lyric timestamp — same contract _parse_lyrics
    # and parse_vocals_sng follow.
    aligned = {
        "segments": [
            {"words": [
                {"word": "hi", "start": 1.234567, "end": 1.876543, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert got[0]["t"] == 1.235
    assert got[0]["d"] == 0.642


def test_mapper_skips_empty_word_text():
    # Whitespace-only "word" entries are dropped — the highway overlay
    # would render them as blank syllables otherwise.
    aligned = {
        "segments": [
            {"words": [
                {"word": "   ", "start": 1.0, "end": 1.2, "score": 0.9},
                {"word": "real", "start": 1.3, "end": 1.5, "score": 0.9},
            ]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.0)
    assert [w["w"] for w in got] == ["real"]


def test_mapper_gap_anchors_on_segment_end_not_last_surviving_word():
    # Trailing word of segment 1 gets filtered (low score). prev_end must
    # still advance to the segment's real end, otherwise the gap to
    # segment 2 measures from "first" (1.2s) to "second" (3.0s) = 1.8s
    # and falsely triggers a `+` line break. The segment's actual end
    # is 5.0s, so gap is 3.0 - 5.0 = -2s, no break.
    aligned = {
        "segments": [
            {
                "end": 5.0,
                "words": [
                    {"word": "first", "start": 1.0, "end": 1.2, "score": 0.9},
                    {"word": "dropped", "start": 4.5, "end": 5.0, "score": 0.05},
                ],
            },
            {
                "words": [
                    {"word": "second", "start": 3.0, "end": 3.3, "score": 0.9},
                ],
            },
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    # Only "first" + "second" survive the filter, no `+` suffix.
    assert got == [
        {"t": 1.0, "d": 0.2, "w": "first"},
        {"t": 3.0, "d": 0.3, "w": "second"},
    ]


def test_mapper_advances_prev_end_past_fully_filtered_segment():
    # Entire middle segment gets filtered out, but prev_end must still
    # advance past it via segment.end. Otherwise the gap from "first"
    # (end 1.2s) to "third" (start 8.0s) registers as 6.8s and triggers
    # a `+` break against "first" — wrong, the actual gap from the
    # filtered segment's true end (7.5s) to "third" (8.0s) is 0.5s.
    aligned = {
        "segments": [
            {"end": 1.5, "words": [{"word": "first", "start": 1.0, "end": 1.2, "score": 0.9}]},
            {"end": 7.5, "words": [{"word": "dropped", "start": 5.0, "end": 7.5, "score": 0.05}]},
            {"words": [{"word": "third", "start": 8.0, "end": 8.4, "score": 0.9}]},
        ],
    }
    got = _whisperx_to_sloppak(aligned, min_score=0.35)
    # No `+` suffix on "first" — short gap to the third segment.
    assert got == [
        {"t": 1.0, "d": 0.2, "w": "first"},
        {"t": 8.0, "d": 0.4, "w": "third"},
    ]


def test_mapper_handles_empty_input():
    assert _whisperx_to_sloppak({}, min_score=0.0) == []
    assert _whisperx_to_sloppak({"segments": []}, min_score=0.0) == []
    assert _whisperx_to_sloppak({"segments": [{"words": []}]}, min_score=0.0) == []


# ── Silence gate (uses soundfile + numpy) ───────────────────────────────────


def _import_soundfile_or_skip():
    """Import soundfile, skip the test if it OR its native libsndfile is
    missing. `pytest.importorskip("soundfile")` alone only catches
    ImportError, but `import soundfile` performs a ctypes load of
    libsndfile at import time — on a host without the native lib it
    raises OSError, which would error the test suite (especially now
    that soundfile is in CI's requirements-test.txt and the wheel is
    expected to be present) instead of cleanly skipping."""
    try:
        import soundfile as sf
        return sf
    except ImportError as e:
        pytest.skip(f"soundfile not installed: {e}")
    except OSError as e:
        pytest.skip(f"soundfile native library unavailable: {e}")


def _make_wav(path, samples, sr: int = 22050):
    sf = _import_soundfile_or_skip()
    sf.write(str(path), samples, sr)


def test_vocals_has_signal_returns_false_for_silent_wav(tmp_path):
    np = pytest.importorskip("numpy")
    _import_soundfile_or_skip()
    silent = np.zeros(22050, dtype="float32")
    p = tmp_path / "silent.wav"
    _make_wav(p, silent)
    assert vocals_has_signal(p, threshold=0.005) is False


def test_vocals_has_signal_returns_true_for_loud_wav(tmp_path):
    np = pytest.importorskip("numpy")
    _import_soundfile_or_skip()
    # 440Hz sine at full scale — clearly above any reasonable threshold.
    t = np.linspace(0, 1.0, 22050, endpoint=False, dtype="float32")
    sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    p = tmp_path / "tone.wav"
    _make_wav(p, sine)
    assert vocals_has_signal(p, threshold=0.005) is True


def test_vocals_has_signal_open_fails_returns_true(tmp_path):
    # Gate is best-effort: when reading the file fails we let downstream
    # surface the real error rather than mis-classify the input as silent.
    _import_soundfile_or_skip()
    p = tmp_path / "does-not-exist.wav"
    assert vocals_has_signal(p, threshold=0.005) is True


# ── Availability probe ─────────────────────────────────────────────────────

def test_whisperx_available_returns_bool():
    # Doesn't matter whether the test machine has whisperx installed —
    # the probe must return a plain bool, never raise.
    result = whisperx_available()
    assert isinstance(result, bool)


def test_whisperx_available_returns_false_when_import_fails(monkeypatch):
    # Force the import to fail so we exercise the False branch even on
    # machines that happen to have whisperx installed in the test venv.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "whisperx":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Force a fresh module import so the deferred `import whisperx` inside
    # whisperx_available() takes the patched path.
    import lyrics_transcribe
    importlib.reload(lyrics_transcribe)
    assert lyrics_transcribe.whisperx_available() is False
    # Reload again unpatched so we don't leave the patched module in
    # sys.modules for downstream tests.
    monkeypatch.undo()
    importlib.reload(lyrics_transcribe)
