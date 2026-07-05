# ProFFanXVI

Mute profanity in **Final Fantasy XVI**'s spoken dialogue, for players who own the game on Steam and want a cleaner audio pass. You pick exactly which words to filter; the tool builds a [Reloaded-II](https://github.com/Reloaded-Project/Reloaded-II) mod that silences them in-game.

It does **not** redistribute any game audio. It runs against *your own* legally-owned install and produces the mod locally.

---

## Quick start (GUI)

```
pip install -r requirements.txt
python proffanxvi_gui.py
```

1. **Paths tab** – click *Auto-detect* (finds a Steam FFXVI install and bundled `./tools`), or point each field at the matching tool. Download links are under [Requirements](#requirements).
2. **Profanity tab** – tick the words to mute. Click *Scan game text* to see how many lines each one hits in your copy of the game.
3. **Build tab** – choose a muting mode, click *Build mod*, then enable it in Reloaded-II alongside `ff16.utility.modloader`.

Prefer the command line? See [Manual usage](#manual-usage).

---

## How it works

FFXVI stores each spoken line's **subtitle text and its exact audio-file path together** in the game's `.pzd` dialogue tables. So detecting profanity is a precise text search over the real subtitles — no guessing, no transcribing needed to *find* anything. Each match points straight at the `.sab` audio file to mute.

The audio itself is Square Enix's `.sab` container (the "SEAD" engine) wrapping a CRI HCA stream — a format documented in [vgmstream](https://github.com/vgmstream/vgmstream)'s open-source parser, which let the muter patch audio back in place without breaking the container. Muting preserves the exact clip length, so lip-sync and event timing are untouched.

Cutscene movies (`.bk2` Bink video) were checked and contain **no audio track at all** (verified with RAD's own Video Tools: "No sound found in the Bink file") — the dialogue you hear over them is played through the same `.sab` system, so nothing special is needed for cutscenes.

## Detection completeness

Because the stakes for this kind of filter are "one missed word ruins it," detection was audited against the **entire** dialogue corpus (27,818 lines), not just a hand-guessed wordlist:

- The wordlist is **concept-based**: each concept (e.g. `bastard`) enumerates *every inflected/possessive form that actually appears in the game* (`bastard`, `bastards`, `bastard's`, `bastards'll`, …). This fixes the classic trap where a naive `\bbastard\b` silently misses the plural "bastards" — which alone is 52 lines in FFXVI.
- The forms were derived empirically by scanning the whole vocabulary (12,063 unique tokens), including a wide sweep for British swearing (FFXVI is heavily British-voiced: `arse`, `bugger`, `sod`, `bollocks`, `prick`, `shite`, …) and for slurs (none present).
- `scripts/verify_coverage.py` is a **zero-leak self-test**: it independently re-scans the corpus and asserts that *every* occurrence of *every* enabled token appears in the edit-list. If anything could slip through, it fails loudly.
- Matching is exact-token with word boundaries, so it never touches innocent substrings (`pass`, `assist`, `class`, `shattered`, "a chink in the armor").

Only ~300 voice files (combat barks like `vo_bh###.sab`) have no subtitle text; these are checked separately by transcribing them directly (see `scripts/` / the report), since a text scan can't reach them.

## Muting modes

- **Accurate (default)** – silences just the profane word, keeping the surrounding dialogue. It locates the word with Whisper (`large-v3` by default for best timing), refines the cut to the real audio energy boundaries, and then **self-verifies**: it re-transcribes the muted clip and, if the word is still audible at all, automatically escalates that line to a whole-line mute. So a mislocated cut can never leak — worst case it over-mutes.
- **Safe (whole-line)** – silences the entire line containing any profanity. Bulletproof and fast (no speech recognition), at the cost of losing a few seconds of surrounding clean dialogue per flagged line.

Every processed line is tagged in the report with which method it got and why.

## Review without spoilers

`scripts/build_listen_kit.py` (or the GUI's *Build listen-kit* button) produces an offline `index.html` with an audio player for every muted line, so you can **hear-confirm the mutes in a browser without launching the game**. The muted clip is front-and-center; the original audio and full subtitle text are hidden behind per-row *reveal* toggles to limit story spoilers.

## Manual usage

```bat
:: 1. Extract & convert dialogue text
python scripts/extract_dialogue_text.py --game-data "<Steam>\FINAL FANTASY XVI\data" --ff16-cli "<path>\FF16Tools.CLI.exe" --out extracted

:: 2. Scan (choose concepts; omit --enable to use defaults, or --all for everything)
python scripts/scan_profanity.py extracted --enable fuck,shit,bastard,arse,bugger,sod,prick,bollocks,whore,damn -o editlist.json

:: 3. Prove zero leaks for that selection
python scripts/verify_coverage.py extracted -e editlist.json

:: 4. Build the mod
set FF16_CLI=<path>\FF16Tools.CLI.exe
set VGAUDIOCLI=<path>\VGAudioCli.exe
set VGMSTREAM_CLI=<path>\vgmstream-cli.exe
set FFXVI_DATA_DIR=<Steam>\FINAL FANTASY XVI\data
set MOD_OUTPUT_DIR=<ReloadedII>\Mods\ff16.audio.profanity-filter\FFXVI\data
set SAFE_MODE=word_level        &:: or whole_line
set WHISPER_MODEL=large-v3      &:: or medium.en / small.en for speed
python scripts/batch_mute_pipeline.py editlist.json
```

Then enable `ff16.audio.profanity-filter` (auto-created with its `ModConfig.json`) in Reloaded-II.

## Requirements

- FFXVI on Steam (PC)
- Python 3.10+ (`pip install -r requirements.txt` → `faster-whisper`, `pyyaml`; `tkinter` ships with Python)
- .NET runtime 8/9/10 (the tool sets `DOTNET_ROLL_FORWARD=LatestMajor` for you)
- [Reloaded-II](https://github.com/Reloaded-Project/Reloaded-II) + [ff16.utility.modloader](https://github.com/Nenkai/ff16.utility.modloader) — portable install; mods go directly in `<ReloadedII>\Mods\<modid>\` (drag-and-drop onto the window does nothing)
- [FF16Tools](https://github.com/Nenkai/FF16Tools) — pack extract / dialogue conversion
- [vgmstream](https://github.com/vgmstream/vgmstream) (`vgmstream-cli.exe`) — decode `.sab`
- [VGAudio](https://github.com/Thealexbarney/VGAudio) (`VGAudioCli.exe`) — re-encode HCA

GPU note: `faster-whisper` accelerates on NVIDIA (CUDA) only. On AMD/CPU, `large-v3` still works but is slow — accuracy mode is a one-time build, and *Safe* mode needs no speech recognition at all.

## Legal / redistribution

The mod is generated from your own game files and contains modified game audio, so it is **not** redistributed here. The repo ships only code and the wordlist. Generated edit-lists (which quote real dialogue) and mod audio are git-ignored. Use only with a copy of the game you own.

## Credits

Built on the [FFXVI Modding](https://nenkai.github.io/ffxvi-modding/) community project and tooling by **Nenkai**. Audio format support via **vgmstream** and **VGAudio**.
