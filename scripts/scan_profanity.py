"""
Scans extracted FFXVI .pzd->.yaml dialogue for profanity and emits an edit-list:
which lines matched, which concept/word, and the exact VoiceSoundPath to mute.

Detection is exact-token with word boundaries (no substring false positives).
The wordlist is concept-based: each concept enumerates every inflected form that
actually appears in the game, so enabling a concept catches all of them (this is
what fixes the classic 'bastard' matching but missing 'bastards' bug).

Metadata only -- reads no game audio. Safe to publish.
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


def load_concepts(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "concepts" in data:
        return data["concepts"]
    # backward-compat: old flat {severity: [words]} format -> one concept per word
    concepts = []
    for sev, terms in data.items():
        if sev.startswith("_"):
            continue
        for t in terms:
            concepts.append({"id": t, "label": t, "category": sev, "default": True, "tokens": [t]})
    return concepts


def token_regex(tok):
    """Wrap a literal token with word boundaries only on the sides that end in a
    word char (so trailing/leading apostrophes like bleedin' still match)."""
    esc = re.escape(tok).replace(r"\ ", r"\s+")
    left = r"\b" if tok[:1].isalnum() else ""
    right = r"\b" if tok[-1:].isalnum() else ""
    return left + esc + right


def build_matcher(concepts, enabled_ids):
    token_to_concept = {}
    patterns = []
    for c in concepts:
        if enabled_ids is not None and c["id"] not in enabled_ids:
            continue
        for tok in c["tokens"]:
            token_to_concept[tok.lower()] = c["id"]
    # longest tokens first so multi-word / longer forms win
    for tok in sorted(token_to_concept, key=len, reverse=True):
        patterns.append(token_regex(tok))
    if not patterns:
        return None, token_to_concept
    pattern = re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)
    return pattern, token_to_concept


def clean_line(text):
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", " ", text)


def scan(yaml_paths, pattern, token_to_concept):
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
            found_lower = sorted(set(f.lower() for f in found))
            concepts_hit = sorted(set(token_to_concept.get(f, "?") for f in found_lower))
            matches.append({
                "id": entry.get("Id"),
                "line": line,
                "matched_words": found_lower,
                "concepts": concepts_hit,
                "voice_sound_path": entry.get("VoiceSoundPath") or "",
                "source_yaml": str(yp),
            })
    return matches, total_entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", help="Directories to search for .yaml dialogue")
    ap.add_argument("-w", "--wordlist", default=str(Path(__file__).parent / "profanity_wordlist.json"))
    ap.add_argument("-o", "--output", default="profanity_editlist.json")
    ap.add_argument("--enable", help="Comma-separated concept ids to enable (default: concepts with default=true)")
    ap.add_argument("--all", action="store_true", help="Enable every concept regardless of default")
    args = ap.parse_args()

    concepts = load_concepts(args.wordlist)
    if args.all:
        enabled = None
    elif args.enable:
        enabled = set(x.strip() for x in args.enable.split(",") if x.strip())
    else:
        enabled = set(c["id"] for c in concepts if c.get("default"))

    pattern, token_to_concept = build_matcher(concepts, enabled)
    if pattern is None:
        sys.exit("No concepts enabled -- nothing to scan for.")

    yaml_paths = []
    for root in args.roots:
        yaml_paths.extend(Path(root).rglob("*.yaml"))
    enabled_label = "ALL" if enabled is None else ", ".join(sorted(enabled))
    print(f"Scanning {len(yaml_paths)} dialogue files; enabled concepts: {enabled_label}")

    matches, total_entries = scan(yaml_paths, pattern, token_to_concept)

    by_concept = {}
    for m in matches:
        for c in m["concepts"]:
            by_concept[c] = by_concept.get(c, 0) + 1

    result = {
        "total_files_scanned": len(yaml_paths),
        "total_dialogue_lines_scanned": total_entries,
        "total_matches": len(matches),
        "enabled_concepts": sorted(enabled) if enabled else "all",
        "matches_by_concept": dict(sorted(by_concept.items(), key=lambda x: -x[1])),
        "matches": matches,
    }
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {len(matches)} matches to {args.output}")
    print(f"By concept: {result['matches_by_concept']}")


if __name__ == "__main__":
    main()
