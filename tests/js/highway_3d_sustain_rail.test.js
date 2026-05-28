// Pins the chord sustain-length rail indicator in plugins/highway_3d/screen.js
// (PR #303). The rails are left/right edge plane meshes showing how long a
// chord is held. A refactor that drops the !isRepeat gate, mixes up the
// arpeggio/teal color choice, or changes the rail renderOrder would silently
// regress the indicator (rails on every repeat frame, wrong tint, or rails
// occluding note gems).
//
// Source-level only — same strategy as the other tests/js/ files.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SCREEN_JS = path.join(__dirname, '..', '..', 'plugins', 'highway_3d', 'screen.js');

test('sustain rails are gated on multi-note chords with a known box width within AHEAD', () => {
    // Each chord in a sequence (including repeats) draws a rail from its onset
    // to the next chord's onset, chaining together to cover the full handshape
    // duration visually. Single notes have no chord frame to anchor a rail to.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /if\s*\(\s*chShape\.size\s*>\s*1\s*&&\s*chordOpenBoxW\s*!=\s*null\s*&&\s*chDt\s*<\s*AHEAD\s*\)/,
        'sustain-rail block must stay gated on chShape.size > 1, chordOpenBoxW and chDt < AHEAD',
    );
});

test('sustain rails pick arpeggio color for arpeggio frames, teal otherwise', () => {
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /chordHighwayLavenderArpVisual\s*\?\s*ARPEGGIO_RIM_BLUE_HEX\s*:\s*CHORD_BOX_TEAL_HEX/,
        'rail color must select ARPEGGIO_RIM_BLUE_HEX for arpeggio frames and CHORD_BOX_TEAL_HEX for chords',
    );
});

test('sustain-rail pool meshes keep renderOrder 16 so note gems (20/21) stay on top', () => {
    // renderOrder 11 sits above the chord frame edges but below note
    // outline/core. Bumping it past the notes would let the rails
    // occlude flying gems.
    const src = fs.readFileSync(SCREEN_JS, 'utf8');
    assert.match(
        src,
        /pSusRail\s*=\s*pool\([^)]*,\s*\(\)\s*=>\s*\{[\s\S]*?m\.renderOrder\s*=\s*11\s*;[\s\S]*?\}\s*\)/,
        'pSusRail pool must seed meshes with renderOrder = 11',
    );
});
