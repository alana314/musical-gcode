#!/usr/bin/env python3
"""
musical_slicer_printrbot.py — slice an STL and emit G-code whose stepper-motor
tones play a MIDI melody while printing.

MACHINE PROFILE: Printrbot Plus (Printrboard, soldered A4988-class drivers).
A4988s have no silent mode — they whine audibly at their step rate, which is
exactly what this hack needs. A stepper at v mm/s sings at STEPS_PER_MM*v Hz;
to play note frequency f we command v = f / STEPS_PER_MM mm/s. We walk the
print's toolpath and stamp each sub-segment with the feedrate of the note that
should be sounding, looping the melody for the whole print.

Profile taken from the user's M503 dump:
  M92  X80.16 Y80.65   -> ~80.4 steps/mm (≈ in tune; 0.6% X/Y split is inaudible)
  M211 X195 Y220       -> build area
  M201 X2000 Y2000     -> max accel
  M205 X20             -> XY jerk (single-axis form; this is old Marlin V1)
  M203 Z2.00           -> Z capped at 2 mm/s, so Z moves use F120
  (no bed PID in dump) -> assume NO heated bed; omit M140/M190

Pure stdlib.
"""

import math
import struct
from collections import defaultdict

# ----------------------------------------------------------------------------
# Config — Printrbot Plus / PLA
# ----------------------------------------------------------------------------
STL_PATH      = "triforce.stl"
MID_PATH      = "zelda-simple.mid"
OUT_PATH      = "triforce_musical_printrbot.gcode"

LAYER_HEIGHT  = 0.2          # mm
LINE_WIDTH    = 0.4          # mm
FILAMENT_DIA  = 1.75         # mm
STEPS_X       = 80.16        # M92 X — used to tune tones on X-dominant moves
STEPS_Y       = 80.65        # M92 Y — used to tune tones on Y-dominant moves
STEPS_PER_MM  = 80.4         # avg, used only for octave-folding decisions

BED_X, BED_Y  = 195.0, 220.0 # Printrbot usable area (M211); model gets centered
NOZZLE_TEMP   = 200          # PLA
HAS_HEATED_BED = True        # Printrbot Plus v2.1 has a heated bed
BED_TEMP      = 60           # PLA

MAX_ACCEL     = 2000         # M201 machine max
Z_FEED        = 120          # mm/min == 2 mm/s, the Z max feedrate (M203 Z2.00)

# Musical speed window (mm/s). Notes outside get octave-folded to stay here,
# which keeps them audible/printable while preserving pitch class.
MIN_V         = 4.0
MAX_V         = 40.0         # well under the 100 mm/s machine max (M203 X/Y)
REST_V        = 30.0         # speed used during rests (still moves, just no target tone)

FIL_AREA  = math.pi * (FILAMENT_DIA / 2.0) ** 2
E_PER_MM  = LINE_WIDTH * LAYER_HEIGHT / FIL_AREA

# ----------------------------------------------------------------------------
# STL (binary) parser
# ----------------------------------------------------------------------------
def load_stl(path):
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    tris = []
    off = 84
    for _ in range(n):
        vals = struct.unpack("<12f", data[off:off + 48])
        v = [(vals[3], vals[4], vals[5]),
             (vals[6], vals[7], vals[8]),
             (vals[9], vals[10], vals[11])]
        tris.append(v)
        off += 50  # 48 + 2-byte attribute
    return tris

# ----------------------------------------------------------------------------
# Slicer: plane-mesh intersection -> stitched loops
# ----------------------------------------------------------------------------
def slice_triangle(tri, z):
    pts = []
    for i in range(3):
        p, q = tri[i], tri[(i + 1) % 3]
        zp, zq = p[2] - z, q[2] - z
        if (zp < 0 and zq > 0) or (zp > 0 and zq < 0):
            t = zp / (zp - zq)
            pts.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return (pts[0], pts[1]) if len(pts) == 2 else None

def stitch_loops(segments, tol=1e-3):
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    adj = defaultdict(list)
    for i, (a, b) in enumerate(segments):
        adj[key(a)].append(i)
        adj[key(b)].append(i)
    used = [False] * len(segments)
    loops = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        a, b = segments[start]
        loop = [a, b]
        cur = b
        while True:
            nxt = None
            for j in adj[key(cur)]:
                if not used[j]:
                    nxt = j
                    break
            if nxt is None:
                break
            used[nxt] = True
            sa, sb = segments[nxt]
            cur = sb if key(sa) == key(cur) else sa
            loop.append(cur)
            if key(cur) == key(loop[0]):
                break
        if len(loop) >= 3:
            loops.append(loop)
    return loops

def slice_mesh(tris):
    zs = [v[2] for t in tris for v in t]
    zmin, zmax = min(zs), max(zs)
    layers = []
    n = max(1, int(math.ceil((zmax - zmin) / LAYER_HEIGHT)))
    for i in range(n):
        z = zmin + (i + 0.5) * LAYER_HEIGHT   # mid-layer sample dodges vertices
        if z >= zmax:
            break
        segs = [s for s in (slice_triangle(t, z) for t in tris) if s]
        loops = stitch_loops(segs)
        if loops:
            layers.append((zmin + (i + 1) * LAYER_HEIGHT, loops))  # print height
    return layers, (zmin, zmax)

# ----------------------------------------------------------------------------
# MIDI parser -> monophonic top-note melody as (freq|None, duration_s)
# ----------------------------------------------------------------------------
def load_midi(path):
    d = open(path, "rb").read()
    assert d[:4] == b"MThd"
    _, fmt, ntrk, div = struct.unpack(">IHHH", d[4:14])
    pos = 14
    note_events = []   # (tick, +1/-1, note)
    tempo_events = []  # (tick, us_per_quarter)
    for _ in range(ntrk):
        assert d[pos:pos + 4] == b"MTrk"
        length = struct.unpack(">I", d[pos + 4:pos + 8])[0]
        pos += 8
        end = pos + length
        tick = 0
        status = 0
        while pos < end:
            dt = 0
            while True:
                b = d[pos]; pos += 1
                dt = (dt << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            tick += dt
            b = d[pos]
            if b & 0x80:
                status = b; pos += 1
            if status == 0xFF:               # meta
                mtype = d[pos]; pos += 1
                mlen = 0
                while True:
                    bb = d[pos]; pos += 1
                    mlen = (mlen << 7) | (bb & 0x7F)
                    if not (bb & 0x80):
                        break
                if mtype == 0x51:
                    tempo_events.append((tick, struct.unpack(">I", b"\x00" + d[pos:pos + 3])[0]))
                pos += mlen
            elif status in (0xF0, 0xF7):     # sysex
                slen = 0
                while True:
                    bb = d[pos]; pos += 1
                    slen = (slen << 7) | (bb & 0x7F)
                    if not (bb & 0x80):
                        break
                pos += slen
            else:
                hi = status & 0xF0
                if hi in (0x80, 0x90):       # note off / on
                    note = d[pos]; vel = d[pos + 1]; pos += 2
                    if hi == 0x90 and vel > 0:
                        note_events.append((tick, +1, note))
                    else:
                        note_events.append((tick, -1, note))
                elif hi in (0xA0, 0xB0, 0xE0):  # 2 data bytes
                    pos += 2
                elif hi in (0xC0, 0xD0):        # 1 data byte
                    pos += 1
                else:
                    pos += 1
        pos = end

    if not tempo_events:
        tempo_events = [(0, 500000)]
    tempo_events.sort()

    def tick_to_sec(t):
        sec = 0.0
        prev_tick, prev_tempo = 0, tempo_events[0][1]
        for ct, ctempo in tempo_events:
            if ct >= t:
                break
            sec += (ct - prev_tick) * (prev_tempo / div / 1e6)
            prev_tick, prev_tempo = ct, ctempo
        sec += (t - prev_tick) * (prev_tempo / div / 1e6)
        return sec

    note_events.sort(key=lambda e: e[0])
    active = defaultdict(int)
    melody = []
    cur_top = None
    last_tick = 0
    for tick, delta, note in note_events:
        if tick > last_tick:
            melody.append((cur_top, last_tick, tick))
            last_tick = tick
        active[note] += delta
        if active[note] <= 0:
            active.pop(note, None)
        cur_top = max(active.keys(), default=None)

    out = []
    for top, t0, t1 in melody:
        if t1 <= t0:
            continue
        dur = tick_to_sec(t1) - tick_to_sec(t0)
        if dur <= 0:
            continue
        freq = 440.0 * 2 ** ((top - 69) / 12.0) if top is not None else None
        if out and ((out[-1][0] is None) == (freq is None)) and \
           (freq is None or abs(out[-1][0] - freq) < 0.1):
            out[-1] = (out[-1][0], out[-1][1] + dur)
        else:
            out.append((freq, dur))
    return out

# ----------------------------------------------------------------------------
# Map melody onto the toolpath
# ----------------------------------------------------------------------------
def fold_speed(freq):
    if freq is None:
        return REST_V
    v = freq / STEPS_PER_MM
    while v < MIN_V:
        v *= 2
    while v > MAX_V:
        v /= 2
    return v

def build_song_stream(melody):
    """List of (duration_s, folded_freq_Hz | None). None == rest."""
    stream = []
    for freq, dur in melody:
        if freq is None:
            stream.append((dur, None))
        else:
            v = fold_speed(freq)             # octave-folded dominant-axis speed
            stream.append((dur, v * STEPS_PER_MM))   # store the folded frequency
    return stream

def flatten_moves(layers):
    moves = []
    cur = None
    for z, loops in layers:
        for loop in loops:
            need_travel = cur is None or \
                abs(cur[0] - loop[0][0]) > 1e-6 or abs(cur[1] - loop[0][1]) > 1e-6
            if need_travel:
                moves.append({"x": loop[0][0], "y": loop[0][1], "z": z, "ext": False})
            for p in loop[1:]:
                moves.append({"x": p[0], "y": p[1], "z": z, "ext": True})
            cur = loop[-1]
    return moves

def center_offset(layers):
    xs = [p[0] for _, loops in layers for loop in loops for p in loop]
    ys = [p[1] for _, loops in layers for loop in loops for p in loop]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    return BED_X / 2 - cx, BED_Y / 2 - cy, (max(xs) - min(xs), max(ys) - min(ys))

# ----------------------------------------------------------------------------
# G-code emitter — Printrbot Plus / old Marlin V1
# ----------------------------------------------------------------------------
def gcode_header():
    bed = (f"M140 S{BED_TEMP}\nM190 S{BED_TEMP}\n" if HAS_HEATED_BED else
           "; (no heated bed on this machine)\n")
    return f"""; musical_slicer_printrbot.py — Printrbot Plus (A4988 drivers, PLA)
; steppers play {MID_PATH} while printing {STL_PATH}
M201 X{MAX_ACCEL} Y{MAX_ACCEL}   ; accel = machine max
M204 S3000
M205 X20.00                ; XY jerk (single-axis form for old Marlin V1)
{bed}M104 S{NOZZLE_TEMP}
M109 S{NOZZLE_TEMP}
M107                       ; fan off — let the music be heard
G28
G92 E0
G1 Z2.0 F{Z_FEED}
G1 X5 Y20 Z0.3 F3000
G1 X5 Y200 Z0.3 F1200 E15  ; prime line
G1 X5.4 Y200 Z0.3 F3000
G1 X5.4 Y20 Z0.3 F1200 E30
G92 E0
G1 Z2.0 F{Z_FEED}
"""

def gcode_footer():
    bedoff = "M140 S0\n" if HAS_HEATED_BED else ""
    return f"""
G91
G1 E-3 F1800
G1 Z10 F{Z_FEED}
G90
G1 X0 Y220 F3000
M104 S0
{bedoff}M107
M84
; end
"""

def emit(layers, melody):
    ox, oy, size = center_offset(layers)
    moves = flatten_moves(layers)
    stream = build_song_stream(melody)
    if not stream:
        stream = [(1e9, REST_V * 60.0)]

    lines = [gcode_header()]
    e = 0.0
    cz = None
    px, py = 5.4, 20.0           # where prime line left us
    si = 0
    note_left, note_freq = stream[si]   # seconds remaining, folded freq | None
    total_len = 0.0
    total_time = 0.0

    for mv in moves:
        tx = mv["x"] + ox
        ty = mv["y"] + oy
        if cz != mv["z"]:
            cz = mv["z"]
            lines.append(f"G1 Z{cz:.3f} F{Z_FEED}")
        seg_len = math.hypot(tx - px, ty - py)
        if seg_len < 1e-9:
            px, py = tx, ty
            continue
        dx, dy = (tx - px) / seg_len, (ty - py) / seg_len
        maxc = max(abs(dx), abs(dy))         # 0.707..1.0; the dominant (loudest) axis
        pos = 0.0
        while pos < seg_len - 1e-9:
            if note_freq is None:
                fv = REST_V                  # rest: plain vector speed, no target pitch
            else:
                steps = STEPS_X if abs(dx) >= abs(dy) else STEPS_Y
                fv = (note_freq / steps) / maxc   # vector speed so the dominant axis hits the pitch
            dist = note_left * fv            # path length this note still wants
            take = min(seg_len - pos, dist)
            if take < 1e-9:                  # note used up exactly at a boundary
                si = (si + 1) % len(stream)
                note_left, note_freq = stream[si]
                continue
            nx, ny = px + dx * (pos + take), py + dy * (pos + take)
            if mv["ext"]:
                e += take * E_PER_MM
                lines.append(f"G1 X{nx:.3f} Y{ny:.3f} E{e:.5f} F{fv * 60:.0f}")
            else:
                lines.append(f"G1 X{nx:.3f} Y{ny:.3f} F{fv * 60:.0f}")
            total_len += take
            total_time += take / fv
            pos += take
            note_left -= take / fv
            if note_left <= 1e-9:
                si = (si + 1) % len(stream)   # loop the song
                note_left, note_freq = stream[si]
        px, py = tx, ty

    lines.append(gcode_footer())
    return "\n".join(lines), total_len, total_time, size

# ----------------------------------------------------------------------------
def main():
    tris = load_stl(STL_PATH)
    layers, (zmin, zmax) = slice_mesh(tris)
    melody = load_midi(MID_PATH)
    gcode, total_len, total_time, size = emit(layers, melody)
    open(OUT_PATH, "w").write(gcode)

    pitched = [m for m in melody if m[0] is not None]
    speeds = [fold_speed(f) for f, _ in pitched]
    print(f"PROFILE      : Printrbot Plus, {STEPS_PER_MM} steps/mm, "
          f"{'heated bed' if HAS_HEATED_BED else 'NO heated bed'}, PLA {NOZZLE_TEMP}C")
    print(f"STL          : {len(tris)} triangles, z {zmin:.2f}..{zmax:.2f} mm")
    print(f"Model size   : {size[0]:.1f} x {size[1]:.1f} mm (centered on {BED_X:.0f}x{BED_Y:.0f} bed)")
    print(f"Layers       : {len(layers)} @ {LAYER_HEIGHT} mm")
    print(f"Melody       : {len(melody)} segments ({len(pitched)} pitched, "
          f"{len(melody) - len(pitched)} rests)")
    if speeds:
        print(f"Note speeds  : {min(speeds):.1f}..{max(speeds):.1f} mm/s "
              f"(tones {min(speeds)*STEPS_PER_MM:.0f}..{max(speeds)*STEPS_PER_MM:.0f} Hz)")
    print(f"Toolpath     : {total_len/1000:.1f} m, ~{total_time/60:.1f} min print")
    print(f"Wrote        : {OUT_PATH}")

if __name__ == "__main__":
    main()
