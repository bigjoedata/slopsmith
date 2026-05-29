"""Tests for lib/gp2rs_gpx.py — the Guitar Pro 6 (.gpx) import path.

Fixture-free: every test exercises a pure helper with hand-built inputs
(ElementTree fragments, tuning lists, crafted container headers). The binary
BCFZ/BCFS round-trip needs a real .gpx and is covered by manual validation in
the PR; here we pin the input-validation guards and the conversion helpers
that are easy to drive without a fixture.
"""

import struct
import xml.etree.ElementTree as ET

import pytest

import gp2rs_gpx
from gp2rs_gpx import convert_file

from gp2rs_gpx import (
    _decompress_bcfz,
    _parse_bcfs,
    _safe_filename_stem,
    _note_is_tie,
    _gpx_tuning,
    _gp6_element_variation_to_midi,
    _GPX_MAX_DECOMPRESSED,
    _find_piano_pairs,
    convert_vocal_track_to_pitch_sidecar,
    _collect_tone_events,
    _inject_tones,
)


# ── _safe_filename_stem ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name, expected", [
    ("Lead Guitar", "Lead_Guitar"),
    ("AC/DC", "AC_DC"),
    (r"..\..\evil", "evil"),
    ("../../etc/passwd", "etc_passwd"),
    ("C:\\Windows\\x", "C_Windows_x"),
    ("", "track"),
    ("...", "track"),
    ("Bass (5)", "Bass_5"),
])
def test_safe_filename_stem(name, expected):
    out = _safe_filename_stem(name)
    assert out == expected
    # Never contains a path separator or traversal segment.
    assert "/" not in out and "\\" not in out
    assert ".." not in out


# ── _decompress_bcfz / _parse_bcfs input guards ─────────────────────────────

def test_decompress_bcfz_rejects_bad_magic():
    with pytest.raises(ValueError):
        _decompress_bcfz(b"XXXX" + b"\x00" * 8)


def test_decompress_bcfz_rejects_oversized_declared_size():
    # 4 bytes after the magic are read verbatim as a little-endian uint32 = the
    # declared decompressed size. Declare > cap -> ValueError before allocating.
    blob = b"BCFZ" + struct.pack("<I", _GPX_MAX_DECOMPRESSED + 1)
    with pytest.raises(ValueError):
        _decompress_bcfz(blob)


def test_parse_bcfs_rejects_bad_magic():
    with pytest.raises(ValueError):
        _parse_bcfs(b"NOPE" + b"\x00" * 16)


# ── _note_is_tie ────────────────────────────────────────────────────────────

def test_note_is_tie_destination():
    el = ET.fromstring('<Note><Tie destination="true"/></Note>')
    assert _note_is_tie(el) is True


def test_note_is_tie_origin_only_is_not_tie():
    el = ET.fromstring('<Note><Tie origin="true"/></Note>')
    assert _note_is_tie(el) is False


def test_note_is_tie_absent():
    assert _note_is_tie(ET.fromstring("<Note/>")) is False


# ── _gp6_element_variation_to_midi ──────────────────────────────────────────

def test_element_variation_out_of_range_is_none():
    assert _gp6_element_variation_to_midi(9999, 0) is None
    assert _gp6_element_variation_to_midi(-1, 0) is None


def test_element_variation_known_pieces():
    # Element 0 = kick (GM 35), element 1 = snare (GM 38). Pin exact values so a
    # mis-edit of the _GP6_EV / _ART_TO_MIDI tables is caught.
    assert _gp6_element_variation_to_midi(0, 0) == 35
    assert _gp6_element_variation_to_midi(1, 0) == 38


# ── _gpx_tuning ─────────────────────────────────────────────────────────────
# GPX string pitches are high->low (index 0 = highest string).

def test_tuning_6string_guitar_standard_is_zero():
    # E B G D A E (MIDI 64 59 55 50 45 40)
    assert _gpx_tuning({"string_pitches": [64, 59, 55, 50, 45, 40]}) == [0, 0, 0, 0, 0, 0]


def test_tuning_6string_guitar_eb_is_minus_one():
    assert _gpx_tuning({"string_pitches": [63, 58, 54, 49, 44, 39]}) == [-1, -1, -1, -1, -1, -1]


def test_tuning_4string_bass_standard_is_zero():
    # G D A E (high->low): 43 38 33 28
    assert _gpx_tuning({"string_pitches": [43, 38, 33, 28]}) == [0, 0, 0, 0]


def test_tuning_5string_low_b_standard_is_zero():
    # low-B 5-string, high->low: G D A E B = 43 38 33 28 23
    assert _gpx_tuning({"string_pitches": [43, 38, 33, 28, 23]}) == [0, 0, 0, 0, 0]


def test_tuning_5string_high_c_standard_is_zero():
    # high-C 5-string, high->low: C G D A E = 48 43 38 33 28.
    # Regression guard: previously forced the low-B reference and produced
    # non-zero offsets for a standard-tuned high-C bass.
    assert _gpx_tuning({"string_pitches": [48, 43, 38, 33, 28]}) == [0, 0, 0, 0, 0]


def test_tuning_empty_pitches_defaults_six_zero():
    assert _gpx_tuning({"string_pitches": []}) == [0, 0, 0, 0, 0, 0]


# ── _find_piano_pairs ───────────────────────────────────────────────────────

def test_find_piano_pairs_returns_rh_to_lh_map():
    # "Piano RH"/"Piano LH" share a stem -> map {rh: lh} (LH merges into RH at
    # import time), LH consumed.
    tracks = [{"name": "Piano RH"}, {"name": "Piano LH"}, {"name": "Lead Guitar"}]
    names = {0: "Keys", 1: "Keys 2", 2: "Lead"}
    filtered, merge_map = _find_piano_pairs([0, 1, 2], tracks, names)
    assert merge_map == {0: 1}
    assert filtered == [0, 2]  # LH (1) removed, order otherwise preserved


def test_find_piano_pairs_no_lh_no_merge():
    # An RH with no matching LH stem is left untouched.
    tracks = [{"name": "Piano RH"}, {"name": "Synth Pad"}]
    names = {0: "Keys", 1: "Keys 2"}
    filtered, merge_map = _find_piano_pairs([0, 1], tracks, names)
    assert merge_map == {}
    assert filtered == [0, 1]


def test_find_piano_pairs_ignores_non_keys_tracks():
    # "rh"/"lh" word boundaries on guitar tracks must not trigger a merge:
    # only piano/keys/keyboard-named (or names[]=Keys*) tracks are considered.
    tracks = [{"name": "Rhythm Guitar RH"}, {"name": "Lead Guitar LH"}]
    names = {0: "Rhythm", 1: "Lead"}
    filtered, merge_map = _find_piano_pairs([0, 1], tracks, names)
    assert merge_map == {}
    assert filtered == [0, 1]


# ── convert_vocal_track_to_pitch_sidecar ────────────────────────────────────
# Drives the per-syllable pitch extraction with a one-bar / one-beat GPX tree
# (String+Fret note encoding) — no real .gpx needed.

def _vocal_sidecar_args(*, with_lyric: bool):
    """Build the minimal ET fragments for a single quarter-note vocal beat at
    middle C (string_pitches[0]=60, String 0 + Fret 0). Returns a dict of
    keyword args (expanded with **) for convert_vocal_track_to_pitch_sidecar."""
    lyric = "<Lyrics><Line>la</Line></Lyrics>" if with_lyric else ""
    beat = ET.fromstring(
        f'<Beat><Rhythm ref="r0"/>{lyric}<Notes>0</Notes></Beat>'
    )
    note = ET.fromstring(
        '<Note>'
        '<Property name="String"><String>0</String></Property>'
        '<Property name="Fret"><Fret>0</Fret></Property>'
        '</Note>'
    )
    masterbar = ET.fromstring('<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>')
    return dict(
        root=ET.fromstring('<GPIF/>'),                  # no MasterTrack -> 120 BPM
        track={'string_pitches': [60]},
        raw_idx=0,
        masterbars=[masterbar],
        bars_by_id={'0': ET.fromstring('<Bar><Voices>0</Voices></Bar>')},
        voices_dict={'0': ET.fromstring('<Voice><Beats>0</Beats></Voice>')},
        beats_dict={'0': beat},
        notes_dict={'0': note},
        rhythms_dict={'r0': ET.fromstring('<Rhythm><NoteValue>Quarter</NoteValue></Rhythm>')},
    )


def test_vocal_pitch_sidecar_emits_lyric_note():
    out = convert_vocal_track_to_pitch_sidecar(**_vocal_sidecar_args(with_lyric=True))
    # Quarter note (1.0 qn) at 120 BPM = 1.0 * 60/120 = 0.5 s; pitch = 60.
    assert out == {"version": 1, "notes": [{"t": 0.0, "d": 0.5, "midi": 60}]}


def test_vocal_pitch_sidecar_skips_beat_without_lyric():
    out = convert_vocal_track_to_pitch_sidecar(**_vocal_sidecar_args(with_lyric=False))
    assert out == {"version": 1, "notes": []}


# ── _collect_tone_events ────────────────────────────────────────────────────

def _tone_args(banks, tempo_map=((0, 120.0),)):
    """One 4/4 bar with one quarter-note beat per entry in `banks`; a None entry
    means a beat with no <Bank>. Returns positional args for _collect_tone_events.
    With the default 120 BPM map a quarter note is 0.5 s."""
    beat_ids = " ".join(str(i) for i in range(len(banks)))
    beats_dict = {}
    for i, b in enumerate(banks):
        bank_el = f"<Bank>{b}</Bank>" if b is not None else ""
        beats_dict[str(i)] = ET.fromstring(f'<Beat><Rhythm ref="r0"/>{bank_el}</Beat>')
    return (
        0,                                                                   # raw_idx
        [ET.fromstring('<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>')],
        {"0": ET.fromstring("<Bar><Voices>0</Voices></Bar>")},
        {"0": ET.fromstring(f"<Voice><Beats>{beat_ids}</Beats></Voice>")},
        beats_dict,
        {"r0": ET.fromstring("<Rhythm><NoteValue>Quarter</NoteValue></Rhythm>")},
        [tuple(t) for t in tempo_map],                                       # tempo_map
        0.0,                                                                 # audio_offset
    )


def test_collect_tone_events_emits_per_bank():
    events = _collect_tone_events(*_tone_args(["Clean", "Dist"]))
    assert events == [(0.0, "Clean"), (0.5, "Dist")]


def test_collect_tone_events_dedupes_consecutive_identical():
    # Consecutive identical banks collapse to a single transition.
    events = _collect_tone_events(*_tone_args(["Clean", "Clean", "Dist"]))
    assert events == [(0.0, "Clean"), (1.0, "Dist")]


def test_collect_tone_events_empty_when_no_banks():
    assert _collect_tone_events(*_tone_args([None, None])) == []


def test_collect_tone_events_honors_base_tempo_bpm():
    # No bar-0 tempo event -> the base tempo_bpm seeds the timeline (matching
    # convert_file). At 60 BPM a quarter note is 1.0 s, so the second bank
    # change lands at 1.0 s (not 0.5 s as it would at the hardcoded 120).
    events = _collect_tone_events(*_tone_args(["Clean", "Dist"], tempo_map=[]),
                                  tempo_bpm=60.0)
    assert events == [(0.0, "Clean"), (1.0, "Dist")]


# ── _inject_tones ───────────────────────────────────────────────────────────

def test_inject_tones_adds_tonebase_and_tones():
    out = _inject_tones("<song><arrangement>Lead</arrangement></song>",
                        [(0.0, "Clean"), (4.5, "Dist")])
    root = ET.fromstring(out)
    assert root.findtext("tonebase") == "Clean"          # base = first tone
    tones = root.find("tones")
    assert tones.get("count") == "2"
    tone_els = tones.findall("tone")
    assert [t.get("name") for t in tone_els] == ["Clean", "Dist"]
    assert [t.get("id") for t in tone_els] == ["0", "1"]
    assert [t.get("time") for t in tone_els] == ["0.000", "4.500"]


def test_inject_tones_does_not_bloat_whitespace():
    # Re-pretty-printing an already-indented arrangement must not stack blank
    # lines (regression guard for the double-pretty-print whitespace explosion).
    pretty = "<song>\n  <arrangement>Lead</arrangement>\n  <notes count=\"0\"/>\n</song>\n"
    out = _inject_tones(pretty, [(0.0, "Clean"), (4.5, "Dist")])
    assert not any(line.strip() == "" for line in out.splitlines())


def test_inject_tones_noop_without_events():
    xml = "<song><arrangement>Lead</arrangement></song>"
    assert _inject_tones(xml, []) == xml


def test_inject_tones_preserves_existing_tonebase():
    out = _inject_tones("<song><tonebase>Existing</tonebase></song>",
                        [(0.0, "Clean")])
    root = ET.fromstring(out)
    bases = root.findall("tonebase")
    assert len(bases) == 1 and bases[0].text == "Existing"


def test_inject_tones_fills_empty_tonebase():
    # An empty/whitespace <tonebase> gets populated with the first tone name.
    out = _inject_tones("<song><tonebase>  </tonebase></song>", [(0.0, "Clean")])
    root = ET.fromstring(out)
    bases = root.findall("tonebase")
    assert len(bases) == 1 and bases[0].text == "Clean"


# ── convert_file end-to-end: Piano LH/RH merge ──────────────────────────────
# Drives the real converter with a hand-built GPIF tree (via monkeypatched
# _load_gpif) to cover the in-converter merge + rename that the helper tests
# can't reach. Two keys tracks (Piano RH / Piano LH), one quarter-note each.

_GPIF_PIANO = """
<GPIF>
  <Score><Title>T</Title><Artist>A</Artist></Score>
  <Tracks>
    <Track id="0"><Name>Piano RH</Name>
      <Property name="Tuning"><Pitches>72</Pitches></Property></Track>
    <Track id="1"><Name>Piano LH</Name>
      <Property name="Tuning"><Pitches>48</Pitches></Property></Track>
  </Tracks>
  <MasterBars>
    <MasterBar><Time>4/4</Time><Bars>0 1</Bars></MasterBar>
  </MasterBars>
  <Bars>
    <Bar id="0"><Voices>0</Voices></Bar>
    <Bar id="1"><Voices>1</Voices></Bar>
  </Bars>
  <Voices>
    <Voice id="0"><Beats>0</Beats></Voice>
    <Voice id="1"><Beats>1</Beats></Voice>
  </Voices>
  <Beats>
    <Beat id="0"><Rhythm ref="r0"/><Notes>0</Notes></Beat>
    <Beat id="1"><Rhythm ref="r0"/><Notes>1</Notes></Beat>
  </Beats>
  <Notes>
    <Note id="0">
      <Property name="String"><String>0</String></Property>
      <Property name="Fret"><Fret>0</Fret></Property></Note>
    <Note id="1">
      <Property name="String"><String>0</String></Property>
      <Property name="Fret"><Fret>0</Fret></Property></Note>
  </Notes>
  <Rhythms><Rhythm id="r0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
</GPIF>
"""


def test_convert_file_merges_piano_lh_into_rh(tmp_path, monkeypatch):
    monkeypatch.setattr(gp2rs_gpx, "_load_gpif", lambda _p: ET.fromstring(_GPIF_PIANO))
    out_files = convert_file(
        "dummy.gpx", str(tmp_path),
        track_indices=[0, 1],
        arrangement_names={0: "Keys", 1: "Keys 2"},
    )
    # (1) LH is consumed by the merge -> a single combined arrangement file.
    assert len(out_files) == 1

    root = ET.parse(out_files[0]).getroot()
    # (2) "Keys 2" collapses to the standard "Keys" name (not "Piano"), so the
    # piano-highway auto-select (arr_name.startswith("keys")) still matches.
    assert root.findtext("arrangement") == "Keys"

    # (3) both hands' notes are present. Keys encoding packs MIDI as
    # string=midi//24, fret=midi%24: RH pitch 72 -> string 3, LH pitch 48 ->
    # string 2. Collect every emitted note (single <note> + chord <chordNote>).
    strings = {
        n.get("string")
        for n in root.iter()
        if n.tag in ("note", "chordNote")
    }
    assert "3" in strings   # RH (midi 72)
    assert "2" in strings   # LH (midi 48) merged in


def test_vocal_pitch_sidecar_sorts_multi_voice_by_time():
    # Two voices in one bar. Voice 0 (traversed first) emits its lyric note at
    # t=0.5 (a no-lyric quarter precedes it); voice 1 (traversed second) emits
    # at t=0.0. Output must be chronological regardless of traversal order.
    def _beat(nid, *, lyric):
        ly = "<Lyrics><Line>la</Line></Lyrics>" if lyric else ""
        return ET.fromstring(f'<Beat><Rhythm ref="r0"/>{ly}<Notes>{nid}</Notes></Beat>')

    def _note():
        return ET.fromstring(
            '<Note><Property name="String"><String>0</String></Property>'
            '<Property name="Fret"><Fret>0</Fret></Property></Note>'
        )

    out = convert_vocal_track_to_pitch_sidecar(
        root=ET.fromstring('<GPIF/>'),
        track={'string_pitches': [60]},
        raw_idx=0,
        masterbars=[ET.fromstring('<MasterBar><Time>4/4</Time><Bars>0</Bars></MasterBar>')],
        bars_by_id={'0': ET.fromstring('<Bar><Voices>0 1</Voices></Bar>')},
        voices_dict={
            '0': ET.fromstring('<Voice><Beats>0 1</Beats></Voice>'),  # rest, then lyric@0.5
            '1': ET.fromstring('<Voice><Beats>2</Beats></Voice>'),    # lyric@0.0
        },
        beats_dict={
            '0': _beat('0', lyric=False),
            '1': _beat('1', lyric=True),
            '2': _beat('2', lyric=True),
        },
        notes_dict={'0': _note(), '1': _note(), '2': _note()},
        rhythms_dict={'r0': ET.fromstring('<Rhythm><NoteValue>Quarter</NoteValue></Rhythm>')},
    )
    times = [n['t'] for n in out['notes']]
    assert times == sorted(times)        # chronological
    assert times == [0.0, 0.5]
