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

## Architecture decision: local tool, not redistributed assets

This project does **not** ship modified Square Enix audio/pack files. It ships:
1. A profanity "edit list" (line ID → mute timestamps), built from the `.pzd` text scan — safe to publish, it's just metadata.
2. A local tool that applies that edit list against *your own* legally-owned game install, via the same extract → mute → repack → Reloaded-II pipeline proven above.

This avoids redistributing Square Enix's copyrighted audio while still being a one-click experience for other players.

## Requirements
- FFXVI on Steam (PC)
- [Reloaded-II](https://github.com/Reloaded-Project/Reloaded-II) + [ff16.utility.modloader](https://github.com/Nenkai/ff16.utility.modloader) (Nenkai)
- [FF16Tools](https://github.com/Nenkai/FF16Tools) (Nenkai)
- [vgmstream](https://github.com/vgmstream/vgmstream)
- [VGAudio](https://github.com/Thealexbarney/VGAudio) (VGAudioCli, for HCA encode)

## Credits
Format documentation and tooling built on the [FFXVI Modding](https://nenkai.github.io/ffxvi-modding/) community project by Nenkai.
