"""
Boundary-accurate verification for the forced-alignment build. For every muted
line it force-aligns the known subtitle text to the ORIGINAL audio (accurate,
deterministic word boundaries), then measures the MUTED audio energy across each
enabled-profanity word's exact span plus a short release window. If any target
word still has audible energy, it's a leak/fragment -- something the ASR gate
can miss (a leftover '-ing' or '...ck' isn't a whole token). Deterministic.

Exits non-zero if anything is flagged.
"""
import argparse, json, os, re, subprocess, sys, wave, array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scan_profanity import load_concepts

WORD_RMS = 70.0      # muted word span must be quieter than this
REL_RMS = 300.0      # release window (right after the word) tolerance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--report", required=True)
    ap.add_argument("-e", "--editlist", required=True)
    ap.add_argument("--extract-dir", required=True)
    ap.add_argument("--mod-dir", required=True)
    ap.add_argument("--vgmstream", required=True)
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    args = ap.parse_args()

    import torch, torchaudio
    torch.set_num_threads(int(os.environ.get("ALIGN_THREADS", "8")))
    b = torchaudio.pipelines.MMS_FA
    model, tok, aligner = b.get_model(), b.get_tokenizer(), b.get_aligner()

    concepts = load_concepts(args.wordlist)
    editlist = json.loads(Path(args.editlist).read_text(encoding="utf-8"))
    enabled = editlist.get("enabled_concepts")
    enabled_ids = set(c["id"] for c in concepts) if enabled in (None, "all") else set(enabled)
    enabled_tokens = set()
    for c in concepts:
        if c["id"] in enabled_ids:
            for t in c["tokens"]:
                for part in t.split():
                    enabled_tokens.add(re.sub(r"[^a-z']", "", part.lower()))
    enabled_tokens.discard("")
    line_by_id = {m["id"]: m for m in editlist["matches"]}

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    extract_dir = Path(args.extract_dir); mod_dir = Path(args.mod_dir)

    def load(path, f):
        subprocess.run([args.vgmstream, "-o", f, os.path.abspath(str(path))], capture_output=True)
        if not os.path.exists(f):
            return None, None
        w = wave.open(f, "rb"); sr = w.getframerate()
        a = array.array("h"); a.frombytes(w.readframes(w.getnframes()))
        return a, sr

    def align(a, sr, text):
        t = torch.tensor(a, dtype=torch.float32).view(1, -1) / 32768.0
        wav = torchaudio.functional.resample(t, sr, b.sample_rate)
        words = re.findall(r"[A-Za-z']+", text.lower())
        if not words:
            return []
        with torch.inference_mode():
            em, _ = model(wav); sp = aligner(em[0], tok(words))
        r = wav.size(1) / em.size(1) / b.sample_rate
        return [(w, s[0].start * r, s[-1].end * r) for w, s in zip(words, sp)]

    def rms(a, sr, s, e):
        seg = a[max(0, int(s * sr)):int(e * sr)]
        return (sum(x * x for x in seg) / len(seg)) ** 0.5 if len(seg) else 0

    leaks, checked = [], 0
    for res in report["results"]:
        if res.get("method") != "word_level":
            continue  # whole-line / fallback are silent by construction
        vid = res["id"]; internal = res["voice_sound_path"][:-4] + ".en.sab"
        m = line_by_id.get(vid)
        if not m:
            continue
        oa, osr = load(extract_dir / internal, "_avo.wav")
        ma, msr = load(mod_dir / internal, "_avm.wav")
        if oa is None or ma is None:
            leaks.append({"id": vid, "problem": "decode_failed"}); continue
        checked += 1
        aligned = align(oa, osr, m["line"])
        for wi, (w, ws, we) in enumerate(aligned):
            if re.sub(r"[^a-z']", "", w) not in enabled_tokens:
                continue
            wr = rms(ma, msr, ws, we)                      # the word's own span must be silent
            # release window, but never past the next word's onset (that neighbour
            # is intentionally preserved and would otherwise false-flag).
            nxt = aligned[wi + 1][1] if wi + 1 < len(aligned) else we + 0.07
            rr = rms(ma, msr, we, min(we + 0.07, nxt))
            if wr >= WORD_RMS or rr >= REL_RMS:
                leaks.append({"id": vid, "word": w, "span": [round(ws, 2), round(we, 2)],
                              "word_rms": round(wr), "release_rms": round(rr),
                              "line": m["line"][:60]})
        if checked % 50 == 0:
            print(f"...{checked} verified, {len(leaks)} flagged", flush=True)

    for f in ("_avo.wav", "_avm.wav"):
        if os.path.exists(f):
            os.remove(f)
    Path("verify_aligned_result.json").write_text(json.dumps(leaks, indent=2), encoding="utf-8")
    print(f"\nChecked {checked} word-level lines. Flagged: {len(leaks)}.")
    for lk in leaks[:40]:
        print("  ", lk)
    sys.exit(1 if leaks else 0)


if __name__ == "__main__":
    main()
