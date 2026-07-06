"""
Full pipeline: takes the scan_profanity.py edit-list and, for every match,
extracts the real .sab from the game's pack files, finds the flagged word's
precise timing (faster-whisper + energy-envelope refinement, same method
validated on the "Moss" test line), mutes just that word, and assembles a
Reloaded-II mod folder.

Safety design: if ASR can't confidently locate the flagged word inside its
own transcription of the clip, this falls back to muting the WHOLE line
(the previously-verified-safe behavior) rather than guessing a wrong time
range. Every result is tagged with which method was used so a human can
prioritize spot-checking the fallback cases.
"""
import json
import re
import subprocess
import sys
import os
import wave
import array
from pathlib import Path

# All paths below can be overridden with environment variables so this script
# isn't tied to one machine's exact folder layout.
TOOLS_DIR = Path(__file__).parent
FF16_CLI = Path(os.environ.get("FF16_CLI", TOOLS_DIR / "FF16Tools" / "win-x64" / "FF16Tools.CLI.exe"))
VGAUDIOCLI = Path(os.environ.get("VGAUDIOCLI", TOOLS_DIR / "VGAudioCli.exe"))
VGMSTREAM_CLI = Path(os.environ.get("VGMSTREAM_CLI", TOOLS_DIR / "vgmstream" / "vgmstream-cli.exe"))
SAB_MUTE = Path(os.environ.get("SAB_MUTE_SCRIPT", TOOLS_DIR / "sab_mute.py"))

DATA_DIR = Path(os.environ.get(
    "FFXVI_DATA_DIR",
    r"C:\Program Files (x86)\Steam\steamapps\common\FINAL FANTASY XVI\data",
))

EXTRACT_DIR = Path(os.environ.get("BATCH_EXTRACT_DIR", TOOLS_DIR / "batch_extracted"))
MOD_DIR = Path(os.environ.get(
    "MOD_OUTPUT_DIR",
    TOOLS_DIR / "ReloadedII" / "Mods" / "ff16.audio.profanity-filter" / "FFXVI" / "data",
))

ENV = dict(os.environ)
ENV["DOTNET_ROLL_FORWARD"] = "LatestMajor"

# Model / mode configuration (env-overridable so the GUI can drive it).
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
# "whole_line" = mute the whole line for every match (bulletproof, no ASR needed).
# "word_level" = precise word cut, self-verified, auto-escalating to whole-line
#                whenever the word can't be located OR is still audible after the cut.
SAFE_MODE = os.environ.get("SAFE_MODE", "word_level")


def pack_for_path(voice_sound_path):
    if voice_sound_path.startswith("sound/voice/dlc2"):
        return DATA_DIR / "2003.en.pac"
    if voice_sound_path.startswith("sound/voice/dlc3"):
        return DATA_DIR / "3003.en.pac"
    return DATA_DIR / "0024.en.pac"


def locale_path(voice_sound_path):
    assert voice_sound_path.endswith(".sab")
    return voice_sound_path[:-len(".sab")] + ".en.sab"


def extract_sab(voice_sound_path):
    pack = pack_for_path(voice_sound_path)
    internal_path = locale_path(voice_sound_path)
    out_dir = EXTRACT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / internal_path
    if dest.exists():
        return dest
    r = subprocess.run(
        [str(FF16_CLI), "unpack", "-i", str(pack), "-f", internal_path, "-o", str(out_dir)],
        capture_output=True, text=True, env=ENV,
    )
    if not dest.exists():
        raise RuntimeError(f"extraction failed for {internal_path}: {r.stdout[-500:]} {r.stderr[-500:]}")
    return dest


def decode_to_wav(sab_path, wav_path):
    r = subprocess.run([str(VGMSTREAM_CLI), "-o", str(wav_path), str(sab_path)],
                        capture_output=True, text=True)
    if r.returncode != 0 or not Path(wav_path).exists():
        raise RuntimeError(f"decode failed: {r.stdout} {r.stderr}")


def normalize_word(w):
    return re.sub(r"[^a-z']", "", w.lower())


def find_phrase_span(whisper_words, phrase):
    """Returns (start, end, confidence, prev_word_end, next_word_start) or None.
    prev_word_end/next_word_start are hard caps so boundary refinement can't
    bleed into neighboring words when there's no silence gap between them."""
    target = [normalize_word(t) for t in phrase.split()]
    target = [t for t in target if t]
    norm = [normalize_word(w.word) for w in whisper_words]
    n = len(target)
    if n == 0:
        return None
    for i in range(len(norm) - n + 1):
        if norm[i:i + n] == target:
            span_words = whisper_words[i:i + n]
            start = span_words[0].start
            end = span_words[-1].end
            conf = min(w.probability for w in span_words)
            prev_end = whisper_words[i - 1].end if i > 0 else None
            next_start = whisper_words[i + n].start if i + n < len(whisper_words) else None
            return start, end, conf, prev_end, next_start
    return None


def rms_envelope(path, win_sec=0.005):
    w = wave.open(path, 'rb')
    sr = w.getframerate()
    n = w.getnframes()
    frames = w.readframes(n)
    arr = array.array('h')
    arr.frombytes(frames)
    win = max(1, int(sr * win_sec))
    envelope = []
    for i in range(0, len(arr), win):
        chunk = arr[i:i + win]
        if not chunk:
            break
        rms = (sum(x * x for x in chunk) / len(chunk)) ** 0.5
        envelope.append((i / sr, rms))
    return envelope


# A word's acoustic extent routinely spills past ASR's timestamps and contains
# internal near-silent gaps -- most importantly the *plosive closure* in words
# like fuck/fucking/fucked (the brief silent "ck" before the release). If the
# boundary search stops at that closure it cuts mid-word and leaves the release
# or the "-ing" audible (real complaints: "missed the -ing", "missed the last
# half of fuck"). So we GROW through gaps shorter than MAX_GAP and only stop at
# a genuine inter-word silence, always bounded by the neighboring word.
NOISE_FLOOR = 120.0    # RMS below this is "silence" (plosive closures sit here)
MAX_GAP = 0.10         # gaps shorter than this are within-word (plosive), keep going
# ASR word boundaries are imprecise: a plosive release routinely sounds AFTER
# where ASR says the next word starts. Allow the cut to overrun the neighbor's
# ASR boundary by this much so releases/onsets are covered. The cost is a few
# ms of the adjacent clean word clipped -- far preferable to a leaked "...ck".
OVERLAP = 0.10


def refine_end(envelope, whisper_end, hard_cap=None):
    clip_end = envelope[-1][0] if envelope else whisper_end
    limit = min(whisper_end + 1.2, clip_end)
    if hard_cap is not None:
        limit = min(limit, hard_cap + OVERLAP)
    idx = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_end - 0.10), 0)
    last_loud = whisper_end
    end = whisper_end
    for i in range(idx, len(envelope)):
        t, rms = envelope[i]
        if t > limit:
            break
        if rms >= NOISE_FLOOR:
            last_loud = t
            end = t
        elif t - last_loud > MAX_GAP:
            break            # sustained real silence -> word ended at last_loud
    return min(end + 0.07, limit)


def refine_start(envelope, whisper_start, hard_cap=None):
    floor_t = (hard_cap - OVERLAP) if hard_cap is not None else 0.0
    floor_t = max(0.0, floor_t)
    lo = max(floor_t, whisper_start - 1.2)
    idx = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_start + 0.10), len(envelope) - 1)
    last_loud = whisper_start
    start = whisper_start
    for i in range(min(idx, len(envelope) - 1), -1, -1):
        t, rms = envelope[i]
        if t < lo:
            break
        if rms >= NOISE_FLOOR:
            last_loud = t
            start = t
        elif last_loud - t > MAX_GAP:
            break
    return max(floor_t, start - 0.05)


def mod_dest_for(voice_sound_path):
    # mirror the pack-internal path (with locale suffix) under the mod's data folder
    internal_path = locale_path(voice_sound_path)
    return MOD_DIR / internal_path


def phrase_is_last_in_line(line_text, phrase):
    """True if the matched phrase is (approximately) the last word(s) of the
    line's text -- used to decide whether it's suspicious that ASR found no
    word after it."""
    tokens = [normalize_word(t) for t in re.findall(r"[A-Za-z']+", line_text)]
    tokens = [t for t in tokens if t]
    target = [normalize_word(t) for t in phrase.split()]
    target = [t for t in target if t]
    if not tokens or not target:
        return True
    return tokens[-len(target):] == target


def mute_whole_line(sab_src, dest):
    subprocess.run([sys.executable, str(SAB_MUTE), str(sab_src), str(dest)],
                   capture_output=True, text=True, check=True)


SPEECH_RMS = 200.0   # windows above this in a detected-word span count as real audible speech
ALIGN_SCORE_HI = 0.15    # forced-alignment confidence at/above this = trust it outright
PRESENCE_TOKENS = set()  # normalized tokens whose presence in ASR confirms a real swear
_ASR = {}                # lazily-loaded large-v3 for presence checks


def _close(a, bset):
    """True if a is within edit-distance 1 of any token in bset (len>=5 only, so
    short words like 'ass' aren't fuzzily confused). Catches ASR mis-spellings
    like 'bullocks' for 'bollocks'."""
    import difflib
    for b in bset:
        if len(b) >= 5 and abs(len(a) - len(b)) <= 1:
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.86:
                return True
    return False


def asr_profane(path, work_dir, target_tokens=None):
    """Does large-v3 actually hear an enabled swear in this clip? Used only to
    disambiguate low-confidence alignments (is the word really spoken, or is the
    subtitle mismatched?). Deterministic. Fuzzy-matches this line's own target
    tokens so an ASR mis-spelling ('bullocks' for 'bollocks') still counts."""
    if "model" not in _ASR:
        from faster_whisper import WhisperModel
        _ASR["model"] = WhisperModel(os.environ.get("ASR_MODEL", "large-v3"),
                                     device="cpu", compute_type="int8")
    p = Path(path)
    if str(p).endswith(".sab"):
        w = work_dir / "_asrchk.wav"; decode_to_wav(p, w); p = w
    segs, _ = _ASR["model"].transcribe(str(p), language="en", temperature=0.0,
                                       condition_on_previous_text=False)
    text = " ".join(s.text for s in segs).lower()
    tgt = set(normalize_word(t) for p in (target_tokens or []) for t in p.split()) - {""}
    for tok in text.split():
        n = re.sub(r"[^a-z']", "", tok)
        if not n:
            continue
        if n in PRESENCE_TOKENS or n.startswith(("fuck", "shit")):
            return True
        if tgt and _close(n, tgt):
            return True
    return False


def find_all_spans(all_words, tokens):
    """Every occurrence of every token, as refine-ready spans. Overlapping/dup
    spans are de-duplicated. Returns list of dicts sorted by start."""
    norm = [normalize_word(w.word) for w in all_words]
    spans = []
    for phrase in tokens:
        target = [normalize_word(t) for t in phrase.split()]
        target = [t for t in target if t]
        n = len(target)
        if n == 0:
            continue
        for i in range(len(norm) - n + 1):
            if norm[i:i + n] == target:
                sw = all_words[i:i + n]
                spans.append({
                    "start": sw[0].start, "end": sw[-1].end,
                    "conf": min(w.probability for w in sw),
                    "prev_end": all_words[i - 1].end if i > 0 else None,
                    "next_start": all_words[i + n].start if i + n < len(all_words) else None,
                    "idx": i, "n": n,
                })
    spans.sort(key=lambda s: s["start"])
    # drop dups (same start idx)
    seen = set(); out = []
    for s in spans:
        if s["idx"] in seen:
            continue
        seen.add(s["idx"]); out.append(s)
    return out


def find_spans_positional(all_words, line_text, tokens):
    """Locate the profane word by POSITION when ASR spells it differently than
    the subtitle (British 'arse' -> ASR 'ass', 'arseache' -> 'arse ache', etc.).
    We know the exact line text from the .pzd, so align the text word-sequence to
    the ASR word-sequence and read off the ASR word(s) sitting where the profane
    word is in the text -- even if ASR mis-transcribed it."""
    import difflib
    text_words = [normalize_word(w) for w in re.findall(r"[A-Za-z']+", line_text)]
    text_words = [w for w in text_words if w]
    asr_norm = [normalize_word(w.word) for w in all_words]
    targets = set(normalize_word(t) for p in tokens for t in p.split()) - {""}
    tgt_positions = [i for i, w in enumerate(text_words) if w in targets]
    if not tgt_positions or not asr_norm:
        return []
    sm = difflib.SequenceMatcher(None, text_words, asr_norm, autojunk=False)
    # map each text index -> an asr index
    t2a = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                t2a[i1 + k] = j1 + k
        elif tag == "replace":
            for k in range(i1, i2):
                # proportional map into the asr replacement block
                off = 0 if (i2 - i1) <= 1 else int((k - i1) / (i2 - i1) * (j2 - j1))
                t2a[k] = min(j2 - 1, j1 + off)
        # delete/insert: no direct mapping
    spans = []
    for tp in tgt_positions:
        ai = t2a.get(tp)
        if ai is None or ai >= len(all_words):
            continue
        w = all_words[ai]
        spans.append({
            "start": w.start, "end": w.end,
            "conf": max(0.15, w.probability),   # positional match -> trust it enough to attempt
            "prev_end": all_words[ai - 1].end if ai > 0 else None,
            "next_start": all_words[ai + 1].start if ai + 1 < len(all_words) else None,
            "idx": ai, "n": 1,
        })
    spans.sort(key=lambda s: s["start"])
    seen = set(); out = []
    for s in spans:
        if s["idx"] in seen:
            continue
        seen.add(s["idx"]); out.append(s)
    return out


def refine_cut(envelope, span):
    r_start = refine_start(envelope, span["start"], hard_cap=span["prev_end"])
    r_end = refine_end(envelope, span["end"], hard_cap=span["next_start"])
    nxt = span["next_start"]
    pad = 0.05 if nxt is None else min(0.05, max(0.0, (nxt - r_end) / 2))
    return max(0.0, r_start - 0.02), r_end + pad


def apply_cuts(sab_src, dest, cuts):
    args = [sys.executable, str(SAB_MUTE), str(sab_src), str(dest)]
    for s, e in cuts:
        args += [f"{s:.3f}", f"{e:.3f}"]
    subprocess.run(args, capture_output=True, text=True, check=True)


ENABLED_TOKENS = set()   # every normalized token across the enabled concepts; set in main()


def audible_target_regions(model, dest_sab, tokens, work_dir):
    """Re-transcribe the muted file WITH word timestamps. For every ENABLED
    profanity token still reported over real audible energy, return its region so
    we can cut it too. Two subtleties this handles:
      - Hallucination: ASR fills a muted word back over silence (e.g. 'damn'
        before a surviving 'it!'). Such a detection sits over near-silence and is
        ignored (energy fraction check).
      - Re-labelling: a residual left by a mislocated cut is often transcribed as
        a DIFFERENT profane word than the line's original (leftover 'fucking'
        heard as 'shit'; 'dammit' heard as 'damn it'). So we must check the FULL
        enabled-token set, not just this line's word -- otherwise the residual
        slips past the self-check (this was a real leak the gate caught)."""
    check = ENABLED_TOKENS if ENABLED_TOKENS else set(
        normalize_word(t) for p in tokens for t in p.split()) - {""}
    verify_wav = work_dir / "verify.wav"
    try:
        decode_to_wav(dest_sab, verify_wav)
    except Exception:
        return [(0.0, 9999.0)]  # can't verify -> force escalation
    env = rms_envelope(str(verify_wav))
    segments, _ = model.transcribe(str(verify_wav), word_timestamps=True, language="en",
                                   temperature=0.0, condition_on_previous_text=False)
    words = [w for seg in segments for w in seg.words]
    real = []
    for w in words:
        if normalize_word(w.word) in check:
            wins = [rms for (t, rms) in env if w.start <= t <= w.end]
            if wins:
                frac = sum(1 for r in wins if r > SPEECH_RMS) / len(wins)
                if frac > 0.25:  # a real audible chunk, not a hallucination over silence
                    real.append((w.start, w.end))
    return real


# --- Forced alignment (MMS_FA) -------------------------------------------------
# We know each line's exact subtitle text from the .pzd, so instead of trusting
# Whisper's (unreliable) word timestamps we force-align that known text to the
# audio. This gives accurate per-word boundaries -- it knows "fucking" is one
# word ending after "-ing", and where a mis-pronounced British "arse" sits --
# which fixed the systematic "cut ends too early, leaves the back half" problem.
_ALIGN = {}


def load_aligner():
    import torch, torchaudio
    torch.set_num_threads(int(os.environ.get("ALIGN_THREADS", "8")))
    b = torchaudio.pipelines.MMS_FA
    _ALIGN.update(bundle=b, model=b.get_model(), tok=b.get_tokenizer(),
                  align=b.get_aligner(), torch=torch, ta=torchaudio)


def align_words(wav_path, text):
    """Force-align the known text to the audio. Returns (aligned, full) where
    aligned = [(word, start, end), ...] and full = True if the whole text was
    aligned. Some .pzd lines are segmented: the text is the full line but this
    .sab holds only a fragment, so the token sequence is longer than the audio
    can support -- in that case we align the largest fitting prefix and report
    full=False (the caller then knows any un-aligned words aren't in this clip)."""
    torch = _ALIGN["torch"]; ta = _ALIGN["ta"]; b = _ALIGN["bundle"]
    w = wave.open(str(wav_path), 'rb'); ch = w.getnchannels(); sr = w.getframerate()
    a = array.array('h'); a.frombytes(w.readframes(w.getnframes()))
    if not a:
        return [], False
    t = torch.tensor(a, dtype=torch.float32).view(-1, ch).mean(1) / 32768.0
    wav = ta.functional.resample(t.unsqueeze(0), sr, b.sample_rate)
    words = re.findall(r"[A-Za-z']+", text.lower())
    if not words:
        return [], False

    def try_align(ws):
        with torch.inference_mode():
            emission, _ = _ALIGN["model"](wav)
            spans = _ALIGN["align"](emission[0], _ALIGN["tok"](ws))
        ratio = wav.size(1) / emission.size(1) / b.sample_rate
        out = []
        for wr, sp in zip(ws, spans):
            score = sum(s.score for s in sp) / len(sp)   # alignment confidence
            out.append((wr, sp[0].start * ratio, sp[-1].end * ratio, score))
        return out

    try:
        return try_align(words), True
    except Exception:
        pass
    # token sequence too long for the audio -> largest fitting prefix
    lo, hi, best = 1, len(words), []
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            best = try_align(words[:mid]); lo = mid + 1
        except Exception:
            hi = mid - 1
    return best, (len(best) == len(words))


def process_match(model, match, work_dir):
    voice_path = match["voice_sound_path"]
    tokens = match["matched_words"]
    result = {"id": match["id"], "voice_sound_path": voice_path, "matched_words": tokens}

    sab_src = extract_sab(voice_path)
    dest = mod_dest_for(voice_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if SAFE_MODE == "whole_line":
        mute_whole_line(sab_src, dest)
        result.update(method="whole_line", reason="safe_mode")
        return result

    wav_path = work_dir / "clip.wav"
    decode_to_wav(sab_src, wav_path)
    envelope = rms_envelope(str(wav_path))

    aligned, full = align_words(wav_path, match.get("line", ""))
    if not aligned:
        mute_whole_line(sab_src, dest)
        result.update(method="whole_line_fallback", reason="alignment_failed")
        return result

    check = ENABLED_TOKENS if ENABLED_TOKENS else set(normalize_word(t) for p in tokens for t in p.split()) - {""}
    prof = [(i, wr, ws, we, sc) for i, (wr, ws, we, sc) in enumerate(aligned) if normalize_word(wr) in check]

    # The alignment score alone can't tell a mis-matched subtitle from a
    # correctly-aligned-but-low-scoring line (both ~0.05). So when confidence is
    # low we let ASR decide whether the word is actually SPOKEN in this clip:
    #  - MMS forced alignment gives the accurate boundaries (used to cut);
    #  - large-v3 gives reliable presence ("is there really a swear here?").
    hi_conf = [p for p in prof if p[4] >= ALIGN_SCORE_HI]
    lo_conf = [p for p in prof if p[4] < ALIGN_SCORE_HI]

    asr_says_profane = None
    if lo_conf or not prof:
        asr_says_profane = asr_profane(wav_path, work_dir, tokens)

    to_cut = list(hi_conf)
    if lo_conf and asr_says_profane:
        to_cut += lo_conf            # ASR confirms a swear is here -> trust the alignment
    to_cut.sort(key=lambda p: p[0])

    def cut_for(i, ws, we):
        prev_end = aligned[i - 1][2] if i > 0 else None
        nxt_start = aligned[i + 1][1] if i + 1 < len(aligned) else None
        s = ws - 0.05
        e = we + 0.10                       # generous tail: cover the plosive release
        if prev_end is not None:
            s = max(s, prev_end - 0.03)
        if nxt_start is not None:
            e = min(e, nxt_start + 0.04)
        return (max(0.0, s), e)

    cuts = [cut_for(i, ws, we) for (i, wr, ws, we, sc) in to_cut]

    if not cuts:
        # No profanity to cut here.
        if asr_says_profane:
            # ASR hears a swear but alignment couldn't place it (short mismatch
            # clip, heavy reverb, etc.) -> guarantee removal with a whole-clip mute.
            mute_whole_line(sab_src, dest)
            result.update(method="whole_line_asr_present", reason="profanity_heard_not_located")
        else:
            # The subtitle profanity isn't actually voiced in this clip (FFXVI's
            # text/audio-mismatched simplevoice entries) -> leave it untouched.
            if dest.exists():
                dest.unlink()
            result.update(method="skipped_not_in_clip", reason="profanity_not_in_audio")
        return result

    apply_cuts(sab_src, dest, cuts)
    # Safety net for the low-confidence cuts: confirm ASR can no longer hear a
    # swear in the muted result; if it can, the alignment was wrong -> whole-clip.
    if lo_conf and asr_says_profane and asr_profane(dest, work_dir, tokens):
        mute_whole_line(sab_src, dest)
        result.update(method="word_level_escalated_to_whole_line",
                      reason="still_audible_after_aligned_cut")
        return result
    result.update(method="word_level", cuts=[[round(s, 3), round(e, 3)] for s, e in cuts],
                  num_occurrences=len(cuts), located_by="forced_alignment")
    return result


def write_mod_config():
    # MOD_DIR is .../<modid>/FFXVI/data -- the mod root (where ModConfig.json
    # belongs) is two levels up. Without this file Reloaded-II won't
    # recognize the folder as a mod at all.
    mod_root = MOD_DIR.parent.parent
    config_path = mod_root / "ModConfig.json"
    if config_path.exists():
        return
    mod_root.mkdir(parents=True, exist_ok=True)
    config = {
        "ModId": mod_root.name,
        "ModName": "ProFFanXVI - Profanity Filter",
        "ModAuthor": "generated by ProFFanXVI batch_mute_pipeline.py",
        "ModVersion": "0.1.0",
        "ModDescription": "Mutes profanity in FFXVI's voice dialogue. See batch_pipeline_report.json for which lines got precise word-level cuts vs. safe whole-line fallback.",
        "ModDll": "",
        "ModIcon": "",
        "Tags": [],
        "CanUnload": None,
        "HasExports": None,
        "IsLibrary": False,
        "IsUniversalMod": True,
        "ModDependencies": [],
        "OptionalDependencies": [],
        "SupportedAppId": ["ffxvi.exe"],
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote {config_path}")


def main():
    editlist_path = sys.argv[1] if len(sys.argv) > 1 else "profanity_editlist.json"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].strip() else None
    # default report name is derived from the editlist name so running against
    # a different editlist (e.g. a delta) can't silently clobber a previous
    # run's report
    default_report_name = f"batch_pipeline_report_{Path(editlist_path).stem}.json"
    report_path = Path(sys.argv[3]) if len(sys.argv) > 3 else TOOLS_DIR / default_report_name

    model = None
    if SAFE_MODE != "whole_line":
        print("Loading forced aligner (torchaudio MMS_FA)...")
        load_aligner()
    else:
        print("SAFE_MODE=whole_line: muting entire lines, no alignment needed.")

    data = json.loads(Path(editlist_path).read_text(encoding="utf-8"))
    matches = data["matches"]
    if limit:
        matches = matches[:limit]

    # Build the full enabled-token set so the self-verify can catch residual
    # profanity even when ASR re-labels it as a different profane word than the
    # line's original ('fucking' leftover heard as 'shit', 'dammit' as 'damn it').
    global ENABLED_TOKENS
    try:
        wl_path = os.environ.get("WORDLIST",
                                 str(Path(__file__).parent / "profanity_wordlist.json"))
        wl = json.loads(Path(wl_path).read_text(encoding="utf-8"))
        enabled = data.get("enabled_concepts")
        enabled_set = None if enabled in (None, "all") else set(enabled)
        for c in wl.get("concepts", []):
            if enabled_set is None or c["id"] in enabled_set:
                for tok in c["tokens"]:
                    for part in tok.split():
                        ENABLED_TOKENS.add(normalize_word(part))
        ENABLED_TOKENS.discard("")
        global PRESENCE_TOKENS
        PRESENCE_TOKENS = set(ENABLED_TOKENS)
        if "arse" in ENABLED_TOKENS or "arses" in ENABLED_TOKENS:
            PRESENCE_TOKENS |= {"ass", "asses", "asshole", "assholes"}  # ASR spells 'arse' as 'ass'
        print(f"Self-verify checks {len(ENABLED_TOKENS)} enabled profanity tokens.")
    except Exception as e:
        print(f"WARN: could not build enabled-token set ({e}); verify falls back to per-line tokens.")

    work_dir = TOOLS_DIR / "_batchwork"
    work_dir.mkdir(exist_ok=True)

    write_mod_config()

    results = []
    errors = []
    for i, match in enumerate(matches):
        try:
            r = process_match(model, match, work_dir)
            results.append(r)
            print(f"[{i+1}/{len(matches)}] {r['method']:20s} {match['voice_sound_path']}")
        except Exception as e:
            errors.append({"id": match["id"], "voice_sound_path": match["voice_sound_path"], "error": str(e)})
            print(f"[{i+1}/{len(matches)}] ERROR: {match['voice_sound_path']}: {e}")

    by_method = {}
    for r in results:
        by_method[r["method"]] = by_method.get(r["method"], 0) + 1

    report = {
        "total_matches": len(matches),
        "succeeded": len(results),
        "failed": len(errors),
        "safe_mode": SAFE_MODE,
        "whisper_model": WHISPER_MODEL if SAFE_MODE != "whole_line" else None,
        "by_method": by_method,
        "results": results,
        "errors": errors,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone. Methods: {by_method}. Errors: {len(errors)}.")
    print(f"Report: {report_path}")
    print(f"Mod folder: {MOD_DIR}")


if __name__ == "__main__":
    main()
