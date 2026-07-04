"""
Extracts and converts all dialogue text (.pzd -> .yaml) needed by
scan_profanity.py: base game + DLC2 + DLC3 English text packs.

Requires FF16Tools (https://github.com/Nenkai/FF16Tools) and a .NET runtime.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

TEXT_PACKS = ["0007.en.pac", "2002.en.pac", "3002.en.pac"]


def unpack_all(ff16_cli, pack_path, out_dir, env):
    r = subprocess.run([str(ff16_cli), "unpack-all", "-i", str(pack_path), "-o", str(out_dir)],
                        capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"FAILED to unpack {pack_path}:\n{r.stdout[-1000:]}\n{r.stderr[-1000:]}", file=sys.stderr)
        return False
    return True


def convert_batch(ff16_cli, batch, env):
    args = [str(ff16_cli), "pzd-conv", "-i"] + [str(p) for p in batch]
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print("BATCH CONVERT FAILED:", r.stdout[-1000:], r.stderr[-1000:], file=sys.stderr)
    return len(batch)


def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-data", required=True, help="Path to FFXVI's data/ folder")
    ap.add_argument("--ff16-cli", required=True, help="Path to FF16Tools.CLI.exe")
    ap.add_argument("--out", default="extracted", help="Output directory root")
    args = ap.parse_args()

    game_data = Path(args.game_data)
    ff16_cli = Path(args.ff16_cli)
    out_root = Path(args.out)

    env = dict(os.environ)
    env["DOTNET_ROLL_FORWARD"] = "LatestMajor"

    for pack in TEXT_PACKS:
        pack_path = game_data / pack
        out_dir = out_root / pack.replace(".pac", "").replace(".", "_")
        print(f"Unpacking {pack} -> {out_dir}")
        unpack_all(ff16_cli, pack_path, out_dir, env)

    all_pzd = list(out_root.rglob("*.pzd"))
    todo = [p for p in all_pzd if not p.with_suffix("").with_suffix(".yaml").exists()]
    print(f"Converting {len(todo)} .pzd files to .yaml ({len(all_pzd) - len(todo)} already done)...")

    batches = list(chunk(todo, 150))
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(convert_batch, ff16_cli, b, env) for b in batches]
        for f in futures:
            done += f.result()
            print(f"progress: {done}/{len(todo)}")

    print(f"Done. Dialogue text ready under {out_root} -- pass these directories to scan_profanity.py")


if __name__ == "__main__":
    main()
