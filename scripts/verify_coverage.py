"""
Zero-tolerance detection self-test. For the enabled concepts, independently
confirms that EVERY occurrence of every listed token in the entire dialogue
corpus produced a match in the edit-list. If any line contains an enabled
token but isn't in the edit-list, it's reported as a LEAK (a profanity that
would slip through). Exits non-zero if any leak is found.
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

from scan_profanity import load_concepts, token_regex, clean_line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    ap.add_argument("-e", "--editlist", required=True, help="edit-list JSON produced by scan_profanity.py")
    args = ap.parse_args()

    concepts = load_concepts(args.wordlist)
    editlist = json.loads(Path(args.editlist).read_text(encoding="utf-8"))
    enabled = editlist.get("enabled_concepts")
    if enabled == "all" or enabled is None:
        enabled_ids = set(c["id"] for c in concepts)
    else:
        enabled_ids = set(enabled)

    # independent matcher, one regex per enabled token (so we can name the culprit)
    tok_patterns = []
    for c in concepts:
        if c["id"] not in enabled_ids:
            continue
        for tok in c["tokens"]:
            tok_patterns.append((tok.lower(), c["id"], re.compile(token_regex(tok), re.IGNORECASE)))

    matched_ids = set(m["id"] for m in editlist["matches"])

    leaks = []
    total_lines = 0
    for root in args.roots:
        for yp in Path(root).rglob("*.yaml"):
            try:
                entries = yaml.safe_load(yp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not entries:
                continue
            for entry in entries:
                total_lines += 1
                line = clean_line(entry.get("Line"))
                if not line:
                    continue
                hits = [(tok, cid) for tok, cid, pat in tok_patterns if pat.search(line)]
                if hits and entry.get("Id") not in matched_ids:
                    leaks.append({
                        "id": entry.get("Id"),
                        "line": line,
                        "tokens": sorted(set(t for t, _ in hits)),
                        "concepts": sorted(set(c for _, c in hits)),
                        "voice_sound_path": entry.get("VoiceSoundPath") or "",
                    })

    print(f"Scanned {total_lines} lines against {len(tok_patterns)} enabled tokens.")
    print(f"Edit-list contains {len(matched_ids)} matched line ids.")
    if leaks:
        print(f"\n*** {len(leaks)} LEAK(S) FOUND -- profanity that would slip through: ***")
        for lk in leaks[:50]:
            print(f"  id={lk['id']} {lk['tokens']} :: {lk['line'][:80]!r}")
        sys.exit(1)
    else:
        print("\nOK: every enabled-token occurrence in the corpus is covered by the edit-list. No leaks.")


if __name__ == "__main__":
    main()
