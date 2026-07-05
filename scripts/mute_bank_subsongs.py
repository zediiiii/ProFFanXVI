"""
Surgically mute specific subsongs inside a multi-stream SAB "bank" (e.g. the
combat-vocalization banks under sound/voice/battle/, which pack dozens of
grunts/callouts each). Silences only the named subsongs; every other subsong
is copied byte-for-byte, so non-profane grunts are untouched.

Layout (per bank), confirmed against vgmstream's sqex_sead parser:
  header | snd/seq/trk sections | mtrl section [ offset-table | entry blocks ]
Each entry block = 0x20 mtrl header + 0x10 extradata prefix + HCA subfile,
padded to 0x10. snd/seq/trk reference subsongs by INDEX (not byte offset), so
re-sizing a muted subsong only requires: patch that entry's stream_size, shift
later blocks, patch the mtrl offset-table, patch total file_size.

Validated by decoding every subsong before/after: targets go silent, all
others stay bit-identical, and vgmstream still parses all streams.
"""
import argparse
import os
import struct
import subprocess
import sys
import wave
import array
from pathlib import Path


def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def align16(x): return (x + 0x0f) & ~0x0f


def parse_entries(data):
    assert data[:4] == b'sabf', "not a sabf bank"
    fn = data[0x09] or 0x0f
    sections = align16(0x10 + fn + 1)
    assert data[sections:sections+4] == b'snd ', "not a sab (no snd section)"
    mtrl_sec = u32(data, sections + 0x38)
    assert data[sections+0x30:sections+0x34] == b'mtrl'
    n = u16(data, mtrl_sec + 0x04)
    entries = []
    for i in range(n):
        rel = u32(data, mtrl_sec + 0x10 + i * 4)
        mtrl_off = rel + mtrl_sec
        extradata_size = u32(data, mtrl_off + 0x14)
        stream_size = u32(data, mtrl_off + 0x18)
        codec = data[mtrl_off + 0x05]
        subfile_off = mtrl_off + 0x20 + 0x10
        subfile_size = stream_size + extradata_size - 0x10
        entries.append(dict(index=i, mtrl_off=mtrl_off, extradata_size=extradata_size,
                            stream_size=stream_size, codec=codec,
                            subfile_off=subfile_off, subfile_size=subfile_size))
    return sections, mtrl_sec, n, entries


def reencode_silence(hca_bytes, work, vgaudio):
    """Decode an HCA subfile, mute it fully, re-encode to HCA. Returns new bytes."""
    hin = work / "s_in.hca"; wav = work / "s.wav"; hout = work / "s_out.hca"
    hin.write_bytes(hca_bytes)
    r = subprocess.run([vgaudio, str(hin), str(wav)], capture_output=True, text=True)
    if not wav.exists():
        raise RuntimeError(f"decode failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    with wave.open(str(wav), 'rb') as w:
        p = w.getparams(); n = w.getnframes()
    silent = b'\x00' * (p.sampwidth * p.nchannels * n)
    with wave.open(str(wav), 'wb') as w:
        w.setparams(p); w.writeframes(silent)
    r = subprocess.run([vgaudio, str(wav), str(hout), "--no-loop"], capture_output=True, text=True)
    if not hout.exists():
        raise RuntimeError(f"encode failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    return hout.read_bytes()


def mute_bank(in_path, out_path, target_subsongs, vgaudio, work):
    """target_subsongs: 1-based subsong indices to silence."""
    data = bytearray(Path(in_path).read_bytes())
    sections, mtrl_sec, n, entries = parse_entries(data)
    targets0 = set(s - 1 for s in target_subsongs)

    order = sorted(entries, key=lambda e: e['mtrl_off'])
    for k, e in enumerate(order):
        e['block_end'] = order[k+1]['mtrl_off'] if k + 1 < len(order) else len(data)

    first_off = order[0]['mtrl_off']
    out = bytearray(data[:first_off])          # header + sections + mtrl offset-table (patched later)
    new_offsets = {}                            # index -> new relative offset
    for e in order:
        idx = e['index']
        new_mtrl_off = len(out)
        new_offsets[idx] = new_mtrl_off - mtrl_sec
        if idx in targets0:
            hca = data[e['subfile_off']:e['subfile_off'] + e['subfile_size']]
            new_hca = reencode_silence(bytes(hca), work, vgaudio)
            new_stream_size = len(new_hca) - e['extradata_size'] + 0x10
            block = bytearray(data[e['mtrl_off']:e['mtrl_off'] + 0x30]) + new_hca
            struct.pack_into('<I', block, 0x18, new_stream_size)   # patch stream_size in mtrl header
            block += b'\x00' * (align16(len(block)) - len(block))
            out += block
        else:
            out += data[e['mtrl_off']:e['block_end']]              # verbatim (keeps padding)

    # patch offset table
    for idx, rel in new_offsets.items():
        struct.pack_into('<I', out, mtrl_sec + 0x10 + idx * 4, rel)
    # patch total file size
    struct.pack_into('<I', out, 0x0c, len(out))
    Path(out_path).write_bytes(out)
    return len(target_subsongs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("subsongs", help="comma-separated 1-based subsong indices to mute")
    ap.add_argument("--vgaudio", default=os.environ.get("VGAUDIOCLI", "VGAudioCli.exe"))
    args = ap.parse_args()
    work = Path(os.environ.get("BANK_WORK", "_bankwork")); work.mkdir(exist_ok=True)
    subs = [int(x) for x in args.subsongs.split(",") if x.strip()]
    n = mute_bank(args.input, args.output, subs, args.vgaudio, work)
    print(f"Muted {n} subsong(s) {subs} in {Path(args.output).name}")


if __name__ == "__main__":
    main()
