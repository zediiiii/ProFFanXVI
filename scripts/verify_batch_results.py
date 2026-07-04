"""
Automated QA pass over batch_mute_pipeline.py's output: for every result,
decode both the original (extracted) and muted .sab and check via RMS that:
  - word_level results: the reported [start,end] region actually dropped in
    energy (the mute took effect), AND overall duration is unchanged.
  - whole_line_fallback results: the whole clip is silent, duration unchanged.
Flags anything that doesn't hold so a human knows exactly which lines need a
manual listen-check first, instead of trusting the pipeline's own self-report.
"""
import json
import subprocess
import sys
import wave
import array
from pathlib import Path

TOOLS_DIR = Path(__file__).parent
VGMSTREAM_CLI = TOOLS_DIR / "vgmstream" / "vgmstream-cli.exe"


def decode(sab_path, wav_path):
    r = subprocess.run([str(VGMSTREAM_CLI), "-o", str(wav_path), str(sab_path)],
                        capture_output=True, text=True)
    return r.returncode == 0 and Path(wav_path).exists()


def read_samples(wav_path):
    w = wave.open(str(wav_path), 'rb')
    n = w.getnframes()
    frames = w.readframes(n)
    arr = array.array('h')
    arr.frombytes(frames)
    return w.getframerate(), n, arr


def region_rms(arr, sr, start, end):
    s, e = int(start * sr), int(end * sr)
    chunk = arr[s:e]
    if not chunk:
        return 0.0
    return (sum(x * x for x in chunk) / len(chunk)) ** 0.5


def overall_rms(arr):
    if not arr:
        return 0.0
    return (sum(x * x for x in arr) / len(arr)) ** 0.5


def main():
    report_path = sys.argv[1] if len(sys.argv) > 1 else "batch_pipeline_report.json"
    extract_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else TOOLS_DIR / "batch_extracted"
    mod_data_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else (
        TOOLS_DIR / "ReloadedII" / "Mods" / "ff16.audio.profanity-filter" / "FFXVI" / "data"
    )

    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    work = TOOLS_DIR / "_verifywork"
    work.mkdir(exist_ok=True)

    flagged = []
    checked = 0
    for r in report["results"]:
        voice_path = r["voice_sound_path"]
        assert voice_path.endswith(".sab")
        internal_path = voice_path[:-4] + ".en.sab"
        orig_sab = extract_dir / internal_path
        muted_sab = mod_data_dir / internal_path
        if not orig_sab.exists() or not muted_sab.exists():
            flagged.append({**r, "problem": "missing_file"})
            continue

        orig_wav = work / "o.wav"
        muted_wav = work / "m.wav"
        if not decode(orig_sab, orig_wav) or not decode(muted_sab, muted_wav):
            flagged.append({**r, "problem": "decode_failed"})
            continue

        sr_o, n_o, arr_o = read_samples(orig_wav)
        sr_m, n_m, arr_m = read_samples(muted_wav)
        checked += 1

        if n_o != n_m:
            flagged.append({**r, "problem": f"duration_mismatch orig={n_o} muted={n_m}"})
            continue

        if r["method"] == "whole_line_fallback":
            m_rms = overall_rms(arr_m)
            if m_rms > 5.0:
                flagged.append({**r, "problem": f"fallback_not_silent rms={m_rms:.1f}"})
        else:
            start, end = r["start"], r["end"]
            orig_region_rms = region_rms(arr_o, sr_o, start, end)
            muted_region_rms = region_rms(arr_m, sr_m, start, end)
            if orig_region_rms > 50 and muted_region_rms > orig_region_rms * 0.15:
                flagged.append({**r, "problem": f"target_not_silenced orig_rms={orig_region_rms:.1f} muted_rms={muted_region_rms:.1f}"})

    print(f"Checked {checked} results, {len(flagged)} flagged for manual review")
    out = TOOLS_DIR / "batch_qa_flagged.json"
    out.write_text(json.dumps(flagged, indent=2), encoding="utf-8")
    print(f"Flagged details written to {out}")


if __name__ == "__main__":
    main()
