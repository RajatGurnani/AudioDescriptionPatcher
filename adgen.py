"""AI audio description for videos that have none (docs/plan/10, P1).

PC fast path: finds dialogue-free gaps (pyannote segmentation, ONNX),
shot cuts (tiny ffmpeg frames), plans cues, asks a local vision-language
model (Qwen3-VL via llama.cpp's llama-server, Vulkan build) for one line
per cue, speaks the lines (Windows SAPI for now; Kokoro is A6), ducks the
original mix under the narration and muxes the result as the default
audio track (original kept as the 2nd track). Everything runs locally.

  python adgen.py --setup [--model 4b]      # one-time downloads
  python adgen.py VIDEO [-o OUT] [--model auto|2b|4b|8b|cpu]

Every stage checkpoints into a job dir keyed by the file's content, so a
killed run resumes where it stopped (plan 10, section 8a). Rules for gaps,
shots and cues mirror the Android code (GapFinder.kt, ShotDetector.kt,
CuePlanner.kt) and the dev tools (tools/gaps_ref.py, tools/shots_ref.py).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
import zipfile

import numpy as np

import adpatch as ap

FFMPEG = ap.FFMPEG
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("ADPATCH_AI_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ADPatcher", "ai")

# ---------------------------------------------------------------- downloads
LLAMA_TAG = "b11785"
LLAMA_ZIP = ("https://github.com/ggml-org/llama.cpp/releases/download/"
             f"{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-vulkan-x64.zip",
             "77445cf9d79347a23cf78775d513913798bd173a1168de192a41454dc8e10e67")
HF = "https://huggingface.co/Qwen/Qwen3-VL-{s}-Instruct-GGUF/resolve/main/"
MODELS = {  # name: (repo size tag, [(file, sha256)]: model, mmproj), VRAM GB
    "2b": ("2B", [("Qwen3VL-2B-Instruct-Q4_K_M.gguf", "089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae"),
                  ("mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf", "f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82")], 2.5),
    "4b": ("4B", [("Qwen3VL-4B-Instruct-Q4_K_M.gguf", "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"),
                  ("mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d")], 4.0),
    "8b": ("8B", [("Qwen3VL-8B-Instruct-Q4_K_M.gguf", "67d1659bfe71b89d50b45a4ad1a9e5b997e5bb16ce5da66a6a6167abd569e9e2"),
                  ("mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf", "c6ba85508d82f42590e6eb77d5340369ab6fecf107a7561d809523d8aa5f3bfd")], 7.0),
}
# pyannote segmentation-3.0 (MIT), int8 ONNX export by sherpa-onnx; the
# Android app bundles the same file
PYANNOTE = os.path.join(HERE, "android", "app", "src", "main", "assets",
                        "pyannote-segmentation-3.0.int8.onnx")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _download(url, dest, sha, log=print):
    if os.path.exists(dest) and _sha256(dest) == sha:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    log(f"downloading {os.path.basename(dest)} ...")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            done += len(b)
            if total and done - last > total / 10:
                last = done
                log(f"  {done * 100 // total}%")
    if _sha256(tmp) != sha:
        os.remove(tmp)
        raise RuntimeError(f"checksum mismatch for {url}")
    os.replace(tmp, dest)
    return dest


def llama_server_exe(log=print):
    exe = os.path.join(HOME, "llama", LLAMA_TAG, "llama-server.exe")
    if not os.path.exists(exe):
        z = _download(LLAMA_ZIP[0], os.path.join(HOME, "llama", f"{LLAMA_TAG}.zip"), LLAMA_ZIP[1], log)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(os.path.dirname(exe))
        os.remove(z)
    return exe


def model_files(name, log=print):
    tag, files, _ = MODELS[name]
    return [_download(HF.format(s=tag) + f, os.path.join(HOME, "models", f), sha, log)
            for f, sha in files]


def free_vram_gb():
    """Free memory of the first NVIDIA GPU (None if unknown)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0]) / 1024
    except Exception:
        return None


def pick_model(choice="auto"):
    """(model name, n_gpu_layers): biggest model that fits free VRAM."""
    if choice == "cpu":
        return "2b", 0
    if choice in MODELS:
        return choice, 99
    free = free_vram_gb()
    for name in ("8b", "4b", "2b"):
        if free is not None and free >= MODELS[name][2]:
            return name, 99
    return "2b", 0   # CPU fallback


# ---------------------------------------------------------------- gaps (A2)
SEG_SR, SEG_WIN, SEG_STEP, SEG_HOP = 16000, 160000, 80000, 270
MIN_GAP_S, GAP_GUARD_S = 1.5, 0.2
THRESHOLD, NEG_THRESHOLD = 0.4, 0.28
MIN_SPEECH_S, MIN_SILENCE_S = 0.25, 0.10


def probs_to_speech(probs, hop_s):
    speech, start, below = [], None, None
    for i, p in enumerate(probs):
        t = i * hop_s
        if start is None:
            if p >= THRESHOLD:
                start, below = t, None
        elif p < NEG_THRESHOLD:
            below = t if below is None else below
            if t + hop_s - below >= MIN_SILENCE_S:
                if below - start >= MIN_SPEECH_S:
                    speech.append([start, below])
                start, below = None, None
        else:
            below = None
    if start is not None and len(probs) * hop_s - start >= MIN_SPEECH_S:
        speech.append([start, len(probs) * hop_s])
    return [[round(s, 3), round(e, 3)] for s, e in speech]


def gaps_from_speech(speech, duration):
    gaps, t = [], 0.0
    for s, e in speech + [[duration + GAP_GUARD_S, duration]]:
        g0 = t + GAP_GUARD_S if t > 0 else 0.0
        g1 = min(s - GAP_GUARD_S, duration)
        if g1 - g0 >= MIN_GAP_S:
            gaps.append([round(g0, 3), round(g1, 3)])
        t = max(t, e)
    return gaps


def find_gaps(media, progress=None):
    """Streams 16 kHz mono through pyannote in 10 s windows (50 % overlap)."""
    import onnxruntime as ort
    sess = ort.InferenceSession(PYANNOTE, providers=["CPUExecutionProvider"])
    dur = ap.probe_duration(media) or 0
    cmd = [FFMPEG, "-v", "error", "-i", media, "-vn", "-ac", "1", "-ar", str(SEG_SR), "-f", "f32le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    nfr = int(dur * SEG_SR) // SEG_HOP + 1 if dur else 1 << 16
    acc, cnt = np.zeros(nfr), np.zeros(nfr)
    buf = np.zeros(0, np.float32)
    base, total, eof = 0, 0, False   # base = absolute sample index of buf[0]
    while True:
        while not eof and len(buf) < SEG_WIN:
            b = p.stdout.read(1 << 18)
            if not b:
                eof = True
                break
            x = np.frombuffer(b[:len(b) // 4 * 4], np.float32)
            buf = np.concatenate([buf, x])
            total += len(x)
        # window starts 0, STEP, ... up to total - WIN (first one always),
        # like GapFinder.WindowPlan / gaps_ref.pyannote_probs
        if len(buf) == 0 or (base > 0 and len(buf) < SEG_WIN):
            break
        c = buf[:SEG_WIN]
        if len(c) < SEG_WIN:
            c = np.pad(c, (0, SEG_WIN - len(c)))
        y = sess.run(None, {"x": c[None, None, :].astype(np.float32)})[0][0]
        pr = 1.0 - np.exp(y[:, 0])
        f0 = base // SEG_HOP
        if f0 + len(pr) > len(acc):
            grow = f0 + len(pr) - len(acc) + (1 << 14)
            acc, cnt = np.pad(acc, (0, grow)), np.pad(cnt, (0, grow))
        acc[f0:f0 + len(pr)] += pr
        cnt[f0:f0 + len(pr)] += 1
        buf = buf[SEG_STEP:]
        base += SEG_STEP
        if progress and dur:
            progress(min(base / SEG_SR / dur, 1.0))
    p.wait()
    nfr = total // SEG_HOP + 1
    probs = acc[:nfr] / np.maximum(cnt[:nfr], 1)
    duration = total / SEG_SR
    speech = probs_to_speech(list(probs), SEG_HOP / SEG_SR)
    return {"speech": speech, "gaps": gaps_from_speech(speech, duration), "duration": round(duration, 3)}


# --------------------------------------------------------------- shots (A3)
GW, GH = 64, 36
ADAPT_WIN, ADAPT_RATIO, MIN_SCORE, MIN_SHOT_S = 2, 3.0, 12.0, 0.6


def cuts_from_scores(ts, s):
    cuts, last = [], -1e9
    n = len(s)
    for i in range(1, n):
        nb = [s[j] for j in range(i - ADAPT_WIN, i + ADAPT_WIN + 1) if j != i and 1 <= j < n]
        ref = max(sum(nb) / max(len(nb), 1), 1e-3)
        if s[i] >= MIN_SCORE and s[i] / ref >= ADAPT_RATIO and ts[i] - last >= MIN_SHOT_S:
            cuts.append(round(float(ts[i]), 3))
            last = ts[i]
    return cuts


def find_shots(media, progress=None):
    """Per-frame content score on 64x36 YUV (ffmpeg area downscale)."""
    dur = ap.probe_duration(media) or 0
    cmd = [FFMPEG, "-v", "info", "-i", media, "-an", "-sn", "-vf",
           f"scale={GW}:{GH}:flags=area,format=yuv420p,showinfo",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts = []

    def drain():
        for line in p.stderr:
            m = re.search(rb"pts_time:([0-9.]+)", line)
            if m:
                pts.append(float(m.group(1)))
    th = threading.Thread(target=drain, daemon=True)
    th.start()
    fs = GW * GH * 3 // 2
    scores, prev, n = [], None, 0
    while True:
        b = p.stdout.read(fs)
        if len(b) < fs:
            break
        f = np.frombuffer(b, np.uint8).astype(np.float32)
        if prev is None:
            scores.append(0.0)
        else:
            d = np.abs(f - prev)
            scores.append(float(d[:GW * GH].mean() + d[GW * GH:GW * GH * 5 // 4].mean()
                                + d[GW * GH * 5 // 4:].mean()))
        prev = f
        n += 1
        if progress and dur and n % 500 == 0 and pts:
            progress(min(pts[-1] / dur, 1.0))
    p.wait()
    th.join()
    ts = pts[:n]
    scores = scores[:len(ts)]
    cuts = cuts_from_scores(ts, scores)
    duration = max(dur, ts[-1] if ts else 0.0)
    return {"cuts": cuts, "duration": round(duration, 3), "frames": len(ts)}


def shots_from_cuts(cuts, duration):
    b = [0.0] + list(cuts) + [duration]
    return [[b[i], b[i + 1]] for i in range(len(b) - 1)]


# ---------------------------------------------------------------- cues (A3)
LOOKBACK_S, MAX_FRAMES, MAX_CUE_S, MIN_STALE_S = 4.0, 3, 10.0, 4.0


def plan_cues(gaps, shots):
    """Same rules as CuePlanner.kt."""
    cues, last_shown = [], -1
    for g0, g1 in gaps:
        pieces = max(1, int(np.ceil((g1 - g0) / MAX_CUE_S)))
        ln = (g1 - g0) / pieces
        for k in range(pieces):
            c0 = g0 + k * ln
            c1 = c0 + ln
            frm = c0 - LOOKBACK_S if k == 0 else c0
            ids = [i for i, (a, b) in enumerate(shots) if b > frm and a < c1][-MAX_FRAMES:]
            if not ids:
                continue
            times = [(max(shots[i][0], frm) + min(shots[i][1], c1)) / 2 for i in ids]
            fresh = ids[-1] > last_shown
            if not fresh and c1 - c0 < MIN_STALE_S:
                continue
            cues.append({"start": round(c0, 3), "end": round(c1, 3),
                         "frames": [round(t, 3) for t in times], "shots": ids, "fresh": fresh})
            last_shown = max(last_shown, ids[-1])
    return cues


def grab_frame(media, t, width=512):
    cmd = [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", media, "-frames:v", "1",
           "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    return subprocess.run(cmd, capture_output=True).stdout


# ----------------------------------------------------------- describe (A4)
WORDS_PER_S = 2.6
PROMPT_VERSION = 2
VLM_FRAMES = 2          # newest frames sent per cue (speed)
MIN_SPEAK_S = 1.8       # shorter cues cannot fit a spoken line
SYSTEM = ("You write audio description for blind and low-vision viewers of a film. "
          "Describe only what is visible: who or what is there, what they do, where. "
          "Present tense, third person, plain everyday words. Never guess feelings, "
          "thoughts or intentions (no 'startled', 'surprised', 'contemplating'); "
          "describe actions and expressions literally. No camera or film terms, never begin with 'In this "
          "scene', 'The image shows' or 'We see'. Do not repeat what earlier lines "
          "already said. If nothing new is visible, answer exactly SKIP.")


class VlmServer:
    """A llama-server process (separate exe, MIT) with one model loaded."""

    def __init__(self, name, ngl, log=print, port=8093):
        self.port, self.log = port, log
        exe = llama_server_exe(log)
        model, mmproj = model_files(name, log)
        cmd = [exe, "-m", model, "--mmproj", mmproj, "-ngl", str(ngl), "-c", "2048",
               "--host", "127.0.0.1", "--port", str(port), "--image-max-tokens", "256"]
        if ngl == 0:
            cmd += ["--no-mmproj-offload", "-t", str(max(2, (os.cpu_count() or 4) // 2))]
        self.logf = open(os.path.join(HOME, "llama-server.log"), "w")
        self.p = subprocess.Popen(cmd, stdout=self.logf, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while True:
            if self.p.poll() is not None:
                raise RuntimeError("llama-server exited; see " + self.logf.name)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        break
            except Exception:
                pass
            if time.time() - t0 > 300:
                raise RuntimeError("llama-server did not become ready")
            time.sleep(0.5)
        self.name = name
        log(f"VLM ready: Qwen3-VL-{name.upper()} ({'GPU' if ngl else 'CPU'}) in {time.time() - t0:.0f} s")

    def chat(self, content, max_tokens=60):
        body = {"messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": content}],
                "max_tokens": max_tokens, "temperature": 0, "cache_prompt": False}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()

    def describe(self, jpegs, words, earlier):
        content = [{"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(j).decode()}} for j in jpegs]
        ctx = " ".join(earlier[-3:]) or "(none)"
        content.append({"type": "text", "text":
                        f"Earlier lines: {ctx}\nThe frames are in time order, the last is the newest. "
                        f"Write ONE sentence of at most {words} words about what is visible now."})
        line = clean_line(self.chat(content))
        if line and len(line.split()) > words:
            line = clean_line(self.chat([{"type": "text", "text":
                                          f"Shorten to at most {words} words, keep the most important "
                                          f"visible action, same style: {line}"}]))
        return line

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.logf.close()


def clean_line(s):
    s = s.strip().strip('"').strip()
    s = re.sub(r"^(in this (scene|frame|image)|the (image|frame|scene) shows|we see)[, ]*", "", s, flags=re.I)
    if not s or s.upper().startswith("SKIP"):
        return ""
    s = s[0].upper() + s[1:]
    if s[-1] not in ".!?":
        s += "."
    return s


# ---------------------------------------------------------------- TTS (A5)
TTS_RATE = 24000
_SAPI_PS = r"""
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 $args[0] | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($it in $items) {
  $s.Rate = [int]$it.rate
  $s.SetOutputToWaveFile($it.path, $fmt)
  $s.Speak([string]$it.text)
  $s.SetOutputToNull()
}
$s.Dispose()
"""


def tts_sapi(items):
    """[{path, text, rate}] -> WAVs (Windows SAPI, built into Windows)."""
    with tempfile.TemporaryDirectory() as td:
        js = os.path.join(td, "items.json")
        ps = os.path.join(td, "tts.ps1")
        json.dump(items, open(js, "w", encoding="utf-8"))
        open(ps, "w", encoding="utf-8").write(_SAPI_PS)
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps, js],
                       check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wav_seconds(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


def speak_lines(lines, tts_dir, log=print):
    """Speak each line so it fits its cue: normal rate, then faster, else drop."""
    os.makedirs(tts_dir, exist_ok=True)
    todo = [ln for ln in lines if ln["text"] and not os.path.exists(os.path.join(tts_dir, f"{ln['id']}.wav"))]
    for rate in (0, 3):
        batch = [{"path": os.path.join(tts_dir, f"{ln['id']}.tmp.wav"), "text": ln["text"], "rate": rate}
                 for ln in todo]
        if not batch:
            break
        tts_sapi(batch)
        retry = []
        for ln, it in zip(todo, batch):
            room = ln["end"] - ln["start"] - 0.1
            if wav_seconds(it["path"]) <= room or rate == 3:
                if wav_seconds(it["path"]) <= room:
                    os.replace(it["path"], os.path.join(tts_dir, f"{ln['id']}.wav"))
                else:
                    os.remove(it["path"])
                    log(f"  dropped (too long even fast): {ln['text']}")
            else:
                os.remove(it["path"])
                retry.append(ln)
        todo = retry


def build_narration(lines, tts_dir, duration, out_wav, gain_db=-1.0):
    """One mono 24 kHz WAV with every clip at its cue start (streamed)."""
    g = 10 ** (gain_db / 20)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(TTS_RATE)
        pos = 0
        for ln in sorted(lines, key=lambda x: x["start"]):
            p = os.path.join(tts_dir, f"{ln['id']}.wav")
            if not ln["text"] or not os.path.exists(p):
                continue
            with wave.open(p) as r:
                x = np.frombuffer(r.readframes(r.getnframes()), np.int16).astype(np.float32)
            peak = max(np.abs(x).max(), 1.0)
            x = np.clip(x * (32767 * g / peak), -32768, 32767).astype(np.int16)
            at = int(ln["start"] * TTS_RATE)
            if at > pos:
                w.writeframes(np.zeros(at - pos, np.int16).tobytes()); pos = at
            w.writeframes(x.tobytes()); pos += len(x)
        end = int(duration * TTS_RATE)
        if end > pos:
            w.writeframes(np.zeros(end - pos, np.int16).tobytes())


def mux(video, narration_wav, output, progress=None, log=print):
    """Original audio ducked under the narration (sidechain) + narration ->
    default track a:0; original copied as a:1; video/subtitles copied."""
    enc, _ = ap.ad_encoder()
    graph = ("[0:a:0]aresample=48000,aformat=channel_layouts=stereo[orig];"
             "[1:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[nar][sc];"
             "[orig][sc]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400:makeup=1[duck];"
             "[duck][nar]amix=inputs=2:normalize=0:duration=first[mix]")
    for subs in (True, False):
        cmd = [FFMPEG, "-y", "-v", "error", "-i", video, "-i", narration_wav,
               "-filter_complex", graph, "-map", "0:v", "-map", "[mix]", "-map", "0:a:0"]
        if subs:
            cmd += ["-map", "0:s?", "-c:s", "copy"]
        cmd += ["-c:v", "copy", "-c:a:0", enc, "-b:a:0", "192k", "-c:a:1", "copy",
                "-disposition:a:0", "default", "-disposition:a:1", "0",
                "-metadata:s:a:0", "title=Audio description (AI)", "-metadata:s:a:0", "language=eng",
                output]
        rc, err = ap.run_ffmpeg(cmd, ap.probe_duration(video), progress)
        if rc == 0:
            return
        log("mux failed" + (" with subtitles, retrying without" if subs else "") + f": {err[-300:]}")
    raise RuntimeError("mux failed")


# ------------------------------------------------------------------- job
def job_dir(media):
    return os.path.join(HOME, "jobs", ap._cache_key(media))


def _load(path):
    return json.load(open(path)) if os.path.exists(path) else None


def _save(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"))
    os.replace(tmp, path)


def generate(video, output=None, model="auto", log=print, progress=None):
    """Full pipeline with per-stage checkpoints. Returns the output path."""
    prog = progress or (lambda phase, f: None)
    output = output or os.path.splitext(video)[0] + ".AIAD.mkv"
    jd = job_dir(video)
    os.makedirs(jd, exist_ok=True)
    timings = {}

    def stage(name, fn, path):
        res = _load(path)
        if res is None:
            t0 = time.time()
            res = fn()
            _save(path, res)
            timings[name] = round(time.time() - t0, 1)
            log(f"{name}: {timings[name]} s")
        else:
            log(f"{name}: from checkpoint")
        return res

    gaps = stage("gaps", lambda: find_gaps(video, lambda f: prog("gaps", f)), os.path.join(jd, "gaps.json"))
    shots = stage("shots", lambda: find_shots(video, lambda f: prog("shots", f)), os.path.join(jd, "shots.json"))
    cues = plan_cues(gaps["gaps"], shots_from_cuts(shots["cuts"], shots["duration"]))
    log(f"{len(gaps['gaps'])} gaps, {len(shots['cuts'])} cuts -> {len(cues)} cues")

    # describe: one JSON line per cue, appended + flushed (resume point)
    lines_path = os.path.join(jd, "lines.jsonl")
    done = {}
    if os.path.exists(lines_path):
        for row in open(lines_path, encoding="utf-8"):
            if row.strip():
                r = json.loads(row)
                if r.get("v") == PROMPT_VERSION:
                    done[r["id"]] = r
    todo = [i for i in range(len(cues)) if i not in done]
    if todo:
        name, ngl = pick_model(model)
        t0 = time.time()
        srv = VlmServer(name, ngl, log)
        # grab frames ahead on a few threads while the VLM works
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(4)
        futs = {i: pool.submit(lambda c: [grab_frame(video, t) for t in c["frames"]], cues[i]) for i in todo}
        vlm_s = 0.0
        try:
            earlier = [done[i]["text"] for i in sorted(done) if done[i]["text"]]
            with open(lines_path, "a", encoding="utf-8") as f:
                for k, i in enumerate(todo):
                    c = cues[i]
                    words = max(3, min(25, int((c["end"] - c["start"]) * WORDS_PER_S)))
                    jpegs = futs.pop(i).result()[-VLM_FRAMES:]
                    tv = time.time()
                    text = srv.describe([j for j in jpegs if j], words, earlier) if (c["end"] - c["start"] >= MIN_SPEAK_S) else ""
                    vlm_s += time.time() - tv
                    row = {"id": i, "v": PROMPT_VERSION, "start": c["start"], "end": c["end"],
                           "text": text, "model": name}
                    f.write(json.dumps(row) + "\n"); f.flush(); os.fsync(f.fileno())
                    done[i] = row
                    if text:
                        earlier.append(text)
                        log(f"  [{ap_time(c['start'])}] {text}")
                    prog("describe", (k + 1) / len(todo))
        finally:
            pool.shutdown(cancel_futures=True)
            srv.close()
        timings["describe"] = round(time.time() - t0, 1)
        timings["vlm"] = round(vlm_s, 1)
        log(f"describe: {timings['describe']} s for {len(todo)} cues ({vlm_s:.0f} s inside the VLM)")
    lines = [done[i] for i in sorted(done)]

    t0 = time.time()
    speak_lines(lines, os.path.join(jd, "tts"), log)
    narr = os.path.join(jd, "narration.wav")
    build_narration(lines, os.path.join(jd, "tts"), shots["duration"], narr)
    timings["tts"] = round(time.time() - t0, 1)

    t0 = time.time()
    part = output + ".part" + os.path.splitext(output)[1]
    mux(video, narr, part, (lambda f: prog("mux", f)), log)
    os.replace(part, output)
    timings["mux"] = round(time.time() - t0, 1)
    spoken = sum(1 for ln in lines if ln["text"] and os.path.exists(os.path.join(jd, "tts", f"{ln['id']}.wav")))
    _save(os.path.join(jd, "summary.json"), {"output": output, "cues": len(cues), "spoken": spoken,
                                              "timings": timings})
    log(f"done: {spoken} descriptions spoken -> {output}  ({timings})")
    return output


def ap_time(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:04.1f}"


def main():
    p = argparse.ArgumentParser(description="Generate AI audio description for a video (local models).")
    p.add_argument("video", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("--model", default="auto", choices=["auto", "cpu", *MODELS])
    p.add_argument("--setup", action="store_true", help="download llama-server + the model, then exit")
    a = p.parse_args()
    if a.setup:
        llama_server_exe()
        model_files(pick_model(a.model)[0])
        return 0
    if not a.video:
        p.error("video required")
    generate(a.video, a.output, a.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
