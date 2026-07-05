"""
Second pass for combat-vocalization banks. Takes the fast first-pass candidates
(high-recall, over-flagged), verifies each with large-v3, keeps only those whose
transcript actually contains an ENABLED profanity token, then surgically mutes
exactly those subsongs inside their banks (leaving all other grunts intact).

Inputs:
  --candidates  bank_candidates.json  (first-pass hits: [{bank, sub, text}])
  --bank-dir    dir with extracted bank .sab under sound/voice/battle/
  --mod-dir     mod .../FFXVI/data output
  --enable      comma-separated enabled concept ids
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scan_profanity import load_concepts, token_regex
from mute_bank_subsongs import mute_bank


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--bank-dir", required=True, help="dir containing sound/voice/battle/*.sab")
    ap.add_argument("--mod-dir", required=True)
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    ap.add_argument("--enable", required=True)
    ap.add_argument("--vgmstream", default=os.environ.get("VGMSTREAM_CLI", "vgmstream-cli.exe"))
    ap.add_argument("--vgaudio", default=os.environ.get("VGAUDIOCLI", "VGAudioCli.exe"))
    ap.add_argument("--model", default="large-v3")
    args = ap.parse_args()

    concepts = load_concepts(args.wordlist)
    enabled = set(x.strip() for x in args.enable.split(",") if x.strip())
    tok_pats = []
    for c in concepts:
        if c["id"] in enabled:
            for tok in c["tokens"]:
                tok_pats.append((c["id"], re.compile(token_regex(tok), re.IGNORECASE)))

    cands = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    bank_dir = Path(args.bank_dir)
    work = Path("_bankverify"); work.mkdir(exist_ok=True)

    from faster_whisper import WhisperModel
    print(f"Verifying {len(cands)} candidates with {args.model}...", flush=True)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    confirmed = defaultdict(list)   # bank filename -> [subsong,...]
    details = []
    for i, c in enumerate(cands):
        bank, sub = c["bank"], c["sub"]
        sab = bank_dir / "sound" / "voice" / "battle" / bank
        wav = work / "c.wav"
        subprocess.run([args.vgmstream, "-s", str(sub), "-o", str(wav), str(sab)],
                       capture_output=True, text=True)
        if not wav.exists():
            continue
        segs, _ = model.transcribe(str(wav), language="en")
        heard = re.sub(r"[^a-z' ]", " ", " ".join(s.text for s in segs).lower())
        hits = sorted(set(cid for cid, pat in tok_pats if pat.search(heard)))
        if hits:
            confirmed[bank].append(sub)
            details.append({"bank": bank, "sub": sub, "concepts": hits, "heard": heard.strip()[:80]})
            print(f"CONFIRMED {bank} #{sub}: {hits} :: {heard.strip()[:50]!r}", flush=True)

    Path("bank_confirmed.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    print(f"\n{sum(len(v) for v in confirmed.values())} subsongs confirmed across {len(confirmed)} banks. Muting...", flush=True)

    muted = 0
    for bank, subs in confirmed.items():
        src = bank_dir / "sound" / "voice" / "battle" / bank
        dest = Path(args.mod_dir) / "sound" / "voice" / "battle" / bank
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            mute_bank(str(src), str(dest), sorted(set(subs)), args.vgaudio, work)
            muted += len(set(subs))
            print(f"muted {bank}: subsongs {sorted(set(subs))}", flush=True)
        except Exception as e:
            print(f"ERROR muting {bank}: {e}", flush=True)

    print(f"\nDone. Muted {muted} subsongs in {len(confirmed)} banks.")


if __name__ == "__main__":
    main()
