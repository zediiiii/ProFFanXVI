import struct
import sys
import subprocess
import wave
import array
import os

def u8(b, o): return b[o]
def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def u32(b, o): return struct.unpack_from('<I', b, o)[0]

def align16(x):
    return (x + 0x0f) & ~0x0f

def parse_sab(data):
    assert data[0:4] == b'sabf', "not a sabf file"
    file_size = u32(data, 0x0c)
    assert file_size == len(data), f"file_size mismatch: header says {file_size}, actual {len(data)}"

    filename_size = u8(data, 0x09)
    if filename_size == 0:
        filename_size = 0x0f
    filename_offset = 0x10
    sections_offset = align16(filename_offset + (filename_size + 1))

    assert data[sections_offset:sections_offset+4] == b'snd ', "snd section not found"
    snd_off = u32(data, sections_offset + 0x08)
    assert data[sections_offset+0x10:sections_offset+0x14] == b'seq ', "seq section not found"
    seq_off = u32(data, sections_offset + 0x18)
    assert data[sections_offset+0x20:sections_offset+0x24] == b'trk ', "trk section not found"
    trk_off = u32(data, sections_offset + 0x28)
    assert data[sections_offset+0x30:sections_offset+0x34] == b'mtrl', "mtrl section not found"
    mtrl_section_offset = u32(data, sections_offset + 0x38)

    mtrl_entries = u16(data, mtrl_section_offset + 0x04)
    mtrl_offset = None
    for i in range(mtrl_entries):
        off = u32(data, mtrl_section_offset + 0x10 + i * 4)
        if off >= file_size:
            continue
        off += mtrl_section_offset
        mtrl_offset = off
        break  # first subsong only, matches our single-line sample files

    assert mtrl_offset is not None, "no subsongs found"

    channels = u8(data, mtrl_offset + 0x04)
    codec = u8(data, mtrl_offset + 0x05)
    sample_rate = u32(data, mtrl_offset + 0x08)
    loop_start = u32(data, mtrl_offset + 0x0c)
    loop_end = u32(data, mtrl_offset + 0x10)
    extradata_size = u32(data, mtrl_offset + 0x14)
    stream_size = u32(data, mtrl_offset + 0x18)
    extradata_id = u16(data, mtrl_offset + 0x1c)
    extradata_offset = mtrl_offset + 0x20

    assert codec == 0x07, f"only HCA-subfile codec (0x07) supported by this script, got {codec:#x}"

    subfile_offset = extradata_offset + 0x10
    subfile_size = stream_size + extradata_size - 0x10

    return {
        'file_size': file_size,
        'channels': channels,
        'codec': codec,
        'sample_rate': sample_rate,
        'loop_start': loop_start,
        'loop_end': loop_end,
        'extradata_size': extradata_size,
        'stream_size': stream_size,
        'mtrl_offset': mtrl_offset,
        'subfile_offset': subfile_offset,
        'subfile_size': subfile_size,
    }


def run(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout)
        print("STDERR:", r.stderr)
        raise RuntimeError(f"command failed: {args}")
    return r


def mute_wav_fully(wav_path):
    """Zero out all audio samples in place, preserving format/length exactly."""
    with wave.open(wav_path, 'rb') as w:
        params = w.getparams()
        n_frames = w.getnframes()
    silence = b'\x00' * (params.sampwidth * params.nchannels * n_frames)
    with wave.open(wav_path, 'wb') as w:
        w.setparams(params)
        w.writeframes(silence)


def mute_wav_range(wav_path, start_sec, end_sec):
    """Zero out samples in [start_sec, end_sec), preserving everything else and
    the total length exactly."""
    with wave.open(wav_path, 'rb') as w:
        params = w.getparams()
        n_frames = w.getnframes()
        frames = w.readframes(n_frames)

    sw = params.sampwidth
    ch = params.nchannels
    sr = params.framerate
    typecode = {2: 'h', 4: 'f'}[sw]

    arr = array.array(typecode)
    arr.frombytes(frames)

    start_sample = max(0, int(start_sec * sr)) * ch
    end_sample = min(len(arr), int(end_sec * sr) * ch)
    for i in range(start_sample, end_sample):
        arr[i] = 0

    with wave.open(wav_path, 'wb') as w:
        w.setparams(params)
        w.writeframes(arr.tobytes())


def main():
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    # tool locations: env override first (set by the pipeline/GUI), else look
    # next to this script (works when the script lives inside the tools folder).
    vgaudiocli = os.environ.get("VGAUDIOCLI") or os.path.join(tools_dir, "VGAudioCli.exe")
    vgmstream_cli = os.environ.get("VGMSTREAM_CLI") or os.path.join(tools_dir, "vgmstream", "vgmstream-cli.exe")

    in_path = sys.argv[1]
    out_path = sys.argv[2]
    mute_start = float(sys.argv[3]) if len(sys.argv) > 3 else None
    mute_end = float(sys.argv[4]) if len(sys.argv) > 4 else None

    data = bytearray(open(in_path, 'rb').read())
    info = parse_sab(data)
    print("Parsed SAB:", {k: v for k, v in info.items()})

    assert info['subfile_offset'] + info['subfile_size'] == info['file_size'], \
        "this sample has trailing data after the audio subfile -- multi-stream patching not implemented"

    hca_bytes = bytes(data[info['subfile_offset']: info['subfile_offset'] + info['subfile_size']])
    assert hca_bytes[0:4] in (b'HCA\x00', b'\x00\x00\x00\x00') or hca_bytes[0:3] == b'HCA', \
        f"expected HCA magic at subfile start, got {hca_bytes[0:4]!r}"

    work = os.path.join(tools_dir, "_sabwork")
    os.makedirs(work, exist_ok=True)
    hca_in = os.path.join(work, "orig.hca")
    wav_path = os.path.join(work, "orig.wav")
    hca_out = os.path.join(work, "muted.hca")

    open(hca_in, 'wb').write(hca_bytes)

    print("Decoding HCA -> WAV via VGAudioCli...")
    run(vgaudiocli, hca_in, wav_path)

    if mute_start is not None and mute_end is not None:
        print(f"Muting range {mute_start:.3f} - {mute_end:.3f}...")
        mute_wav_range(wav_path, mute_start, mute_end)
    else:
        print("Muting entire clip...")
        mute_wav_fully(wav_path)

    print("Re-encoding WAV -> HCA via VGAudioCli (matching original bitrate)...")
    # 88000 bps observed for our 44.1kHz mono sample via vgmstream -m
    run(vgaudiocli, wav_path, hca_out, "--bitrate", "88000", "--no-loop")

    new_hca = open(hca_out, 'rb').read()
    print(f"Original HCA size: {len(hca_bytes)}  New HCA size: {len(new_hca)}")

    # stream_size excludes both the 0x10 SEAD extradata prefix AND the HCA's own internal
    # header (whose length is folded into extradata_size): subfile_size = stream_size + extradata_size - 0x10
    # Assuming the re-encoded HCA's own header length is unchanged, extradata_size stays the same.
    new_extradata_size = info['extradata_size']
    new_stream_size = len(new_hca) - new_extradata_size + 0x10

    new_data = bytearray(data[0:info['subfile_offset']]) + bytearray(new_hca)
    # patch stream_size field
    struct.pack_into('<I', new_data, info['mtrl_offset'] + 0x18, new_stream_size)
    struct.pack_into('<I', new_data, info['mtrl_offset'] + 0x14, new_extradata_size)
    # patch total file_size field
    struct.pack_into('<I', new_data, 0x0c, len(new_data))

    open(out_path, 'wb').write(new_data)
    print(f"Wrote patched SAB: {out_path} ({len(new_data)} bytes)")

    print("\n--- Verifying with vgmstream-cli ---")
    run(vgmstream_cli, "-m", out_path)


if __name__ == "__main__":
    main()
