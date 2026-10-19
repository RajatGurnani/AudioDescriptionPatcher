# Handoff — main session (2026-10-19)

The coordinating session: roadmap iterations 0–7, Android 0.4.1, and the
three side sessions below. Read this first, then each session's own
handoff in this folder.

## State of `main` (last own commit 9703825, pushed)

- **PC: all roadmap iterations 0–7 done** (docs/plan/README.md),
  verified on Windows only:
  - harness (`tools/harness.py`, 11 cases, mkv + mp4)
  - `tools/batch_test.py`
  - `tools/options_test.py`
  - `tools/desktop_smoke.py` (headless)
  - the built exe checked over HTTP with a hidden window
- **Desktop exe:** builds locally to `dist/ADPatcher` (gitignored; never
  upload). Rebuild with `.venv\Scripts\pyinstaller desktop.spec --noconfirm`.
- **Android 0.4.1** (`com.fuzzypyjamas.adpatcher`) is installed on the
  CMF only.
  - The old `com.renderedideas.adpatcher` is still installed; the owner
    removes it by hand.
  - The notification-permission prompt was left for the owner to decide.
  - Iteration 7 output options and batch mode are PC-only; Android comes
    later, per the owner.

## Uncommitted work in this checkout — NOT mine

At shutdown, `git status` on main showed modified `CLAUDE.md`,
`android/app/build.gradle.kts`, `Aligner.kt`, `AudioEngine.kt` and
possibly more. These belong to the sync-fix and voice-boost sessions,
which share this folder; their handoffs say what to keep. Don't
commit, revert or stash them blindly.

## Side sessions (started from here)

| Session | Where | Status at shutdown | Handoff |
|---|---|---|---|
| Fix Android 45 ms step at piecewise splices | this checkout (main) | working; uses the CMF | docs/handoff/android-splice-sync.md |
| Android: generate AD for any video (AI) | branch `claude/ai-ad-generation-c6685c` (own worktree) | A1 done: llama.cpp runs SmolVLM2-500M + Qwen3-VL-2B on the CMF (commit 96af797); not merged | docs/handoff/ai-audio-description.md (on its branch) |
| Android: voice boost (dialogue clarity) | this checkout | built; PC measurement passed (+2.8/+3.6 dB speech band); CMF run and commit pending | docs/handoff/voice-boost.md |

Each was asked at shutdown to write its handoff and commit/push it. If a
file is missing, read that session's transcript in the app.

## Open items / decisions for the owner

1. **Android splice sync:** a ~45 ms step at piecewise splices
   (docs/plan/09). The sync session owns this.
2. **AI audio description:**
   - Each line sees only 1–3 frames plus the last few lines, so the
     narration doesn't know the plot.
   - Owner questions forwarded: resume-from-any-point, and getting audio
     from "movie reading" apps (MovieReading etc.); answers belong in
     docs/plan/10.
   - Next: the owner's go-ahead for A2+.
3. **Voice boost:** owner to pick the modes (doc 11); then the CMF run
   and commit.
4. **Hand-off buttons:** MKVToolNix/HandBrake were only tested with a
   stand-in tool (neither is installed on the dev PC).
5. **Clean PC:** the exe hasn't been tried on a second machine.

## Owner rules (also in memory)

- Deploy only to the CMF Phone 1 (serial 00116646S015275, model A015).
- Test GUIs headless; never pop windows on the PC.
- No artifacts, releases or uploads; only commits and pushes.
- Open-source and zero-cost; local models only.
- Gradle 8.9 lives in the session scratchpad (gone after a restart):
  download it again and set JAVA_HOME to the Android Studio jbr.
- Git Bash: set `MSYS_NO_PATHCONV=1` for adb paths.

## Handy

- adb UI driving: a small uiautomator dump/tap script (scratchpad
  `ui.py`, also gone after a restart; pattern: `uiautomator dump`, then
  parse bounds, then `input tap`).
- Phone test pair: `/sdcard/Download/ADPatcherTest/` (test_video.mp4 +
  test_AD_cut_pal.mp3).
- Measure sync of any output on the PC: `adpatch.onset_envelope` on the
  output and on the video, then `segment_match` in 5 s windows.
