# Handoff: Android piecewise splice sync (~45 ms step)

## Status
Fixed. On the CMF Phone 1 with the debug build, the harness 10-min media
measured against the video's own audio (5 s windows):
- cut+PAL: piece 1 ≈ 0 ms, piece 2 ≈ −5 ms (was −8 / −53)
- cut only: ≤ 0.6 ms
- PAL only: ≤ 2.7 ms

Kotlin parity test passes and the release build compiles. **Not yet done:**
the clean (debug code removed) build hasn't been installed and re-run on the
phone.

## Root causes (verified at the sample level, phone PCM vs ffmpeg)
1. **MP3 pts are wrong on Android.** The first decoded buffer is short (278
   of 576 samples) but stamped as a full frame, so pts run 75 ms ahead of the
   content. After a seek they're off by 57–395 ms. AAC-in-MP4 pts are exact.
   Sample counts always match ffmpeg exactly.
2. `EnvelopeAccumulator` followed pts. It filled the MP3 pts gap with
   phantom silence (+19.6 ms), and when a seeked range started after its slot
   it set `nextPos = max(...)`, which dropped the gap and slid the envelope
   early (video −20 / −10 ms).
3. **Ranged decodes stopped on pts** (`rangeUs.last`). Because MP3 pts run
   ahead, range 1 ended 4 frames short, and the concatenation slid every
   later range 40 ms early.
4. **Render: AAC encoder priming.** `c2.android.aac.encoder` adds 2048
   frames (46 ms at 44.1 kHz). MediaMuxer writes no edit list, so the whole
   AD track played 46 ms late.

## Fix (my files: AudioEngine.kt, Aligner.kt, my hunks of Patcher.kt)
- **AudioEngine:**
  - Samples are placed by count from an anchor: 0 for a decode from the
    start, the first pts after a seek. A range from 0 never seeks (even
    `seekTo(0)` drops an AAC frame).
  - Ranges after the first decode from 2 s early. A 0.5 s PCM head probe is
    located in the previous range's tail by normalized xcorr (score 1.000 in
    practice), and the range's energies are shifted by the difference
    (`shift`, linear interpolation).
  - Ranges stop by sample count only, and each slot is filled exactly.
- **Aligner:** `nextPow2` and `crossCorrelate` are now public, for reuse.
- **Patcher:**
  - `encoderDelay()` measures the delay once per rate by a loopback (encode
    a noise burst, decode it, find it), falling back to 2048.
  - The render starts `delay` frames in, and `EncoderSink` subtracts the
    delay from the pts.

## Working-tree ownership
The voice-boost session edited the same checkout: VoiceBoost.kt,
MainActivity, the layout, its hunks in Patcher.kt (internal modifiers, `fx`,
`boost`), JobRunner, build.gradle, CLAUDE.md and its docs. I committed only
my files. My Patcher.kt hunks were staged from HEAD plus my changes, so their
hunks are still unstaged.

## Repro / test
- Media: `%TEMP%\adpatch-phone\test_video.mp4` (AAC) and the harness ADs
  `%TEMP%\adpatch-harness\ad4_{cut,pal,cut_pal}_10m.mp3`, pushed to
  `/sdcard/Download/ADPatcherTest`.
- Drive the phone with the scratchpad scripts `ui.py` / `run.sh` (session
  cde144db scratchpad): uiautomator dump + `adb input tap`, pinned to serial
  00116646S015275; check the model is A015 before installing.
- Measure: `lag.py <out.mp4> <video>` = `adpatch.onset_envelope` +
  `segment_match` in 5 s windows.

## Next steps
1. Install the clean build (check the model first) and re-run cut+PAL. The
   logcat should show `seam 1: +395.06 ms` for the AD and
   `AAC encoder delay: 2048 frames`.
2. Follow-up: serial MediaCodec decode is slow (~75 s for 9 min of 16 kHz
   MP3), probably from the per-frame 10 ms output wait in `decodeAudio`.

Device state: CMF idle, no job running. Leftover debug files (*.f32, *.s16,
dump_env) remain in the app's external files dir; harmless, since the dump
code is removed.
