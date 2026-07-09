"""
Build the mod from a precompiled cutlist.json instead of running the full
scan+align+ASR pipeline. This is the fast path: it only needs the same
lightweight native tools as the rest of this repo (FF16Tools, vgmstream,
VGAudioCli) -- no PyTorch, no Whisper model download, no per-line alignment.

Every entry in cutlist.json already says exactly what to cut (in seconds) and
carries a sha256 fingerprint of the original clip. This script:
  1. Extracts each flagged file fresh from YOUR OWN legally-owned game files.
  2. Verifies its hash still matches what the cutlist expects.
  3. Applies the precomputed cut (or whole-line mute) -- or, if the game was
     patched and the audio no longer matches, falls back to a whole-line mute
     (dialogue) / skips it (combat banks) rather than trusting stale offsets
     against different audio.

Usage:
  python apply_cutlist.py ../data/cutlist.json --enable fuck,shit,bastard,...
Env vars (same names as batch_mute_pipeline.py, so the GUI can reuse them):
  FF16_CLI, VGAUDIOCLI, VGMSTREAM_CLI, FFXVI_DATA_DIR, MOD_OUTPUT_DIR,
  SAB_MUTE_SCRIPT, MUTE_BANK_SCRIPT
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import batch_mute_pipeline as bmp  # noqa: E402  (torch is imported lazily inside it, not at load time)

MUTE_BANK = Path(os.environ.get("MUTE_BANK_SCRIPT", SCRIPTS / "mute_bank_subsongs.py"))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract_raw(internal_path):
    """Always re-extracts fresh from the pack -- never trusts a stale cached
    copy -- so the integrity check below is meaningful even if this script has
    been run before against an older game version."""
    pack = bmp.pack_for_path(internal_path)
    dest = bmp.EXTRACT_DIR / internal_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    r = subprocess.run(
        [str(bmp.FF16_CLI), "unpack", "-i", str(pack), "-f", internal_path, "-o", str(bmp.EXTRACT_DIR)],
        capture_output=True, text=True, env=bmp.ENV,
    )
    if not dest.exists():
        raise RuntimeError(f"extraction failed for {internal_path}: {r.stdout[-300:]} {r.stderr[-300:]}")
    return dest


def apply_dialogue(entries, enabled, stats):
    for entry in entries:
        if not (set(entry["concepts"]) & enabled):
            stats["skipped_no_concept"] += 1
            continue
        internal = bmp.locale_path(entry["path"])
        try:
            src = extract_raw(internal)
        except Exception as ex:
            print(f"  ! extract failed {internal}: {ex}")
            stats["extract_failed"] += 1
            continue
        dest = bmp.mod_dest_for(entry["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if sha256_file(src) != entry["orig_sha256"]:
            print(f"  ! version mismatch, whole-line-muting as a safety fallback: {internal}")
            bmp.mute_whole_line(src, dest)
            stats["mismatch_whole_lined"] += 1
            continue
        if entry["method"] == "whole_line":
            bmp.mute_whole_line(src, dest)
            stats["whole_line"] += 1
        else:
            bmp.apply_cuts(src, dest, entry["cuts"])
            stats["word_level"] += 1


def apply_banks(entries, enabled, stats):
    for bank in entries:
        subs = [s["index"] for s in bank["subsongs"] if set(s["concepts"]) & enabled]
        if not subs:
            stats["skipped_no_concept"] += 1
            continue
        # Combat banks are locale-tagged in the pack just like dialogue (e.g.
        # sound/voice/battle/vo_pc001.en.sab), and the mod loader only routes a
        # loose file correctly if its own filename carries that .en suffix too
        # -- so both the pack lookup AND the mod's output filename need it.
        internal = bmp.locale_path(bank["bank"])
        try:
            src = extract_raw(internal)
        except Exception as ex:
            print(f"  ! bank extract failed {internal}: {ex}")
            stats["bank_extract_failed"] += len(subs)
            continue
        if sha256_file(src) != bank["orig_sha256"]:
            print(f"  ! version mismatch, skipping bank (left unmuted): {internal}")
            stats["bank_mismatch_skipped"] += len(subs)
            continue
        dest = bmp.MOD_DIR / internal
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, str(MUTE_BANK), str(src), str(dest), ",".join(str(i) for i in subs),
             "--vgaudio", str(bmp.VGAUDIOCLI)],
            capture_output=True, text=True, env=bmp.ENV,
        )
        if not dest.exists():
            print(f"  ! bank mute failed {internal}: {r.stdout[-300:]} {r.stderr[-300:]}")
            stats["bank_extract_failed"] += len(subs)
            continue
        stats["bank_subsongs_muted"] += len(subs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cutlist")
    ap.add_argument("--enable", required=True, help="comma-separated concept ids to mute")
    ap.add_argument("-o", "--report", default=None)
    args = ap.parse_args()

    enabled = set(x.strip() for x in args.enable.split(",") if x.strip())
    if not enabled:
        print("Nothing enabled -- pick at least one concept with --enable.")
        sys.exit(1)

    data = json.loads(Path(args.cutlist).read_text(encoding="utf-8"))
    stats = {
        "word_level": 0, "whole_line": 0, "mismatch_whole_lined": 0,
        "extract_failed": 0, "skipped_no_concept": 0,
        "bank_subsongs_muted": 0, "bank_mismatch_skipped": 0, "bank_extract_failed": 0,
    }

    print(f"Applying cutlist ({len(data['dialogue'])} dialogue lines, {len(data['banks'])} combat banks)...")
    apply_dialogue(data["dialogue"], enabled, stats)
    apply_banks(data["banks"], enabled, stats)
    bmp.write_mod_config()

    print("\n--- apply_cutlist summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if args.report:
        Path(args.report).write_text(json.dumps(stats, indent=2), encoding="utf-8")

    if stats["mismatch_whole_lined"] or stats["bank_mismatch_skipped"]:
        print("\nNOTE: some clips didn't match this cutlist's fingerprint (likely a game update).")
        print("Mismatched dialogue lines were whole-line-muted as a safety fallback; mismatched")
        print("combat-bank subsongs were left untouched (skipped rather than blanking a whole bank")
        print("of legitimate grunts). Re-run the full pipeline (needs the ML deps in requirements.txt)")
        print("for precise cuts on just those files, or regenerate/update the cutlist.")


if __name__ == "__main__":
    main()
