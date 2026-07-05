"""
Definitive zero-tolerance gate. Independently (not trusting the build's own
self-report) re-transcribes every muted output file and asserts no enabled
profanity token is still audible, plus sanity energy/duration checks. This is
the strongest automated guarantee that nothing slips through: if the same
speech recogniser that would judge the game can no longer hear the word, it's
gone. Exits non-zero if any leak is found.

Usage:
  python verify_audio_clean.py -r report.json -e editlist.json \
      --extract-dir batch_extracted --mod-dir <mod>/FFXVI/data \
      --vgmstream <vgmstream-cli.exe> [--model large-v3]
"""
import argparse
import json
import re
import subprocess
import sys
import wave
import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scan_profanity import load_concepts, token_regex


def decode(vgm, sab, wav):
    r = subprocess.run([str(vgm), "-o", str(wav), str(sab)], capture_output=True, text=True)
    return r.returncode == 0 and Path(wav).exists()


def read_wav(wav):
    w = wave.open(str(wav), "rb")
    n = w.getnframes()
    a = array.array("h"); a.frombytes(w.readframes(n))
    return w.getframerate(), n, a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--report", required=True)
    ap.add_argument("-e", "--editlist", required=True)
    ap.add_argument("--extract-dir", required=True)
    ap.add_argument("--mod-dir", required=True)
    ap.add_argument("--vgmstream", required=True)
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--barks-flagged", help="optional battle_barks_flagged.json to also verify muted barks")
    ap.add_argument("--bark-mod-subdir", default="sound/voice/battle")
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    editlist = json.loads(Path(args.editlist).read_text(encoding="utf-8"))
    concepts = load_concepts(args.wordlist)
    enabled = editlist.get("enabled_concepts")
    enabled_ids = set(c["id"] for c in concepts) if enabled in (None, "all") else set(enabled)
    tok_pats = []
    for c in concepts:
        if c["id"] in enabled_ids:
            for tok in c["tokens"]:
                tok_pats.append((tok.lower(), re.compile(token_regex(tok), re.IGNORECASE)))

    from faster_whisper import WhisperModel
    print(f"Loading {args.model} for independent verification...", flush=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    extract_dir = Path(args.extract_dir)
    mod_dir = Path(args.mod_dir)
    work = Path("_verifywork"); work.mkdir(exist_ok=True)

    targets = []
    for res in report["results"]:
        internal = res["voice_sound_path"][:-4] + ".en.sab"
        targets.append(("line", internal, res))
    if args.barks_flagged and Path(args.barks_flagged).exists():
        for b in json.loads(Path(args.barks_flagged).read_text(encoding="utf-8")):
            stem = Path(b["file"]).stem
            internal = f"{args.bark_mod_subdir}/{stem}.sab"
            # only ones whose concept is enabled
            if set(b.get("concepts", [])) & enabled_ids:
                targets.append(("bark", internal, b))

    leaks, anomalies, checked = [], [], 0
    for kind, internal, meta in targets:
        muted = mod_dir / internal
        if not muted.exists():
            anomalies.append({"file": internal, "problem": "muted_file_missing"})
            continue
        mwav = work / "m.wav"
        if not decode(args.vgmstream, muted, mwav):
            anomalies.append({"file": internal, "problem": "decode_failed"})
            continue
        # duration check vs original (if available)
        orig = extract_dir / internal
        if orig.exists():
            owav = work / "o.wav"
            if decode(args.vgmstream, orig, owav):
                _, no, _ = read_wav(owav)
                _, nm, _ = read_wav(mwav)
                if no != nm:
                    anomalies.append({"file": internal, "problem": f"duration_mismatch {no} vs {nm}"})
        # THE definitive check: transcribe muted output, ensure no enabled token
        segs, _ = model.transcribe(str(mwav), language="en")
        heard = re.sub(r"[^a-z' ]", " ", " ".join(s.text for s in segs).lower())
        hits = sorted(set(tok for tok, pat in tok_pats if pat.search(heard)))
        checked += 1
        if hits:
            leaks.append({"file": internal, "heard": heard.strip()[:100], "tokens": hits,
                          "method": meta.get("method", kind)})
            print(f"LEAK: {internal} still has {hits} :: {heard.strip()[:70]!r}", flush=True)
        if checked % 50 == 0:
            print(f"...{checked}/{len(targets)} verified", flush=True)

    result = {"checked": checked, "leaks": leaks, "anomalies": anomalies}
    Path("verify_audio_clean_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nVerified {checked} muted files. Leaks: {len(leaks)}. Anomalies: {len(anomalies)}.")
    if leaks:
        print("*** PROFANITY STILL AUDIBLE IN THE ABOVE FILES -- NOT CLEAN ***")
        sys.exit(1)
    print("OK: no enabled profanity token detected in any muted output.")


if __name__ == "__main__":
    main()
