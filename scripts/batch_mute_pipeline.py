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


def refine_end(envelope, whisper_end, hard_cap=None, noise_floor=60.0, sustain_sec=0.08, win_sec=0.005):
    """Find where the target word truly ends. Walk forward from the ASR end
    marker looking for the first sustained silence (that's the real end of the
    word's acoustic tail, which routinely extends past ASR's timestamp). Never
    search past hard_cap (the next word's own start) so we don't eat a
    neighbor. If no silence is found before the limit, the word's sound runs
    right up to that limit, so we must cut all the way TO the limit -- returning
    the ASR end here would leave the word's tail audible (a real leak seen with
    single-word clips like 'SHIT!' where the tail ran to the clip end)."""
    clip_end = envelope[-1][0] if envelope else whisper_end
    sustain_windows = max(1, int(sustain_sec / win_sec))
    search_limit = min(whisper_end + 1.0, clip_end)
    if hard_cap is not None:
        search_limit = min(search_limit, hard_cap)
    idx_start = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_end - 0.05), 0)
    idx_limit = next((i for i, (t, _) in enumerate(envelope) if t >= search_limit), len(envelope))
    for i in range(idx_start, min(idx_limit, len(envelope) - sustain_windows)):
        window = envelope[i:i + sustain_windows]
        if all(rms < noise_floor for _, rms in window):
            return envelope[i][0]
    # no clean silence before the limit -> the word is still sounding there;
    # cut all the way to the limit (next word start, or clip end).
    return search_limit


def refine_start(envelope, whisper_start, hard_cap=None, noise_floor=60.0, lookback_sec=0.15):
    search_floor = whisper_start - lookback_sec
    if hard_cap is not None:
        search_floor = max(search_floor, hard_cap)
    idx_anchor = next((i for i, (t, _) in enumerate(envelope) if t >= whisper_start), 0)
    idx_floor = next((i for i, (t, _) in enumerate(envelope) if t >= search_floor), 0)
    for i in range(idx_anchor, idx_floor, -1):
        if envelope[i][1] < noise_floor:
            return envelope[i][0]
    return search_floor if hard_cap is not None else whisper_start


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


def audible_target_regions(model, dest_sab, tokens, work_dir):
    """Re-transcribe the muted file WITH word timestamps. For every target token
    still reported, measure how much of its detected span is actually above the
    speech floor in the muted audio. A token reported over silence is an ASR
    hallucination (e.g. it fills 'damn' back in before a surviving 'it!') and is
    ignored; a token over real energy is a genuine remaining instance and its
    span is returned so we can cut it too. Returns list of (start,end) real
    regions (empty = clean)."""
    verify_wav = work_dir / "verify.wav"
    try:
        decode_to_wav(dest_sab, verify_wav)
    except Exception:
        return [(0.0, 9999.0)]  # can't verify -> force escalation
    env = rms_envelope(str(verify_wav))
    segments, _ = model.transcribe(str(verify_wav), word_timestamps=True, language="en")
    words = [w for seg in segments for w in seg.words]
    tnorm = set(normalize_word(t) for p in tokens for t in p.split())
    tnorm.discard("")
    real = []
    for w in words:
        if normalize_word(w.word) in tnorm:
            wins = [rms for (t, rms) in env if w.start <= t <= w.end]
            if wins:
                frac = sum(1 for r in wins if r > SPEECH_RMS) / len(wins)
                if frac > 0.25:  # a real audible chunk, not a hallucination over silence
                    real.append((w.start, w.end))
    return real


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
    segments, info = model.transcribe(str(wav_path), word_timestamps=True, language="en")
    all_words = [w for seg in segments for w in seg.words]

    spans = find_all_spans(all_words, tokens)
    spans = [s for s in spans if (s["end"] - s["start"]) >= 0.05 and s["conf"] >= 0.15]

    if not spans:
        mute_whole_line(sab_src, dest)
        result.update(method="whole_line_fallback", reason="no_confident_asr_match")
        return result

    envelope = rms_envelope(str(wav_path))
    cuts = [refine_cut(envelope, s) for s in spans]

    # Iteratively cut, then re-verify with energy awareness. Add any genuinely
    # still-audible target regions and re-cut. Escalate only if truly stuck.
    escalated = False
    for attempt in range(3):
        apply_cuts(sab_src, dest, cuts)
        real = audible_target_regions(model, dest, tokens, work_dir)
        if not real:
            break
        # widen: add the still-audible regions (with a little pad) to the cut set
        for (s, e) in real:
            cuts.append((max(0.0, s - 0.05), e + 0.08))
    else:
        # after retries a real audible instance remains -> guarantee via whole-line
        mute_whole_line(sab_src, dest)
        escalated = True

    if escalated:
        result.update(method="word_level_escalated_to_whole_line",
                      reason="target_still_audible_after_retries", num_spans=len(spans))
    else:
        result.update(method="word_level", cuts=[[round(s, 3), round(e, 3)] for s, e in cuts],
                      num_occurrences=len(spans), self_verified=True)
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
        from faster_whisper import WhisperModel
        print(f"Loading whisper model '{WHISPER_MODEL}' ({WHISPER_DEVICE}/{WHISPER_COMPUTE})...")
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    else:
        print("SAFE_MODE=whole_line: muting entire lines, no ASR needed.")

    data = json.loads(Path(editlist_path).read_text(encoding="utf-8"))
    matches = data["matches"]
    if limit:
        matches = matches[:limit]

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
