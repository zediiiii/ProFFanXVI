"""
Scans all extracted FFXVI .pzd->.yaml dialogue files for profanity and emits
an edit-list JSON: which lines matched, which word(s), and the exact
VoiceSoundPath to the .sab file that needs muting.

This is metadata only -- it does not read or write any copyrighted game
audio itself. Safe to publish/share.
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


def load_wordlist(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    words = {}
    for severity, terms in data.items():
        if severity.startswith("_"):
            continue
        for term in terms:
            words[term.lower()] = severity
    return words


def build_pattern(words):
    # longest-first so multi-word phrases match before their substrings
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    # allow flexible whitespace within phrases (e.g. "god damn")
    escaped = [w.replace(r"\ ", r"\s+") for w in escaped]
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


def clean_line(text):
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", " ", text)


def scan(yaml_paths, words, pattern):
    matches = []
    total_entries = 0
    for yp in yaml_paths:
        try:
            entries = yaml.safe_load(yp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN: failed to parse {yp}: {e}", file=sys.stderr)
            continue
        if not entries:
            continue
        total_entries += len(entries)
        for entry in entries:
            line = clean_line(entry.get("Line"))
            if not line:
                continue
            found = pattern.findall(line)
            if not found:
                continue
            voice_path = entry.get("VoiceSoundPath") or ""
            matches.append({
                "id": entry.get("Id"),
                "line": line,
                "matched_words": sorted(set(w.lower() for w in found)),
                "severities": sorted(set(words.get(w.lower(), "unknown") for w in found)),
                "voice_sound_path": voice_path,
                "source_yaml": str(yp),
            })
    return matches, total_entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", help="Root directories to search for .yaml files (converted from .pzd)")
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    ap.add_argument("-o", "--output", default="profanity_editlist.json")
    args = ap.parse_args()

    words = load_wordlist(args.wordlist)
    pattern = build_pattern(words)

    yaml_paths = []
    for root in args.roots:
        yaml_paths.extend(Path(root).rglob("*.yaml"))
    print(f"Scanning {len(yaml_paths)} dialogue files against {len(words)} wordlist entries...")

    matches, total_entries = scan(yaml_paths, words, pattern)

    by_severity = {}
    for m in matches:
        for s in m["severities"]:
            by_severity[s] = by_severity.get(s, 0) + 1

    result = {
        "total_files_scanned": len(yaml_paths),
        "total_dialogue_lines_scanned": total_entries,
        "total_matches": len(matches),
        "matches_by_severity": by_severity,
        "matches": matches,
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {len(matches)} matches to {args.output}")
    print(f"By severity: {by_severity}")


if __name__ == "__main__":
    main()
