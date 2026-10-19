# 08 — Output options + hand-offs to existing tools (Iteration 7, v1.0.0)

Principle: implement the options that are *one ffmpeg flag* in our
existing mux; hand off anything that needs a full editor or re-encode
pipeline to the tools that already do it well.

## 8.1 Built in (cheap, inside `build_mux_cmd`)

Stream listing: extend the `ffmpeg -i` stderr parse to a list of
`{index, type, codec, lang, title, default, channels, bitrate}` (regex
on `Stream #0:N(lang): Audio: ...` lines + following `title :` metadata
lines). Expose as `GET /api/probe` (desktop: local path; browser: after
upload).

Options (all optional, defaults = today's behaviour):

| Option | ffmpeg | Notes |
|---|---|---|
| Keep/drop each original audio track | `-map 0:a:N` per kept track | today: only `0:a:0` kept |
| Keep/drop each subtitle track | `-map 0:s:N` | today: all or none |
| Add external subtitle (.srt/.ass/.vtt) | `-i sub.srt -map 2:0 -c:s srt` (mkv) / `mov_text` (mp4) | language + title fields; "default"/"forced" flags |
| AD codec | `aac` / `libopus` / `ac3` / `eac3` | `ac3` for TVs that can't play Opus; Opus best quality/size |
| AD bitrate | `-b:a:0 96k…640k` | default 192k |
| AD channels | `-ac 2` or keep source | AudioVault files are stereo |
| AD track language/title | `-metadata:s:a:0` | default `eng` / "Audio Description" |
| Which track is default | `-disposition` | AD default (today) or original default |
| Container | mkv / mp4 | mp4 restricts subs (mov_text) + codecs |
| Loudness match AD to original | `loudnorm` 2-pass or simple `volume` from measured RMS | AudioVault files are often quieter |

Validation table per container (mp4 can't take ASS/PGS; mp4 + Opus is
legal but poorly supported) → UI greys out illegal combos instead of
failing at mux time. Keeps the existing "retry without subtitles" as a
last resort.

## 8.2 Size / resolution / video bitrate — optional, minimal

Video re-encode is HandBrake's job. One built-in preset only:
**"Fit to N GB"**:
- `video_bitrate = (N·8e9 − audio_bits − 1 % mux overhead) / duration`.
- Encoder auto-pick: `hevc_nvenc` (NVIDIA) → `hevc_qsv` (Intel) →
  `hevc_amf` (AMD) → `libx265` (CPU). Probe by running a 1-frame test
  encode; all four are in the bundled ffmpeg (verified 2026-10-19).
- NVENC: `-rc vbr -b:v X -maxrate 1.5X -bufsize 2X -multipass fullres`
  (single run, two-pass-like quality).
- Optional max height (2160/1080/720): `scale_cuda=-2:H` on NVENC
  (keeps frames on GPU), `scale=-2:H` otherwise.
- HDR sources: warn and skip (tone mapping is a rabbit hole — hand off).
- Clearly labelled "slow — re-encodes the video" with an ETA from the
  first 30 s.

Everything beyond that → hand-off.

## 8.3 Hand-offs

| Tool | Licence | How we hand off | Why not rebuild |
|---|---|---|---|
| **MKVToolNix** (mkvmerge/GUI) | GPL-2.0 | "Open in MKVToolNix" button: launch `mkvtoolnix-gui.exe <output>` if found in Program Files / PATH | full track editor, chapters, attachments, header editing |
| **HandBrake** | GPL-2.0 | "Open in HandBrake" (GUI with file) or run `HandBrakeCLI` with a preset JSON for "Fit to N GB" if user prefers | presets, HDR, filters, NVENC/QSV tuning |
| **qBittorrent** | GPL-2.0 (we never link it) | Optional "magnet → qBittorrent" via its Web API using `qbittorrent-api` (MIT) client; user enables the Web UI and supplies host/port; we only add the magnet and watch for completion to auto-fill the video slot | a torrent engine + UI is a project of its own; search over indexers is a legal risk — **we don't build search** |
| **audiovault-downloader** | no licence file (all rights reserved by default) | not used; our own WebView approach ([07 §7.3](07-desktop-app.md)) | — |

All hand-offs are subprocess launches or HTTP calls to a separately
installed program: no GPL code in our tree. Detect absence → hide the
button, show "install X to enable".

## 8.4 Presets

Save option sets as named presets in
`%LOCALAPPDATA%\ADPatcher\presets.json` (e.g. "TV-friendly: mp4, AC3
640k, AD default", "Archive: mkv, Opus 160k, keep all tracks"). Batch
mode (06) applies one preset to the whole queue.

## Verification

- Every option combo in the container validation table: harness file
  muxes, `ffmpeg -i output` lists the expected streams/dispositions.
- External SRT round-trip: subtitle appears in VLC/MPC with correct
  timing (subs are NOT shifted — they belong to the video timeline).
- "Fit to 1 GB" on a ~2 GB real clip (10 min): output within ±3 %
  of target; NVENC path used on the dev PC.
- Hand-off buttons hidden when tools missing; launch correctly when
  present.
