# 14 — Batch for whole TV shows (seasons × episodes)

> **Status: CLOSED (2026-10-28).** Step 4 (auto-split double-episode
> AD) dropped: per the owner, no AD file covers two episodes.
> Later additions: progress table (episode, status, %, sync, time,
> size; 3264c42) and its own full-width "Folder / TV show" tab with
> Videos / AD-files folder zones (170b03e).
>
> Done 2026-10-26: Steps 1-3
> (`parse_episode`, two-pass candidates, `content_score` coverage),
> 5 (season headers + select-all, SxxEyy, notes), 6 (retry failed on
> resume, `-o` mirrors season folders; envelopes were already float32).
> Double-episode AD is detected and noted, not patched. Absolute
> (anime) numbering skipped (YAGNI). Commits f8742fc, f4508b3, 225cb94.
>
> Planned 2026-10-26. Builds on [06](06-batch-and-pairing.md).
> Goal: point the app at a show folder (several seasons, 100–300
> episodes) plus a folder of AD files, and have every episode paired
> correctly, patched, and resumable, with no manual pairing except for
> real ambiguities.

## What breaks today (batch.py, built + tested for movies)

| Problem | Where | Effect on a show |
|---|---|---|
| Duration gate only (15 %) | `pair_files` `batch.py:133` | episodes have near-equal lengths → almost every pair is a candidate → N×M speed scans (220 eps → ~48 k scans, hours) |
| Score = single speed-scan peak | `match_score` `batch.py:115` | shared theme song, cold-open recaps ("previously on" = scenes from *other* episodes), identical credits → wrong episode can peak high, or near-ties → "check" |
| Names compared with difflib | `name_similarity` | "Show S01E02" vs "Show S01E03" ≈ 0.95 → names can't break ties; episode numbers never parsed |
| No episode model | — | double episodes (S01E01-E02, one AD for two files or vice versa), specials (S00/"Special"), missing/extra episodes, absolute numbering (anime "Ep 137") all unhandled |
| Flat queue | `Queue` | no per-show/season grouping; one bad file mid-run is fine (resumable) but review of 200 rows is a wall |
| Analysis threads = 3, all envelopes in RAM | `scan` | 440 envelopes ≈ 1 GB float64; fine on 16 GB, but should be float32 + loaded on demand |

## Design

### 1. Episode identity from names (cheap, first)
- `parse_episode(path)` → `{show, season, episode(s), absolute, special, year}`.
  Patterns: `S01E02`, `s1e2`, `1x02`, `S01E01-E02`/`S01E01E02`,
  `Season 1/…Episode 2`, `Ep 02`, `E02` with the season from the parent
  folder ("Season 1", "S01"), and absolute `- 137 -` (anime).
  Season/show taken from the folder path when the file name lacks it.
- Show name = normalized title with the episode token and everything
  after it stripped, which groups files by show.
- Unit-tested table of ~40 real-world name styles (AudioVault names
  like "Show Name - S01E02 - Title (AD).mp3", scene names, Plex
  names).

### 2. Candidate generation (replace all-pairs)
- Both sides parsed and same (show, season, episode) → **one**
  candidate. Verify it with content (step 3); if it passes → matched
  without any other comparisons.
- One side parsed and the other not, or the verification fails → candidates
  = same season ± 2 episodes (renumbering, missing pilot, double
  episode), then the whole season, then the whole show (only if still
  unresolved).
- Nothing parsed (movie-style folders) → today's duration-gated
  all-pairs, unchanged.
- Cost for a cleanly named show: N verifications instead of N×M
  scans (220 eps: ~220 instead of ~48 k).

### 3. Content score that ignores shared intros/recaps
- Replace the single peak with **coverage**: after the speed scan
  picks (a, b), run the 30 s segment check that `fit_alignment`
  already does. Score = the fraction of the AD's runtime whose segments
  agree with (a, b) within 0.2 s. A different episode that shares a
  60 s intro scores ~0.03; the right one scores > 0.6.
- Exclude the first and last 10 % from the *peak* search (cold open /
  recap / credits) but not from coverage.
- Keep the old peak as a tie-breaker; the decision uses coverage:
  matched if coverage ≥ 0.5 and ≥ 2× the best rival's, otherwise "check".
- Piecewise cuts (TV rip with ad breaks): coverage is computed from the
  piecewise fit when the linear coverage is < 0.9, so ad-break AD
  doesn't look like a mismatch.

### 4. Double episodes / splits
- AD duration ≈ the sum of two consecutive episodes (± 15 %) and the names
  say E01-E02 → candidate "AD spans two videos". Piecewise alignment
  already supports pieces: align the AD against each video separately
  (each video covers part of the AD, found by coverage), and patch
  each video with its slice.
- Reverse case (one video, two AD files) → concat the AD files in episode
  order, then patch normally.
- First version: detect these and mark them "check" with a clear reason;
  the auto-handling ships as a follow-up within this iteration if
  time allows.

### 5. Review UI for big batches
- Group the rows by show → season, collapsed per season with counts
  (✓ matched / ? check / ✗ missing).
- Show the parsed SxxEyy next to each file, and the coverage % as the score.
- Bulk actions: "accept season", "skip season".
- Missing list: which episodes have no AD and which AD files are unused,
  per season.
- Output naming keeps the video's name plus `.AD.mkv`, in place (see
  [13](13-standalone-pc-app.md)) or mirrored under the chosen output
  folder (`Show/Season 01/…`).

### 6. Robustness
- Queue: per-item status already resumable. Add: skip-and-continue on
  error (already), a retry-failed button, a log per item, and
  "pause after current" so the queue can be stopped cleanly.
- Envelopes stored as float32, loaded lazily from the cache during
  pairing (no 1 GB dict). Analysis workers = min(4, cores/2) on the
  Ryzen 250.
- Rescan is idempotent: files already patched (`*.AD.*` next to them)
  are listed as done, not queued again.
- Long paths (> 260 chars) and Unicode names: use `\\?\` paths for
  ffmpeg on Windows, and test with a Japanese/accented name.

## Tests (`tools/batch_test.py` additions)

- **Synthetic show**: 3 seasons × 8 episodes (~3 min each to keep it
  fast), sharing an identical 20 s theme intro and 15 s credits.
  Episodes 2+ open with a 30 s "previously on" built from the previous
  episode's audio.
  - Case A: clean SxxEyy names on both sides → all matched, scan count ≈ N.
  - Case B: AD files renamed to random names → all matched by content.
  - Case C: one missing AD, one extra AD, one double episode,
    S00 special → correct "missing / unused / check" rows.
  - Case D: one AD with an ad-break gap (piecewise) → matched.
- **Name parser** table test (~40 styles).
- Exit: every case correct; scan count and time printed; no false
  "matched" anywhere (a wrong match is worse than a "check").

## Order

1 → 2 → 3 (pairing core, with tests A/B) → 5 (UI) → 6 → 4 (double
episodes last; rare). Do it after or alongside 13 steps 3–4. It touches
`batch.py` + the batch card, while 13 touches the shell and bridge, so
they can run in parallel if the UI work is sequenced.
