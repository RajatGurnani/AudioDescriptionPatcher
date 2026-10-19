# 06 — Batch mode + content-based pairing (Iteration 5, v0.8.0)

> **Outcome (2026-10-19):** done, with one deliberate simplification:
> pairing scores **full** envelopes (cached) instead of 3x3-min probes.
> Matched pairs need the full envelopes for alignment anyway, so the only
> extra decode is for files that end up unpaired - and it avoids probe
> windows missing each other under PAL drift (a 4 % speed difference
> shifts the 85 % probe by ~4 min on a 2 h film). Duration gate widened
> to 15 % (a 30 s cut on a short episode exceeds 10 %). Score = stage-1
> speed-scan peak; greedy assignment with 1.5x uniqueness, names as
> tie-break. tools/batch_test.py: 7 movies incl. two episodes sharing a
> 60 s intro, a cut, PAL/NTSC, decoy AD, video without AD -> all correct;
> queue stop/resume (also across a server restart in the web UI) and
> skip-existing verified.

## Goal

Point at a folder (or several) of movies and AD files with arbitrary
names → auto-pair by **audio content**, show the pairing for
confirmation, process as a resumable queue.

## 6.1 Discovery

- Inputs: one or more folders (recursive optional) or a file list.
- Classify each file with the existing `classify_inputs` logic, extended
  to single files: video ext / `probe_has_video` → video; else audio.
- Skip already-patched outputs (`*.AD.mkv`, `*.AD.mp4`) and files < 1 min.
- Probe durations (`ffmpeg -i` stderr parse, one process per file,
  parallel ×4).

## 6.2 Pairing — three passes, cheapest first

**Pass 1 — duration gate (free).**
Candidate pair (v, d) only if
`duration(v) / duration(d)` ∈ [0.90, 1.10] (covers PAL ±4.3 % plus
trimmed intros, minus cut differences). Typically eliminates most pairs.

**Pass 2 — fingerprint score (cheap).**
For each file, compute a *probe envelope*: three 3-min windows at 15 %,
50 %, 85 % of its duration, decoded with input seeking
(`-ss ... -t 180`) → ~9 min of audio per file instead of the full
runtime. Store in the envelope cache (02 §2.3) under a "probe" key.
For each surviving pair: run the stage-1 speed scan on the probes
(coarse grid only: special ratios + 0.95–1.05 at 0.5 % step) allowing
the probe windows to be matched to any position (FFT over the full
probe concatenation). Score = best normalized peak.

Cost: ~9 min decode per file ≈ 5–10 % of a full run; 20 movies × 20 AD
files = 40 probes, not 400.

**Pass 3 — assignment.**
- Build score matrix S[v][d]; disallowed pairs = 0.
- One-to-one assignment maximizing total score. For N ≤ 50 a greedy
  "take highest remaining score" is near-optimal and dependency-free;
  if scipy is already a dependency, use
  `scipy.optimize.linear_sum_assignment` (BSD-3).
- Accept a pair only if score ≥ 0.15 **and** it beats the row's and
  column's runner-up by ≥ 1.5× (uniqueness). Else → "unmatched / ambiguous".
- Name similarity as a tie-breaker only: normalized titles (strip
  year, resolution, codec, "AD", "audio description", punctuation) →
  `difflib.SequenceMatcher` ratio (stdlib). RapidFuzz (MIT) is faster but
  unnecessary at these sizes.

Edge cases:
- Multiple AD versions for one movie → pick best, list the rest as
  alternatives in the confirmation table.
- Series episodes (similar durations, shared intro music!) → the shared
  intro makes probe windows at 15 % risky; for files < 70 min, shift the
  first probe to 30 %. Uniqueness check catches the rest.

## 6.3 Confirmation table (UI)

| Movie | AD file | score | status | |
|---|---|---|---|---|
| Avatar (2009).mkv | avatar_ad.mp3 | 0.41 | ✓ matched | change ▾ |
| Dune.mkv | — | — | no match | pick ▾ |

User can override any row (dropdown of AD files), untick rows, set
output folder + options once for the batch.

## 6.4 Queue

- Sequential jobs (alignment is light, mux is disk-bound; parallel muxes
  on one disk just thrash). Option: 2 concurrent if source and output
  disks differ.
- Persist queue state to `%LOCALAPPDATA%\ADPatcher\queue.json` after
  each job → resume after crash/close (same idea as Android
  `JobStore` resume in v0.3.0).
- Per-job result (confidence, warnings, output path) in a summary table;
  jobs with band "likely wrong" are marked for review, not deleted.
- The full alignment pass needs full-length envelopes (probes are only
  for pairing); both are cached, so re-runs skip decoding entirely.

## 6.5 Interfaces

- CLI: `adpatch.py batch <folder> [<folder2>] [--yes] [-o outdir]`
  prints the pairing table; without `--yes` asks to confirm.
- Web/desktop: "Batch" tab. In web (browser) mode, folder access needs
  the server to see the files: works when the browser runs on the same
  PC (enter/paste a folder path, server-side listing). Phones can't
  batch-upload sensibly — batch is a desktop/PC feature.

## Verification

- Harness: generate 5 synthetic "movies" (different seeds) + their AD
  variants with shuffled random names → 5/5 correct pairs, 1 decoy AD
  left unmatched.
- Two "episodes" sharing a 60 s identical intro → still paired
  correctly.
- Kill the app mid-batch → restart resumes at the next unfinished job.
