"""Generate MIDI and render audio from a Guitar Pro file."""

import os
import subprocess
import tempfile
from pathlib import Path

import guitarpro
from midiutil import MIDIFile

GP_TICKS_PER_QUARTER = 960

# Standard tuning MIDI values (GP string order: 1=high, 6=low)
STANDARD_6 = [64, 59, 55, 50, 45, 40]  # e B G D A E
STANDARD_4 = [43, 38, 33, 28]          # G D A E (bass)


def gp_to_midi(gp_path: str, output_midi: str, track_indices: list[int] | None = None,
               force_standard_tuning: bool = False) -> str:
    """Convert Guitar Pro file to MIDI.

    Args:
        gp_path: Path to .gp5/.gp4/.gp3 file
        output_midi: Output .mid file path
        track_indices: Which tracks to include (None = all non-percussion)
        force_standard_tuning: If True, use E standard tuning for all instruments
            (keeps fret numbers, changes the pitch of open strings)

    Returns:
        Path to the MIDI file
    """
    song = guitarpro.parse(gp_path)

    if track_indices is None:
        track_indices = list(range(len(song.tracks)))

    midi = MIDIFile(
        len(track_indices),
        ticks_per_quarternote=GP_TICKS_PER_QUARTER,
    )

    for midi_track_idx, gp_track_idx in enumerate(track_indices):
        track = song.tracks[gp_track_idx]
        is_perc = track.isPercussionTrack

        # MIDI channel: percussion must be 9, others avoid 9
        if is_perc:
            channel = 9
        else:
            channel = midi_track_idx if midi_track_idx < 9 else midi_track_idx + 1
            channel = min(channel, 15)

        midi.addTrackName(midi_track_idx, 0, track.name)
        midi.addTempo(midi_track_idx, 0, song.tempo)

        # Instrument and volume from GP channel data
        gp_ch = track.channel
        if gp_ch and not is_perc:
            midi.addProgramChange(midi_track_idx, channel, 0, gp_ch.instrument)
        elif not is_perc:
            midi.addProgramChange(midi_track_idx, channel, 0, 29)  # overdriven guitar

        # Volume (CC7) and pan (CC10)
        if gp_ch:
            vol = min(127, gp_ch.volume)
            pan = min(127, gp_ch.balance)
            midi.addControllerEvent(midi_track_idx, channel, 0, 7, vol)
            midi.addControllerEvent(midi_track_idx, channel, 0, 10, pan)

        # Tempo changes
        tempo_added = set()
        for measure in track.measures:
            for voice in measure.voices:
                for beat in voice.beats:
                    if beat.effect and beat.effect.mixTableChange:
                        mtc = beat.effect.mixTableChange
                        if mtc.tempo and mtc.tempo.value > 0:
                            tick_time = beat.start / GP_TICKS_PER_QUARTER
                            if tick_time not in tempo_added:
                                midi.addTempo(midi_track_idx, tick_time, mtc.tempo.value)
                                tempo_added.add(tick_time)

        # Notes
        for measure in track.measures:
            for voice in measure.voices:
                for beat in voice.beats:
                    if not beat.notes:
                        continue

                    beat_time = beat.start / GP_TICKS_PER_QUARTER

                    dur_quarters = 4.0 / beat.duration.value
                    if beat.duration.isDotted:
                        dur_quarters *= 1.5
                    if beat.duration.tuplet.enters > 0 and beat.duration.tuplet.times > 0:
                        dur_quarters *= beat.duration.tuplet.times / beat.duration.tuplet.enters

                    for note in beat.notes:
                        if note.type == guitarpro.NoteType.rest:
                            continue

                        if force_standard_tuning and not is_perc:
                            num_strings = len(track.strings)
                            std = STANDARD_4 if num_strings == 4 else STANDARD_6
                            string_midi = std[note.string - 1] if note.string - 1 < len(std) else track.strings[note.string - 1].value
                        else:
                            string_midi = track.strings[note.string - 1].value
                        pitch = string_midi + note.value

                        if note.type == guitarpro.NoteType.dead:
                            dur_q = 0.05
                        else:
                            dur_q = dur_quarters

                        velocity = note.velocity
                        if note.effect.ghostNote:
                            velocity = max(20, velocity // 2)

                        midi.addNote(
                            midi_track_idx, channel,
                            pitch, beat_time, dur_q, velocity,
                        )

    with open(output_midi, "wb") as f:
        midi.writeFile(f)

    return output_midi


def render_midi_to_audio(midi_path: str, output_path: str) -> str:
    """Render MIDI to OGG audio using fluidsynth."""
    sf_paths = [
        "/usr/share/soundfonts/FluidR3_GM.sf2",
        "/usr/share/soundfonts/FluidR3_GS.sf2",
        "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    ]
    soundfont = None
    for sf in sf_paths:
        if os.path.exists(sf):
            soundfont = sf
            break

    if not soundfont:
        raise RuntimeError("No soundfont found. Install: sudo pacman -S soundfont-fluid")

    wav_path = output_path + ".wav"
    ogg_path = output_path + ".ogg"

    result = subprocess.run(
        ["fluidsynth", "-ni", "-T", "wav", "-F", wav_path, "-r", "44100", soundfont, midi_path],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError(f"fluidsynth failed: {result.stderr[-300:]}")

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-q:a", "6", ogg_path],
        capture_output=True, timeout=60,
    )
    if result.returncode == 0 and os.path.exists(ogg_path):
        os.remove(wav_path)
        return ogg_path

    return wav_path


def gp_to_audio(gp_path: str, output_path: str,
                track_indices: list[int] | None = None,
                force_standard_tuning: bool = False) -> str:
    """Convert Guitar Pro file directly to audio.

    Args:
        gp_path: Path to .gp5 file
        output_path: Output audio file path (without extension)
        track_indices: Which tracks (None = all including drums)
        force_standard_tuning: Force E standard tuning (keeps frets, changes pitch)

    Returns:
        Path to the audio file
    """
    tmp_midi = tempfile.mktemp(suffix=".mid", prefix="rs_midi_")
    try:
        tuning_label = " (E Standard)" if force_standard_tuning else ""
        print(f"Generating MIDI from {Path(gp_path).name}{tuning_label}...")
        gp_to_midi(gp_path, tmp_midi, track_indices, force_standard_tuning)
        print(f"Rendering audio with FluidSynth...")
        return render_midi_to_audio(tmp_midi, output_path)
    finally:
        if os.path.exists(tmp_midi):
            os.remove(tmp_midi)
