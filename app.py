#!/usr/bin/env python3
"""Web UI for adpatch - use from this PC's browser or from a phone.

Run:  python app.py   (or start_app.bat)

Starts a small local web server and prints two URLs:
  - http://localhost:8756           - use on this PC
  - http://<your LAN IP>:8756      - open on a phone on the same Wi-Fi
    (a QR code for this URL is printed in the terminal)

The phone uploads the video + AD file over Wi-Fi; this PC does the
alignment and muxing (see adpatch.py); the patched file is downloaded
back. Nothing leaves the local network.
"""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser

try:
    import uvicorn
    from fastapi import FastAPI, File, Form, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    import adpatch
except ImportError as e:
    sys.exit(f"missing package: {e.name} - run start_app.bat, which "
             "installs the dependencies (or: pip install -r "
             "requirements.txt)")

PORT = 8756
JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")

app = FastAPI(title="AudioDescriptionPatcher")

# job id -> {"status": queued|running|done|error, "log": [...],
#            "output": path|None, "error": str|None, "name": str}
jobs = {}

server = None   # the uvicorn.Server, so /api/shutdown can stop it


def is_local(request):
    """Only this PC may stop the server - not phones on the LAN."""
    return (request.client is not None
            and request.client.host in ("127.0.0.1", "::1"))


def run_job(job_id, video_path, ad_path, output_path, replace):
    job = jobs[job_id]
    job["status"] = "running"
    job["started"] = time.time()

    def progress(phase, frac):
        job["phase"], job["frac"] = phase, frac

    try:
        result = adpatch.patch(video_path, ad_path, output=output_path,
                               replace=replace, log=job["log"].append,
                               progress=progress)
        job["output"] = result["output"]
        job["status"] = "done"
        job["ended"] = time.time()
    except Exception as e:   # surface any failure to the UI
        job["error"] = str(e)
        job["status"] = "error"
    finally:
        # uploads are no longer needed once the job has finished
        for p in (video_path, ad_path):
            try:
                os.remove(p)
            except OSError:
                pass


@app.post("/api/patch")
async def start_patch(video: UploadFile = File(...),
                      ad: UploadFile = File(...),
                      replace: bool = Form(False)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def save(upload, fallback):
        name = os.path.basename(upload.filename or fallback)
        dst = os.path.join(job_dir, name)
        with open(dst, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        return dst

    video_path = save(video, "video.mp4")
    ad_path = save(ad, "ad.mp3")
    stem = os.path.splitext(os.path.basename(video_path))[0]
    output_path = os.path.join(job_dir, stem + ".AD.mkv")

    jobs[job_id] = {"status": "queued", "log": [], "output": None,
                    "error": None, "name": stem + ".AD.mkv",
                    "phase": None, "frac": 0.0}
    threading.Thread(target=run_job,
                     args=(job_id, video_path, ad_path, output_path,
                           bool(replace)),
                     daemon=True).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    started = job.get("started")
    elapsed = (job.get("ended") or time.time()) - started if started else 0
    return {"status": job["status"], "log": job["log"],
            "error": job["error"], "name": job["name"],
            "phase": job["phase"], "frac": job["frac"],
            "elapsed": elapsed}


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return JSONResponse({"error": "not ready"}, status_code=404)
    return FileResponse(job["output"], filename=job["name"],
                        media_type="video/x-matroska")


@app.get("/api/info")
def info(request: Request):
    return {"app": "adpatch", "can_shutdown": is_local(request)}


@app.post("/api/shutdown")
def shutdown(request: Request, force: bool = False):
    if not is_local(request):
        return JSONResponse({"error": "only allowed from this PC"},
                            status_code=403)
    running = [j["name"] for j in jobs.values()
               if j["status"] in ("queued", "running")]
    if running and not force:
        return JSONResponse({"error": "jobs running", "running": running},
                            status_code=409)

    def stop():
        time.sleep(0.5)   # let this response reach the browser first
        if running and os.name == "nt":
            # running jobs have ffmpeg children that would outlive us:
            # take down the whole process tree
            subprocess.run(["taskkill", "/F", "/T", "/PID",
                            str(os.getpid())], capture_output=True)
        elif running:
            os._exit(0)
        server.should_exit = True

    threading.Thread(target=stop, daemon=True).start()
    return {"stopping": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AD Patcher</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background: #14161a;
         color: #e8e8e8; margin: 0; padding: 1.2rem;
         display: flex; justify-content: center; }
  main { width: 100%; max-width: 540px; }
  h1 { font-size: 1.4rem; margin: .3rem 0 1rem; }
  .card { background: #1e2127; border-radius: 12px; padding: 1rem;
          margin-bottom: 1rem; }
  label.file { display: block; border: 2px dashed #3a3f4a;
               border-radius: 10px; padding: 1.1rem; text-align: center;
               margin-bottom: .8rem; cursor: pointer; }
  label.file.set { border-color: #4c8f5d; }
  label.file input { display: none; }
  .hint { color: #9aa2af; font-size: .85rem; }
  button { width: 100%; padding: .9rem; font-size: 1.05rem; border: 0;
           border-radius: 10px; background: #3d6bd6; color: white;
           cursor: pointer; }
  button:disabled { background: #333844; color: #777; }
  progress { width: 100%; height: 10px; }
  pre { white-space: pre-wrap; font-size: .78rem; color: #b7c0cc;
        max-height: 45vh; overflow-y: auto; }
  a.dl { display: block; text-align: center; background: #2f8f4e;
         color: white; padding: .9rem; border-radius: 10px;
         text-decoration: none; font-size: 1.05rem; }
  .warn { color: #e6b355; }
  header { display: flex; align-items: center;
           justify-content: space-between; }
  button.stop { width: auto; padding: .45rem .8rem; font-size: .85rem;
                background: #5a2a2a; }
  body.dragging main { outline: 3px dashed #3d6bd6; outline-offset: 6px;
                       border-radius: 12px; }
  details { margin-top: .6rem; }
</style>
</head>
<body>
<main>
  <header>
    <h1>&#127909; Audio Description Patcher</h1>
    <button class="stop" id="stop" hidden
            title="Stop the server on this PC and close">&#9211; Stop</button>
  </header>
  <div class="card" id="pick">
    <label class="file" id="lv" data-slot="video">&#128249; Choose video
      <div class="hint" id="hv">tap to select &middot; or drop it here</div>
      <input type="file" id="fv" accept="video/*,.mkv,.avi,.ts">
    </label>
    <label class="file" id="la" data-slot="ad">&#127911; Choose audio description file
      <div class="hint" id="ha">tap to select &middot; or drop it here</div>
      <input type="file" id="fa"
             accept="audio/*,video/*,.mp3,.m4a,.m4b,.mka">
    </label>
    <div class="hint" style="margin:-.3rem 0 .8rem">
      Tip: drop both files anywhere on the page at once.</div>
    <label style="display:block;margin-bottom:.8rem">
      <input type="checkbox" id="rep"> replace original audio entirely
    </label>
    <button id="go" disabled>Patch it</button>
  </div>
  <div class="card" id="prog" hidden>
    <div id="stage">uploading&hellip;</div>
    <progress id="bar" max="100" value="0"></progress>
    <div class="hint" id="pct"></div>
    <details id="logbox"><summary class="hint">log</summary>
      <pre id="log"></pre></details>
    <a class="dl" id="dl" hidden>&#11015;&#65039; Download patched video</a>
  </div>
</main>
<script>
const stopBtn = document.getElementById('stop');
fetch('api/info').then(r => r.json())
  .then(j => { stopBtn.hidden = !j.can_shutdown; }).catch(() => {});
stopBtn.onclick = async () => {
  if (!confirm('Stop the AD Patcher server?')) return;
  let r = await fetch('api/shutdown', {method: 'POST'});
  if (r.status === 409) {
    const j = await r.json();
    if (!confirm(j.running.length + ' job(s) still running:\\n' +
                 j.running.join('\\n') + '\\n\\nStop anyway?')) return;
    r = await fetch('api/shutdown?force=true', {method: 'POST'});
  }
  if (!r.ok) { alert('Could not stop: ' + (await r.text())); return; }
  document.body.innerHTML = '<main style="text-align:center;margin-top:30vh">' +
    '<h1>Server stopped</h1><p class="hint">You can close this tab.</p></main>';
  window.close();   // only works if a script opened this tab
};

// ---- file selection: picker (remembers folders), input fallback, drop ----
const VIDEO_EXTS = ['.mp4','.mkv','.avi','.mov','.m4v','.webm','.wmv',
                    '.flv','.ts','.mpg','.mpeg','.vob','.3gp'];
const AUDIO_EXTS = ['.mp3','.m4a','.m4b','.aac','.wav','.flac','.ogg',
                    '.opus','.mka','.wma','.ac3'];
const files = {video: null, ad: null};
const go = document.getElementById('go');
const ui = {video: ['lv', 'hv', 'fv'], ad: ['la', 'ha', 'fa']};

function ext(name) { const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase(); }
function isVideo(f) { return VIDEO_EXTS.includes(ext(f.name)) ||
  (f.type.startsWith('video/') && !AUDIO_EXTS.includes(ext(f.name))); }

function setFile(slot, f) {
  files[slot] = f;
  const [lab, hint] = ui[slot];
  document.getElementById(lab).classList.add('set');
  document.getElementById(hint).textContent =
    f.name + '  (' + (f.size / 1e6).toFixed(0) + ' MB)';
  go.disabled = !(files.video && files.ad);
}

// Chromium's File System Access picker remembers the last folder per `id`
// across sessions, separately for video and AD. Only on localhost/https;
// phones and other browsers fall back to the plain <input type=file>.
const PICKER_TYPES = {
  video: [{description: 'Video', accept: {'video/*': VIDEO_EXTS}}],
  ad: [{description: 'Audio description', accept: {'audio/*': AUDIO_EXTS}}],
};
for (const slot of ['video', 'ad']) {
  const [lab, , inp] = ui[slot];
  const input = document.getElementById(inp);
  input.onchange = () => { if (input.files.length) setFile(slot, input.files[0]); };
  document.getElementById(lab).addEventListener('click', async e => {
    if (!window.showOpenFilePicker) return;      // default: opens <input>
    e.preventDefault();
    try {
      const [h] = await showOpenFilePicker({id: 'adpatch-' + slot,
                                            types: PICKER_TYPES[slot]});
      setFile(slot, await h.getFile());
    } catch (err) {
      if (err.name !== 'AbortError') input.click();   // picker failed
    }
  });
}

// Drag and drop: onto a slot = that slot; anywhere else = sorted by type.
let dragDepth = 0;
document.addEventListener('dragenter', e => { e.preventDefault();
  dragDepth++; document.body.classList.add('dragging'); });
document.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0;
    document.body.classList.remove('dragging'); } });
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0;
  document.body.classList.remove('dragging');
  if (!document.getElementById('prog').hidden &&
      document.getElementById('dl').hidden) return;   // job in progress
  const dropped = [...e.dataTransfer.files];
  const target = e.target.closest && e.target.closest('label.file');
  if (target && dropped.length === 1) {
    setFile(target.dataset.slot, dropped[0]); return; }
  for (const f of dropped) {
    if (isVideo(f) && !(files.video && dropped.length > 1 &&
                        dropped.every(isVideo))) setFile('video', f);
    else setFile('ad', f);
  }
});

// ---- run + progress ----
const PHASES = {video: ['1/4', 'Analyzing video audio'],
                ad: ['2/4', 'Analyzing AD audio'],
                align: ['3/4', 'Aligning'],
                mux: ['4/4', 'Writing output']};
const bar = document.getElementById('bar');
const stage = document.getElementById('stage');
const pct = document.getElementById('pct');

go.onclick = () => {
  const fd = new FormData();
  fd.append('video', files.video);
  fd.append('ad', files.ad);
  fd.append('replace', document.getElementById('rep').checked);
  document.getElementById('prog').hidden = false;
  document.getElementById('dl').hidden = true;
  go.disabled = true;
  stage.textContent = 'Uploading\\u2026';
  const t0 = Date.now();
  const xhr = new XMLHttpRequest();
  xhr.open('POST', 'api/patch');
  xhr.upload.onprogress = e => {
    if (!e.total) return;
    bar.value = 100 * e.loaded / e.total;
    const s = (Date.now() - t0) / 1000;
    pct.textContent = bar.value.toFixed(0) + '%  \\u00b7  ' +
      (e.loaded / 1e6).toFixed(0) + ' / ' + (e.total / 1e6).toFixed(0) +
      ' MB  \\u00b7  ' + (e.loaded / 1e6 / Math.max(s, .1)).toFixed(0) + ' MB/s';
  };
  xhr.onload = () => {
    if (xhr.status !== 200) { fail('upload failed: ' + xhr.responseText); return; }
    stage.textContent = 'Processing\\u2026'; bar.value = 0; pct.textContent = '';
    poll(JSON.parse(xhr.responseText).job);
  };
  xhr.onerror = () => fail('upload failed - is the server still running?');
  xhr.send(fd);
};

function fail(msg) {
  stage.innerHTML = '<span class="warn">&#9888;&#65039; </span>';
  stage.firstChild.append(msg);
  go.disabled = !(files.video && files.ad);
}

function poll(id) {
  const t = setInterval(async () => {
    let j;
    try {
      const r = await fetch('api/jobs/' + id);
      if (!r.ok) { clearInterval(t); fail('job lost'); return; }
      j = await r.json();
    } catch { clearInterval(t); fail('server stopped'); return; }
    document.getElementById('log').textContent = j.log.join('\\n');
    if (j.phase && PHASES[j.phase]) {
      const [step, label] = PHASES[j.phase];
      stage.textContent = 'Step ' + step + ': ' + label + '\\u2026';
      bar.value = 100 * j.frac;
      pct.textContent = (100 * j.frac).toFixed(0) + '%' +
        (j.elapsed ? '  \\u00b7  ' + fmtTime(j.elapsed) + ' elapsed' : '');
    }
    if (j.status === 'done') {
      clearInterval(t);
      stage.textContent = 'Done \\u2705'; bar.value = 100;
      pct.textContent = 'took ' + fmtTime(j.elapsed);
      const a = document.getElementById('dl');
      a.href = 'api/jobs/' + id + '/download'; a.hidden = false;
      go.disabled = !(files.video && files.ad);
    } else if (j.status === 'error') {
      clearInterval(t); fail(j.error);
    }
  }, 700);
}

function fmtTime(s) {
  s = Math.round(s);
  return s < 60 ? s + ' s' : Math.floor(s / 60) + ' min ' + (s % 60) + ' s';
}
</script>
</body>
</html>"""


def lan_ip():
    """Best-effort LAN IP: open a UDP socket toward a public address and
    read back which local interface the OS picked (no traffic is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def already_running():
    """True if an adpatch server already answers on PORT."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info",
                                    timeout=2) as r:
            return b"adpatch" in r.read()
    except OSError:
        return False


def port_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", PORT))
            return True
        except OSError:
            return False


def clean_old_jobs(max_age_h=24):
    """Delete job folders (uploads + outputs) older than max_age_h."""
    cutoff = time.time() - max_age_h * 3600
    for name in os.listdir(JOBS_DIR):
        path = os.path.join(JOBS_DIR, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    local_url = f"http://localhost:{PORT}"
    if not port_free():
        if already_running():
            print(f"AD Patcher is already running - opening {local_url}")
            if not os.environ.get("ADPATCH_NO_BROWSER"):
                webbrowser.open(local_url)
            sys.exit(0)
        sys.exit(f"port {PORT} is in use by another program - close it "
                 "and try again")
    os.makedirs(JOBS_DIR, exist_ok=True)
    clean_old_jobs()
    url = f"http://{lan_ip()}:{PORT}"
    print(f"\n  On this PC:    {local_url}")
    print(f"  On your phone: {url}   (same Wi-Fi)\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except (ImportError, UnicodeEncodeError):   # no qrcode / non-UTF-8 console
        pass
    print("If the phone can't connect, allow Python through the Windows "
          "firewall (a prompt appears on first run).\n")
    print("Stop it with the Stop button on this PC's page, or Ctrl+C.\n")
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT,
                                           log_level="warning"))
    if not os.environ.get("ADPATCH_NO_BROWSER"):
        threading.Timer(1.0, webbrowser.open, args=(local_url,)).start()
    server.run()
