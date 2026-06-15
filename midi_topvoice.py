#!/usr/bin/env python3
"""
midi_topvoice.py — extract the monophonic top-note melody from a MIDI file and
write it back out as a single-track MIDI, so you can listen to exactly what the
slicer reduces the polyphony to. Preserves the source division + tempo.

Pure stdlib.  Usage: python3 midi_topvoice.py [in.mid] [out.mid]
"""
import struct
import sys
from collections import defaultdict

IN_PATH  = sys.argv[1] if len(sys.argv) > 1 else "zelda-overworld1.mid"
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else "top_voice.mid"


def parse(path):
    d = open(path, "rb").read()
    assert d[:4] == b"MThd"
    _, fmt, ntrk, div = struct.unpack(">IHHH", d[4:14])
    pos = 14
    note_events = []   # (tick, +1/-1, note)
    tempo = None
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
            if status == 0xFF:
                mtype = d[pos]; pos += 1
                mlen = 0
                while True:
                    bb = d[pos]; pos += 1
                    mlen = (mlen << 7) | (bb & 0x7F)
                    if not (bb & 0x80):
                        break
                if mtype == 0x51 and tempo is None:
                    tempo = struct.unpack(">I", b"\x00" + d[pos:pos + 3])[0]
                pos += mlen
            elif status in (0xF0, 0xF7):
                slen = 0
                while True:
                    bb = d[pos]; pos += 1
                    slen = (slen << 7) | (bb & 0x7F)
                    if not (bb & 0x80):
                        break
                pos += slen
            else:
                hi = status & 0xF0
                if hi in (0x80, 0x90):
                    note = d[pos]; vel = d[pos + 1]; pos += 2
                    note_events.append((tick, +1 if (hi == 0x90 and vel > 0) else -1, note))
                elif hi in (0xA0, 0xB0, 0xE0):
                    pos += 2
                elif hi in (0xC0, 0xD0):
                    pos += 1
                else:
                    pos += 1
        pos = end
    return div, tempo or 500000, note_events


def top_voice(note_events):
    """Sweep events -> list of (note, start_tick, end_tick) for the highest pitch."""
    note_events.sort(key=lambda e: e[0])
    active = defaultdict(int)
    segs = []
    cur_top = None
    last_tick = 0
    for tick, delta, note in note_events:
        if tick > last_tick:
            if cur_top is not None:
                segs.append((cur_top, last_tick, tick))
            last_tick = tick
        active[note] += delta
        if active[note] <= 0:
            active.pop(note, None)
        cur_top = max(active.keys(), default=None)
    # merge adjacent same-pitch segments
    merged = []
    for n, t0, t1 in segs:
        if merged and merged[-1][0] == n and merged[-1][2] == t0:
            merged[-1] = (n, merged[-1][1], t1)
        else:
            merged.append((n, t0, t1))
    return merged


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def write_midi(path, div, tempo, segs, velocity=100):
    # event stream: (tick, kind, note); kind 0=off sorts before 1=on at same tick
    evs = []
    for n, t0, t1 in segs:
        evs.append((t0, 1, n))
        evs.append((t1, 0, n))
    evs.sort(key=lambda e: (e[0], e[1]))

    body = bytearray()
    body += b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big")   # tempo
    body += b"\x00\xc0\x00"                                  # program 0 (piano)
    last = 0
    for tick, kind, note in evs:
        body += vlq(tick - last)
        last = tick
        if kind == 1:
            body += bytes((0x90, note, velocity))
        else:
            body += bytes((0x80, note, 0))
    body += b"\x00\xff\x2f\x00"                              # end of track

    track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, div)
    open(path, "wb").write(header + track)


def main():
    div, tempo, note_events = parse(IN_PATH)
    segs = top_voice(note_events)
    write_midi(OUT_PATH, div, tempo, segs)

    bpm = round(60_000_000 / tempo)
    notes = [n for n, _, _ in segs]
    NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    name = lambda n: f"{NAMES[n % 12]}{n // 12 - 1}"
    print(f"Source       : {IN_PATH}  ({div} ticks/quarter, {bpm} BPM)")
    print(f"Top voice    : {len(segs)} notes, range {name(min(notes))}..{name(max(notes))}")
    print(f"First 16     : {' '.join(name(n) for n in notes[:16])}")
    print(f"Wrote        : {OUT_PATH}")


if __name__ == "__main__":
    main()
