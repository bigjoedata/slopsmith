"""Side-effect-free metadata extraction worker for the library scan.

This module is deliberately kept apart from ``server.py`` so that
``ProcessPoolExecutor`` workers can import and unpickle ``_scan_one``
without dragging in ``server.py``'s import-time side effects
(``configure_logging()``, ``meta_db = MetadataDB()`` opening/migrating
SQLite, and ``register_plugin_api(app)`` registering routes).

The background scan spawns its pool with the ``spawn`` start method (see
``server._background_scan``), so each worker is a fresh interpreter that
imports only this module plus the pure ``lib`` helpers below — never the
whole server. That avoids two problems flagged in review:

* forking a ``ProcessPoolExecutor`` from the non-main scan thread (the
  default on Linux), which can deadlock on locks held by other threads at
  fork time; and
* re-running ``server.py``'s side effects in every worker on ``spawn``
  platforms (macOS/Windows), which would reopen SQLite per worker and let
  a multi-process ``RotatingFileHandler`` corrupt the log file.

It also means the per-file ``log.debug`` below simply no-ops inside
workers (logging is unconfigured there), which is the desired behaviour —
worker log records never reach the shared log file.
"""

import json
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from psarc import unpack_psarc, read_psarc_entries
from song import load_song, compute_smart_names
from tunings import tuning_name
import sloppak as sloppak_mod
import loosefolder as loosefolder_mod

log = logging.getLogger("slopsmith.scan_worker")


def _relpath(f: Path, dlc: Path) -> str:
    # Store the path relative to the DLC root so sub-folders (e.g.
    # dlc/sloppak/foo.sloppak produced by the converter) resolve back
    # correctly later. PSARCs always live directly in dlc/, so this
    # reduces to f.name for them.
    try:
        return f.relative_to(dlc).as_posix()
    except ValueError:
        return f.name


def _extract_meta_fast(psarc_path: Path) -> dict:
    """Extract metadata from a PSARC using in-memory reading (no disk I/O)."""
    files = read_psarc_entries(str(psarc_path), ["*.json", "*.xml", "*vocals*.sng"])

    title = artist = album = year = ""
    duration = 0.0
    tuning = "E Standard"
    # Track the offsets for the tuning we ultimately keep so we can
    # compute tuning_sort_key (#22) without re-deriving it from the
    # name. Defaults to E Standard offsets.
    tuning_offsets: list[int] = [0] * 6
    _tuning_from_guitar = False
    arrangements = []
    has_lyrics = False
    arr_index = 0

    # Parse manifest JSONs for metadata + arrangement info
    for path, data in sorted(files.items()):
        if not path.lower().endswith(".json"):
            continue
        try:
            jdata = json.loads(data)
            entries = jdata.get("Entries") or {}
            for k, v in entries.items():
                attrs = v.get("Attributes") or {}
                arr_name = attrs.get("ArrangementName", "")
                if arr_name in ("Vocals", "ShowLights", "JVocals"):
                    continue
                if not title:
                    title = attrs.get("SongName", "")
                    artist = attrs.get("ArtistName", "")
                    album = attrs.get("AlbumName", "")
                    yr = attrs.get("SongYear")
                    year = str(yr) if yr else ""
                    sl = attrs.get("SongLength")
                    if sl:
                        try: duration = float(sl)
                        except (ValueError, TypeError): pass
                if arr_name:
                    # Get tuning - prefer guitar arrangements over bass
                    tun = attrs.get("Tuning")
                    if tun and isinstance(tun, dict):
                        offsets = [tun.get(f"string{i}", 0) for i in range(6)]
                        tun_name = tuning_name(offsets)
                        is_guitar = arr_name in ("Lead", "Rhythm", "Combo")
                        if tuning == "E Standard" or (is_guitar and not _tuning_from_guitar):
                            tuning = tun_name
                            tuning_offsets = offsets
                            if is_guitar:
                                _tuning_from_guitar = True
                    notes = attrs.get("NotesHard", 0) or attrs.get("NotesMedium", 0) or 0
                    # Read path flags for smart naming — stored temporarily with
                    # underscore prefix and removed after smart names are computed.
                    props = attrs.get("ArrangementProperties") or {}
                    def _pi(key: str) -> int:
                        try: return int(props.get(key, 0) or 0)
                        except (TypeError, ValueError): return 0
                    arrangements.append({
                        "index": arr_index, "name": arr_name, "notes": notes,
                        "_path_lead": bool(_pi("pathLead")),
                        "_path_rhythm": bool(_pi("pathRhythm")),
                        "_path_bass": bool(_pi("pathBass")),
                        "_bonus_arr": bool(_pi("bonusArr")),
                        "_represent": _pi("represent"),
                    })
                    arr_index += 1
        except Exception:
            continue

    # Check XMLs for vocals (CDLC), or fall back to vocals SNG (official DLC)
    for path, data in files.items():
        if path.lower().endswith(".xml"):
            try:
                root = ET.fromstring(data)
                if root.tag == "vocals":
                    has_lyrics = True
                    break
            except Exception:
                continue
        elif path.lower().endswith(".sng") and "vocals" in path.lower():
            has_lyrics = True
            break

    # Sort arrangements: Lead > Combo > Rhythm > Bass
    priority = {"Lead": 0, "Combo": 1, "Rhythm": 2, "Bass": 3}
    arrangements.sort(key=lambda a: priority.get(a["name"], 99))
    for i, a in enumerate(arrangements):
        a["index"] = i

    # Compute smart names using the path flags read from the manifest.
    # Build minimal Arrangement objects, then discard the temp flag fields.
    from song import Arrangement as _ArrCls
    _arr_objs = [
        _ArrCls(
            name=a["name"],
            path_lead=a.pop("_path_lead", False),
            path_rhythm=a.pop("_path_rhythm", False),
            path_bass=a.pop("_path_bass", False),
            bonus_arr=a.pop("_bonus_arr", False),
            represent=a.pop("_represent", 0),
        )
        for a in arrangements
    ]
    _smart = compute_smart_names(_arr_objs)
    for a, sn in zip(arrangements, _smart):
        a["smart_name"] = sn

    return {
        "title": title, "artist": artist, "album": album, "year": year,
        "duration": duration, "tuning": tuning,
        "arrangements": arrangements, "has_lyrics": has_lyrics,
        # PSARCs have no stems; emit an empty list so the column round-
        # trips uniformly with sloppaks (slopsmith#129).
        "stem_ids": [],
        # Cached tuning fields (slopsmith#22 / #69). The text `tuning`
        # column above stays the source of truth for display; these are
        # the indexable / filterable forms.
        "tuning_name": tuning,
        "tuning_sort_key": sum(tuning_offsets),
    }


def _extract_meta_sloppak(path: Path) -> dict:
    """Extract metadata for a sloppak (file or directory)."""
    meta = sloppak_mod.extract_meta(path)
    offsets = meta.pop("tuning_offsets", None) or [0] * 6
    name = tuning_name(offsets)
    meta["tuning"] = name
    meta["tuning_name"] = name
    meta["tuning_sort_key"] = sum(offsets)
    meta["format"] = "sloppak"
    # `extract_meta` already populates `stem_ids` (slopsmith#129);
    # default to empty for older callers / mocks.
    meta.setdefault("stem_ids", [])
    # Compute smart names for sloppak arrangements using name-based fallback
    # (sloppak manifests use display names like "Lead"/"Rhythm"/"Bass" directly).
    arrs = meta.get("arrangements") or []
    if arrs:
        from song import Arrangement as _ArrCls
        _arr_objs = [_ArrCls(name=a.get("name", "")) for a in arrs]
        _smart = compute_smart_names(_arr_objs)
        for a, sn in zip(arrs, _smart):
            a["smart_name"] = sn
    return meta


def _extract_meta_loosefolder(path: Path, dlc_root: Path | None) -> dict:
    """Extract metadata for a loose song folder (raw XMLs + WEM audio).

    `dlc_root` is passed in (rather than resolved here via the server's
    `_get_dlc_dir()`) so this module stays free of server.py state and is
    safe to import in spawned ProcessPool workers.
    """
    # Pass the DLC root so artist/album folder inference operates on the
    # dlc-relative path; otherwise absolute-path parts (e.g. the user's
    # home dir name) would leak into metadata for songs placed shallow
    # inside DLC_DIR.
    meta = loosefolder_mod.extract_meta(path, dlc_root=dlc_root)
    offsets = meta.pop("tuning_offsets", None) or [0] * 6
    name = tuning_name(offsets)
    meta["tuning"] = name
    meta["tuning_name"] = name
    meta["tuning_sort_key"] = sum(offsets)
    meta["format"] = "loose"
    meta.setdefault("stem_ids", [])
    # The library helper exposes absolute filesystem paths for audio/art
    # so callers inside the server can resolve them. Strip these before
    # the meta enters the API/DB cache — `/api/song/{filename}` returns
    # the dict directly on a cache miss, which would otherwise leak
    # `/home/<user>/...` paths to the frontend.
    meta.pop("audio_path", None)
    meta.pop("art_path", None)
    return meta


def _extract_meta_for_file(psarc_path: Path, dlc_root=None) -> dict:
    """Extract metadata — dispatches on extension; PSARC path tries fast then falls back.

    `dlc_root` is only consulted for loose-folder songs (for dlc-relative
    artist/album inference). It may be a `Path`, `None`, or a zero-arg
    callable returning `Path | None`; the callable is invoked lazily, only
    on the loose-folder branch, so PSARC/sloppak extraction never triggers
    a (potentially disk-reading) DLC-root lookup. The background scan passes
    the root it already resolved; in-process callers can pass the resolver
    itself (e.g. `_get_dlc_dir`) to keep the lookup lazy.
    """
    # Sloppak is detected by `.sloppak` suffix only (cheap), so check it
    # first — that way a user's loose folder named `foo.sloppak` still wins
    # the sloppak branch instead of being misclassified.
    if sloppak_mod.is_sloppak(psarc_path):
        return _extract_meta_sloppak(psarc_path)
    if loosefolder_mod.is_loose_song(psarc_path):
        root = dlc_root() if callable(dlc_root) else dlc_root
        return _extract_meta_loosefolder(psarc_path, root)
    try:
        meta = _extract_meta_fast(psarc_path)
        if meta["title"]:
            return meta
    except Exception:
        pass
    # Fallback: full extraction (handles SNG-only official DLC etc.)
    tmp = tempfile.mkdtemp(prefix="rs_scan_")
    try:
        unpack_psarc(str(psarc_path), tmp)
        song = load_song(tmp)
        tuning = "E Standard"
        tuning_offsets: list[int] = [0] * 6
        if song.arrangements and song.arrangements[0].tuning:
            tuning_offsets = list(song.arrangements[0].tuning)
            tuning = tuning_name(tuning_offsets)
        # Order arrangements Lead > Combo > Rhythm > Bass to match the fast
        # path (_extract_meta_fast), so `index`/`smart_name` are consistent
        # regardless of which extraction path a song takes.
        priority = {"Lead": 0, "Combo": 1, "Rhythm": 2, "Bass": 3}
        sorted_arrangements = sorted(
            song.arrangements, key=lambda a: priority.get(a.name, 99),
        )
        _fb_smart = compute_smart_names(sorted_arrangements)
        arrangements = [
            {
                "index": i, "name": a.name,
                "notes": len(a.notes) + sum(len(c.notes) for c in a.chords),
                "smart_name": _fb_smart[i],
            }
            for i, a in enumerate(sorted_arrangements)
        ]
        has_lyrics = False
        for xf in Path(tmp).rglob("*.xml"):
            try:
                if ET.parse(str(xf)).getroot().tag == "vocals":
                    has_lyrics = True
                    break
            except Exception:
                pass
        return {
            "title": song.title, "artist": song.artist,
            "album": song.album, "year": str(song.year) if song.year else "",
            "duration": song.song_length, "tuning": tuning,
            "arrangements": arrangements, "has_lyrics": has_lyrics,
            "stem_ids": [],
            "tuning_name": tuning,
            "tuning_sort_key": sum(tuning_offsets),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _scan_one(item):
    """Process-pool worker: extract metadata for one library item.

    Top-level (and in this side-effect-free module) so ProcessPoolExecutor
    can pickle it by reference and the spawned worker can import it without
    pulling in server.py. `dlc` travels through the tuple rather than being
    captured from a closure so it survives pickling.
    """
    f, mtime, size, dlc = item
    log.debug("scanning %s", f.name)
    meta = _extract_meta_for_file(f, dlc)
    return _relpath(f, dlc), mtime, size, meta
