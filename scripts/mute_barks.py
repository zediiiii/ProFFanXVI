"""
Mutes the un-subtitled combat barks that a text scan can't reach. These voice
files (e.g. sound/voice/battle/vo_zh003_a.sab) have no .pzd subtitle, so they
were transcribed directly (see the transcribe step) to find spoken profanity.
Each flagged bark is short (a single combat exclamation), so we whole-clip mute
it -- guaranteed removal, and losing a battle grunt is inconsequential.

Input: a flagged-barks JSON [{"file": "vo_xxx.wav", "text": "...", "concepts": [...]}]
The matching .sab is expected under --bark-sab-dir/sound/voice/battle/<stem>.sab
(these barks are language-neutral: no .en suffix).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--flagged", required=True)
    ap.add_argument("--bark-sab-dir", required=True, help="dir with extracted bark .sab (…/sound/voice/battle/*.sab under it)")
    ap.add_argument("--mod-dir", required=True, help="mod …/FFXVI/data output dir")
    ap.add_argument("--sab-mute", default=str(Path(__file__).parent / "sab_mute.py"))
    ap.add_argument("--enabled", default="", help="comma-separated concept ids to actually mute (default: all flagged)")
    args = ap.parse_args()

    flagged = json.loads(Path(args.flagged).read_text(encoding="utf-8"))
    enabled = set(x.strip() for x in args.enabled.split(",") if x.strip())
    bark_dir = Path(args.bark_sab_dir)
    mod_dir = Path(args.mod_dir)

    done, skipped, errors = 0, 0, []
    for b in flagged:
        concepts = b.get("concepts", [])
        if enabled and not (set(concepts) & enabled):
            skipped += 1
            continue
        stem = Path(b["file"]).stem  # vo_zh003_a
        rel = f"sound/voice/battle/{stem}.sab"
        src = bark_dir / rel
        if not src.exists():
            errors.append({"file": b["file"], "error": "sab not found"})
            continue
        dest = mod_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, args.sab_mute, str(src), str(dest)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not dest.exists():
            errors.append({"file": b["file"], "error": r.stderr[-300:]})
        else:
            done += 1
            print(f"muted bark {stem}: {b.get('text','')!r} {concepts}")

    print(f"\nBarks muted: {done}, skipped (concept off): {skipped}, errors: {len(errors)}")
    for e in errors:
        print("  ERROR", e)


if __name__ == "__main__":
    main()
