import os, re, subprocess, json, sys, tempfile
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

VGM = os.path.abspath('vgmstream/vgmstream-cli.exe')
BANK_DIR = Path('battle_barks/sound/voice/battle')
CKPT = Path('bank_scan.jsonl')       # one line per processed subsong -> crash-resumable
WORKERS = 8                          # machine dedicated now -> full speed
THREADS = 3

STEMS = ["fuck","shit","shi","bast","arse","ass","bugg","sod","prick","bolloc","bollox",
         "cock","whore","hoor","slut","cunt","damn","wench","scum","filth","swine","balls",
         "tit","piss","crap","hell","bloody","bleed","bitch","dick","twat","wank","knob","git "]
STEM_RE = re.compile("|".join(re.escape(s) for s in STEMS), re.IGNORECASE)

_model = None
def _init():
    global _model
    from faster_whisper import WhisperModel
    _model = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=THREADS)

def _work(task):
    bank, sub = task
    global _model
    tmp = os.path.join(tempfile.gettempdir(), f"bk_{os.getpid()}.wav")
    r = subprocess.run([VGM, "-s", str(sub), "-o", tmp, os.path.abspath(str(BANK_DIR/bank))],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        return (bank, sub, None)
    try:
        segs, _ = _model.transcribe(tmp, language="en")
        text = " ".join(s.text for s in segs).strip()
    except Exception:
        return (bank, sub, None)
    return (bank, sub, text if (text and STEM_RE.search(text)) else "")

def main():
    counts = json.loads(Path("bank_subsong_counts.json").read_text())
    all_tasks = [(bank, s) for bank, n in counts.items() for s in range(1, n+1)]

    done = set()
    if CKPT.exists():
        for line in CKPT.open(encoding="utf-8"):
            try:
                d = json.loads(line); done.add((d["b"], d["s"]))
            except Exception:
                pass
    tasks = [t for t in all_tasks if t not in done]
    print(f"{len(all_tasks)} total, {len(done)} already done, {len(tasks)} remaining", flush=True)

    ck = CKPT.open("a", encoding="utf-8")
    processed = 0; hits = 0
    with ProcessPoolExecutor(max_workers=WORKERS, initializer=_init) as ex:
        for bank, sub, text in ex.map(_work, tasks, chunksize=15):
            ck.write(json.dumps({"b": bank, "s": sub, "t": text or ""}) + "\n")
            processed += 1
            if text:
                hits += 1
                print(f"CANDIDATE {bank} #{sub}: {text[:60]!r}", flush=True)
            if processed % 500 == 0:
                ck.flush()
                print(f"...{processed}/{len(tasks)} this run ({hits} new candidates)", flush=True)
    ck.close()

    # compile final candidates from the full checkpoint
    cands = []
    for line in CKPT.open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("t"):
            cands.append({"bank": d["b"], "sub": d["s"], "text": d["t"]})
    Path("bank_candidates.json").write_text(json.dumps(cands, indent=2), encoding="utf-8")
    print(f"\nDONE. {len(cands)} total candidates across the full corpus.", flush=True)

if __name__ == "__main__":
    main()
