# ProFFanXVI Mod

A tool to detect and mute profanity in Final Fantasy XVI's voice/dialogue audio, for players who own the game on Steam and want a cleaner audio pass.

## Status: confirmed working end-to-end, including in-game

Muted the game's actual opening line ("It was Moss the Chronicler who said...") via the full pipeline (extract → mute → repack → Reloaded-II) and confirmed in-game: the line played silent, the next line was unaffected (no bleed, no desync).

### Word-level muting confirmed (not just whole-line)

Whisper's word timestamps aren't trustworthy as-is — on this exact clip it reported "Moss" spanning 0.68s-1.96s at 6% confidence, but the real energy envelope shows the word ends around 1.05-1.1s followed by a genuine dramatic pause before "the chronicler". `scripts/word_align.py` anchors off the ASR timestamp then refines the boundary against the actual RMS energy envelope (walks forward/backward to the true silence crossing, requiring a sustained quiet window so it doesn't stop on a brief consonant dip).

Muting just the refined "Moss" range (0.68s-1.19s) instead of the whole clip, verified by RMS comparison against the original:

| Region | Original RMS | Muted RMS |
|---|---|---|
| "was" (before target) | 2825 | 2819 (unchanged) |
| "Moss" (target) | 2135 | 12 (99.4% reduction) |
| "the/chronicler" (after target) | 2521 | 2508 (unchanged) |
| "who/said" (after target) | 2416 | 2391 (unchanged) |

Sample count identical before/after (166,357). `scripts/sab_mute.py` now accepts an optional `start_sec end_sec` range and only touches that window, leaving the rest of the line byte-for-byte equivalent (modulo normal lossy re-encode noise, ~1%).

## Feasibility findings

- **Detection**: FFXVI's `.pzd` dialogue files carry the exact subtitle text (`Line:`) *and* the exact relative audio path (`VoiceSoundPath:`) for every voice line. Profanity detection is a deterministic text search — no ASR/Whisper transcription needed for the ~41,820 standalone dialogue/bark files in `0024.en.pac`.
- **Audio format**: `.sab` (Square Enix "SEAD" engine) is a documented container — verified byte-for-byte against vgmstream's open-source parser (`src/meta/sqex_sead.c`) — wrapping a CRI HCA stream.
- **Round-trip proven**: `scripts/sab_mute.py` parses a real `.sab`, decodes the embedded HCA, mutes it, re-encodes, and patches it back in place. Verified against a real extracted line (DLC2 boss-battle bark, "Clive."):
  - Original: 27,468 samples, peak amplitude 21,573, RMS 4,791.
  - Muted: 27,468 samples (**identical duration**), peak 0, RMS 0.0.
  - vgmstream parses the patched file cleanly with identical metadata (sample rate, channels, bitrate).
- **Cutscenes**: story movies are Bink2 (`.bk2`), audio baked into video — 105 files total (95 base + 10 DLC), confirmed via pack index. Extracted files start with a clean, unwrapped `KB2n` magic (no disguising header needed here, unlike the older FFXV-era trick). However, revision `n` is newer than what any open-source tool currently parses — checked ffmpeg's own bink demuxer source (`libavformat/bink.c`, latest master as of 2026-07): it only recognizes revisions `i`/`j`/`k`. That means audio-track replacement for cutscenes genuinely needs RAD's own official Video Tools (the actual creators of the codec), not a reverse-engineered path — and that tool is GUI-driven (its self-extracting installer wouldn't launch cleanly in an unattended/scripted context). **This step needs to be run by a human with hands on the keyboard**, not automated further.

### Profanity scanner

`scripts/scan_profanity.py` + `scripts/profanity_wordlist.json`: scans every extracted `.pzd`→`.yaml` dialogue file's `Line:` text against a word-boundary-matched, severity-tiered wordlist, and emits an edit-list (line ID, matched word(s), severity, exact `VoiceSoundPath`) — no audio decoding needed for detection at all.

First run against base game + DLC2 + DLC3 text (6,127 `.pzd` files, 27,818 individual dialogue lines total): 395 matches. Spot-checked for false positives, including the highest-risk word ("ass", prone to matching inside "class"/"grass"/"assassin") — word-boundary regex correctly returned only 1 genuine hit, no substring contamination.

Iterated on the wordlist twice based on actual usage data from the scan:
- Added "bloody" to the mild tier after noticing it appears standalone (not just in "bloody hell") in 216 dialogue files — FFXVI's dialogue is notably British in style. Brought the total to 487.
- Then removed "bloody", "hell", "piss", "pissed", and "crap" entirely after review (kept "damn"/"damned"/"ass") — these are common, mild British-English intensifiers that came back much more often than anything else flagged and didn't need muting. **Current total: 294 matches** (103 severe, 87 moderate, 104 mild).

The generated edit-list itself (which necessarily contains real game dialogue quotes) is **not committed to this repo** — same "don't redistribute copyrighted content" principle as the audio. Run the scanner yourself against your own extracted files to regenerate it locally.

### Full batch pipeline

`scripts/batch_mute_pipeline.py` ties everything together: takes the scanner's edit-list, and for every match extracts the real `.sab`, finds the flagged word's precise timing (faster-whisper word timestamps + energy-envelope refinement, bounded by the neighboring word's own ASR boundary so it can't bleed across a word with no pause between them), mutes just that word, and assembles a ready-to-use Reloaded-II mod folder.

Every result is tagged `word_level` or `whole_line_fallback` (with a reason) so nothing gets a guessed boundary it can't back up: low ASR confidence, a line whose text implies trailing words that ASR didn't detect at all, or (see below) a degenerate zero-width ASR timestamp all fall back to the safe, fully-verified whole-line mute instead.

**Final state after processing the full 294-match wordlist** (across the original run, a "bloody"-expansion delta, then reconciling back down after excluding those 5 words — see commit history for the blow-by-blow): 290 of 294 matches have a muted file in the mod folder; the other 4 are the same `simpleq`/side-quest lines whose `.pzd` text entry has no corresponding shipped English audio in any pack tested (consistent ~1-6% rate across multiple runs — orphaned cut-content database rows, not a pipeline bug).

Three real bugs were caught and fixed by actually verifying output rather than trusting the pipeline's self-report:
1. **Boundary bleed** — with no pause between words ("Fucking dog!"), the energy-based refinement search had no cap and bled 0.8s into the neighboring word. Fixed by bounding the search at the next/previous word's own ASR timestamp.
2. **Missing `ModConfig.json`** — the pipeline was producing muted files but not the config Reloaded-II needs to recognize the folder as a mod at all.
3. **Degenerate zero-width ASR spans** — Whisper occasionally reports a word's start == end (seen in practice: three consecutive words colliding at the same instant in fast speech), which produces a mute range too short to silence anything. The pipeline now treats a sub-50ms ASR span as untrustworthy and falls back to whole-line muting. Caught by an independent RMS re-verification pass that specifically checks for *any* genuinely silenced region in the clip (not a whole-clip average, which dilutes a single muted word into meaninglessness across a multi-second line — an earlier, cruder version of the check falsely flagged 82/290 files before this was corrected).

After all three fixes, an independent re-verification (re-decoding every output and checking for a real silenced region, not trusting self-reports) passed **290/290**.

## Architecture decision: local tool, not redistributed assets

This project does **not** ship modified Square Enix audio/pack files. It ships:
1. A profanity "edit list" (line ID → mute timestamps), built from the `.pzd` text scan — safe to publish, it's just metadata.
2. A local tool that applies that edit list against *your own* legally-owned game install, via the same extract → mute → repack → Reloaded-II pipeline proven above.

This avoids redistributing Square Enix's copyrighted audio while still being a one-click experience for other players.

## Requirements
- FFXVI on Steam (PC)
- .NET runtime (8, 9, or 10 -- set `DOTNET_ROLL_FORWARD=LatestMajor` if you only have one that isn't exactly 9)
- Python 3.10+, `pip install faster-whisper pyyaml`
- [Reloaded-II](https://github.com/Reloaded-Project/Reloaded-II) + [ff16.utility.modloader](https://github.com/Nenkai/ff16.utility.modloader) (Nenkai) -- portable install; mods go directly in `<ReloadedII>/Mods/<modid>/`, drag-and-drop onto the app window doesn't do anything
- [FF16Tools](https://github.com/Nenkai/FF16Tools) (Nenkai)
- [vgmstream](https://github.com/vgmstream/vgmstream) (`vgmstream-cli.exe`)
- [VGAudio](https://github.com/Thealexbarney/VGAudio) (`VGAudioCli.exe`, for HCA encode)

## Usage

1. Extract and convert all dialogue text:
   ```
   python scripts/extract_dialogue_text.py --game-data "<Steam>/FINAL FANTASY XVI/data" --ff16-cli "<path>/FF16Tools.CLI.exe" --out extracted
   ```
2. Scan for profanity:
   ```
   python scripts/scan_profanity.py extracted -o profanity_editlist.json
   ```
3. Run the batch pipeline (set the env vars below to match where you put things, or edit the defaults in the script):
   ```
   set FF16_CLI=<path>\FF16Tools.CLI.exe
   set VGAUDIOCLI=<path>\VGAudioCli.exe
   set VGMSTREAM_CLI=<path>\vgmstream-cli.exe
   set FFXVI_DATA_DIR=<Steam>\FINAL FANTASY XVI\data
   set MOD_OUTPUT_DIR=<ReloadedII>\Mods\ff16.audio.profanity-filter\FFXVI\data
   python scripts/batch_mute_pipeline.py profanity_editlist.json
   ```
4. Add a `ModConfig.json` next to the mod's `FFXVI/` folder (see `scripts/` for reference), enable it in Reloaded-II alongside `ff16.utility.modloader`, and launch.

Cutscene (`.bk2`) audio isn't covered by this pipeline yet -- see the Bink findings above.

## Credits
Format documentation and tooling built on the [FFXVI Modding](https://nenkai.github.io/ffxvi-modding/) community project by Nenkai.
