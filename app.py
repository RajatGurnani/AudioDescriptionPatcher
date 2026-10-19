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
    try:
        result = adpatch.patch(video_path, ad_path, output=output_path,
                               replace=replace, log=job["log"].append)
        job["output"] = result["output"]
        job["status"] = "done"
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
                    "error": None, "name": stem + ".AD.mkv"}
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
    return {"status": job["status"], "log": job["log"],
            "error": job["error"], "name": job["name"]}


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
</style>
</head>
<body>
<main>
  <header>
    <h1>&#127909; Audio Description Patcher</h1>
    <button class="stop" id="stop" hidden
            title="Stop the server on this PC and close">&#9211; Stop</button>
  </header>
  <div class="card">
    <label class="file" id="lv">&#128249; Choose video
      <div class="hint" id="hv">tap to select</div>
      <input type="file" id="fv" accept="video/*,.mkv,.avi,.ts">
    </label>
    <label class="file" id="la">&#127911; Choose audio description file
      <div class="hint" id="ha">tap to select</div>
      <input type="file" id="fa"
             accept="audio/*,video/*,.mp3,.m4a,.m4b,.mka">
    </label>
    <label style="display:block;margin-bottom:.8rem">
      <input type="checkbox" id="rep"> replace original audio entirely
    </label>
    <button id="go" disabled>Patch it</button>
  </div>
  <div class="card" id="prog" hidden>
    <div id="stage">uploading&hellip;</div>
    <progress id="bar" max="100" value="0"></progress>
    <pre id="log"></pre>
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

const fv = document.getElementById('fv'), fa = document.getElementById('fa');
const go = document.getElementById('go');
function upd() {
  for (const [inp, lab, hint] of [[fv,'lv','hv'],[fa,'la','ha']]) {
    const l = document.getElementById(lab), h = document.getElementById(hint);
    if (inp.files.length) { l.classList.add('set');
      h.textContent = inp.files[0].name; }
  }
  go.disabled = !(fv.files.length && fa.files.length);
}
fv.onchange = fa.onchange = upd;

go.onclick = () => {
  const fd = new FormData();
  fd.append('video', fv.files[0]);
  fd.append('ad', fa.files[0]);
  fd.append('replace', document.getElementById('rep').checked);
  document.getElementById('prog').hidden = false;
  go.disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', 'api/patch');
  xhr.upload.onprogress = e => {
    document.getElementById('bar').value = e.total ? 100*e.loaded/e.total : 0;
  };
  xhr.onload = () => {
    if (xhr.status !== 200) { fail('upload failed: ' + xhr.responseText); return; }
    document.getElementById('stage').textContent = 'processing\\u2026';
    poll(JSON.parse(xhr.responseText).job);
  };
  xhr.onerror = () => fail('upload failed - is the server still running?');
  xhr.send(fd);
};

function fail(msg) {
  document.getElementById('stage').innerHTML =
    '<span class="warn">&#9888;&#65039; ' + msg + '</span>';
}

function poll(id) {
  const t = setInterval(async () => {
    const r = await fetch('api/jobs/' + id);
    if (!r.ok) { clearInterval(t); fail('job lost'); return; }
    const j = await r.json();
    document.getElementById('log').textContent = j.log.join('\\n');
    if (j.status === 'done') {
      clearInterval(t);
      document.getElementById('stage').textContent = 'done \\u2705';
      const a = document.getElementById('dl');
      a.href = 'api/jobs/' + id + '/download'; a.hidden = false;
    } else if (j.status === 'error') {
      clearInterval(t); fail(j.error);
    }
  }, 1000);
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
