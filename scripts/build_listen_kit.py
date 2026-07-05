"""
Builds an offline audio review kit so you can hear-confirm every mute WITHOUT
launching the game (no spoilers from playing). For each muted line it decodes
the original and muted audio to browser-playable WAV and generates a single
index.html: play the MUTED clip to confirm the swear is gone; optionally reveal
the original clip and the full subtitle text (hidden by default to limit
spoilers -- only the target word is shown up front).
"""
import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent  # repo root guess; overridden by args


def decode(vgmstream_cli, sab, wav):
    if wav.exists():
        return True
    r = subprocess.run([str(vgmstream_cli), "-o", str(wav), str(sab)], capture_output=True, text=True)
    return r.returncode == 0 and wav.exists()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--report", required=True, help="batch_pipeline_report_*.json")
    ap.add_argument("-e", "--editlist", required=True, help="edit-list JSON (for line text + concepts)")
    ap.add_argument("--extract-dir", required=True, help="dir with extracted ORIGINAL .sab (batch_extracted)")
    ap.add_argument("--mod-dir", required=True, help="mod .../FFXVI/data dir with MUTED .sab")
    ap.add_argument("--vgmstream", required=True, help="path to vgmstream-cli.exe")
    ap.add_argument("-o", "--out", default="listen_kit")
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    editlist = json.loads(Path(args.editlist).read_text(encoding="utf-8"))
    text_by_id = {m["id"]: m for m in editlist["matches"]}

    out = Path(args.out)
    audio = out / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    extract_dir = Path(args.extract_dir)
    mod_dir = Path(args.mod_dir)

    rows = []
    for res in report["results"]:
        vid = res["id"]
        vp = res["voice_sound_path"]
        internal = vp[:-4] + ".en.sab"
        orig_sab = extract_dir / internal
        muted_sab = mod_dir / internal
        meta = text_by_id.get(vid, {})
        stem = f"{vid}"
        orig_wav = audio / f"{stem}_orig.wav"
        muted_wav = audio / f"{stem}_muted.wav"
        ok_o = orig_sab.exists() and decode(args.vgmstream, orig_sab, orig_wav)
        ok_m = muted_sab.exists() and decode(args.vgmstream, muted_sab, muted_wav)
        if not ok_m:
            continue
        rows.append({
            "id": vid,
            "words": meta.get("matched_words", res.get("matched_words", [])),
            "concepts": meta.get("concepts", []),
            "line": meta.get("line", ""),
            "method": res.get("method", ""),
            "orig": f"audio/{stem}_orig.wav" if ok_o else None,
            "muted": f"audio/{stem}_muted.wav",
        })

    # sort by concept then id for easy scanning
    rows.sort(key=lambda r: (",".join(r["concepts"]), r["id"]))

    parts = ["""<!doctype html><html><head><meta charset="utf-8">
<title>ProFFanXVI - Mute Review Kit</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#111;color:#eee}
 header{position:sticky;top:0;background:#1b1b1b;padding:12px 18px;border-bottom:1px solid #333;z-index:2}
 h1{margin:0 0 6px;font-size:18px} .sub{color:#aaa;font-size:13px}
 .controls{margin-top:8px;font-size:13px} input[type=search]{padding:5px 8px;width:240px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
 table{border-collapse:collapse;width:100%} td,th{padding:8px 10px;border-bottom:1px solid #2a2a2a;vertical-align:middle;font-size:14px}
 th{position:sticky;top:84px;background:#1b1b1b;text-align:left;color:#bbb}
 .word{font-weight:700;color:#ff8080} .concept{color:#88aaff;font-size:12px}
 .method{font-size:11px;color:#9a9a9a}
 audio{height:32px;vertical-align:middle}
 .reveal{cursor:pointer;color:#66c;text-decoration:underline;font-size:12px}
 .line{display:none;color:#ccc;font-style:italic;max-width:520px}
 .esc{color:#ffc266}
</style></head><body>
<header>
 <h1>ProFFanXVI &mdash; Mute Review Kit</h1>
 <div class="sub">Play the <b>Muted</b> clip and confirm no profanity is audible. Original audio &amp; full subtitle text are hidden by default (spoilers) &mdash; click <i>reveal</i> per row.</div>
 <div class="controls">Filter: <input type="search" id="q" placeholder="word, concept, or id..." oninput="filt()"> &nbsp; <span id="count"></span></div>
</header>
<table id="t"><thead><tr><th>#</th><th>Word</th><th>Concept</th><th>Muted (confirm silent)</th><th>Original</th><th>Method</th></tr></thead><tbody>
"""]
    for r in rows:
        words = " ".join(html.escape(w) for w in r["words"])
        concepts = " ".join(html.escape(c) for c in r["concepts"])
        method = html.escape(r["method"])
        esc_cls = " esc" if "escalated" in method else ""
        line = html.escape(r["line"])
        orig_cell = (f'<span class="reveal" onclick="this.nextElementSibling.style.display=\'inline\';this.style.display=\'none\'">reveal</span>'
                     f'<audio style="display:none" controls preload="none" src="{r["orig"]}"></audio>') if r["orig"] else "-"
        parts.append(
            f'<tr data-s="{words} {concepts} {r["id"]}">'
            f'<td>{r["id"]}</td>'
            f'<td class="word">{words}</td>'
            f'<td class="concept">{concepts}</td>'
            f'<td><audio controls preload="none" src="{r["muted"]}"></audio></td>'
            f'<td>{orig_cell}<span class="reveal" onclick="var l=this.parentElement.querySelector(\'.line\');l.style.display=l.style.display==\'inline\'?\'none\':\'inline\'">text</span> <span class="line">{line}</span></td>'
            f'<td class="method{esc_cls}">{method}</td>'
            f'</tr>')
    parts.append("""</tbody></table>
<script>
 function filt(){var q=document.getElementById('q').value.toLowerCase();var n=0;
  document.querySelectorAll('#t tbody tr').forEach(function(tr){var m=tr.dataset.s.toLowerCase().indexOf(q)>=0;tr.style.display=m?'':'none';if(m)n++;});
  document.getElementById('count').textContent=n+' shown';}
 filt();
</script></body></html>""")

    (out / "index.html").write_text("".join(parts), encoding="utf-8")
    print(f"Listen kit: {len(rows)} muted lines -> {out/'index.html'}")
    print("Open that file in a browser. Play each 'Muted' clip to confirm no profanity remains.")


if __name__ == "__main__":
    main()
