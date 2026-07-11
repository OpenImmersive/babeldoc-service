#!/usr/bin/env python3
# OpenImmersive / yulingling — BabelDOC translation wrapper
#
# This is a deliberately thin, zero-business-logic HTTP wrapper around the
# pdf2zh-next CLI (BabelDOC engine, AGPL-3.0). It exists as an arm's-length
# isolation layer: this whole directory is open-sourced to satisfy AGPL
# section 13, while the proprietary Node gateway talks to it over HTTP only.
#
# Rules for this file:
#   - NO billing / auth / quota logic. Ever. That lives in the gateway.
#   - The only integration with pdf2zh-next is spawning its CLI as a
#     subprocess ("mere aggregation" boundary). Never `import pdf2zh_next`.
#
# Endpoints:
#   POST /translate            multipart: file=<pdf> [lang_out=zh] [pages=1-3] [qps=2]
#                              -> {"id": "<job id>"}
#   GET  /status/<id>          -> {"id", "status", "log_tail", ...}
#   GET  /result/<id>          -> translated PDF (mono). ?variant=dual for bilingual.
#   GET  /health               -> {"ok": true}
#
# Jobs are persisted under $DATA_DIR/jobs/<id>/ (survive restart) and are
# deleted after 24h. Translation runs strictly one-at-a-time (CPU inference).

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
PORT = int(os.environ.get("PORT", "21012"))
HOST = os.environ.get("HOST", "0.0.0.0")
# Translation engine flag passed to the CLI (--google, --bing, --deepl, ...).
# Google is free and needs no API key, so it is the default.
ENGINE = os.environ.get("TRANSLATE_ENGINE", "google")
# Extra CLI args for the engine (API keys etc.), e.g. for DeepL:
#   TRANSLATE_ENGINE_ARGS=--deepl-auth-key xxxx
# Values are appended to the CLI verbatim but REDACTED from the job log
# (log tails are served over /status).
ENGINE_ARGS = shlex.split(os.environ.get("TRANSLATE_ENGINE_ARGS", ""))
JOB_TTL_SECONDS = 24 * 3600
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "3600"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
# pdf2zh-next installs its CLI as `pdf2zh` (and historically `pdf2zh_next`).
PDF2ZH_BIN = (
    os.environ.get("PDF2ZH_BIN")
    or shutil.which("pdf2zh")
    or shutil.which("pdf2zh_next")
    or "pdf2zh"
)

_meta_lock = threading.Lock()
_job_queue: "queue.Queue[str]" = queue.Queue()

RE_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
RE_PAGES = re.compile(r"^[0-9]+(-[0-9]*)?(,[0-9]+(-[0-9]*)?)*$")
RE_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{2,8})?$")
RE_ENGINE = re.compile(r"^[a-z][a-z0-9]{1,31}$")


# ---------------------------------------------------------------- job store

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def read_meta(job_id: str):
    p = job_dir(job_id) / "meta.json"
    try:
        with _meta_lock:
            return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def write_meta(job_id: str, meta: dict) -> None:
    p = job_dir(job_id) / "meta.json"
    tmp = p.with_suffix(".json.tmp")
    with _meta_lock:
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        tmp.replace(p)


def update_meta(job_id: str, **fields):
    meta = read_meta(job_id) or {}
    meta.update(fields)
    write_meta(job_id, meta)
    return meta


def log_tail(job_id: str, max_bytes: int = 4000) -> str:
    p = job_dir(job_id) / "log.txt"
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- worker

def run_job(job_id: str) -> None:
    meta = read_meta(job_id)
    if meta is None or meta.get("status") not in ("queued",):
        return
    d = job_dir(job_id)
    out_dir = d / "out"
    out_dir.mkdir(exist_ok=True)
    update_meta(job_id, status="processing", started_at=time.time())

    cmd = [
        PDF2ZH_BIN,
        str(d / "input.pdf"),
        f"--{meta['engine']}",
        "--lang-out", meta["lang_out"],
        "--output", str(out_dir),
        "--qps", str(meta["qps"]),
        "--watermark-output-mode", "no_watermark",
    ]
    if meta.get("pages"):
        cmd += ["--pages", meta["pages"]]
    # Engine credentials go last; the logged command line redacts their values.
    logged = " ".join(cmd) + (" " + " ".join(
        a if a.startswith("-") else "***" for a in ENGINE_ARGS) if ENGINE_ARGS else "")
    cmd += ENGINE_ARGS

    log_path = d / "log.txt"
    try:
        with log_path.open("ab") as lf:
            lf.write((logged + "\n").encode())
            lf.flush()
            proc = subprocess.run(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                timeout=JOB_TIMEOUT_SECONDS,
                cwd=str(d),
            )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        update_meta(job_id, status="failed", finished_at=time.time(),
                    error=f"timeout after {JOB_TIMEOUT_SECONDS}s")
        return
    except OSError as e:
        update_meta(job_id, status="failed", finished_at=time.time(),
                    error=f"failed to spawn {PDF2ZH_BIN}: {e}")
        return

    mono = sorted(out_dir.glob("*mono*.pdf"))
    if rc == 0 and mono:
        update_meta(job_id, status="done", finished_at=time.time(),
                    outputs=sorted(p.name for p in out_dir.glob("*.pdf")))
    else:
        update_meta(job_id, status="failed", finished_at=time.time(),
                    error=f"pdf2zh exited with code {rc}"
                          + ("" if rc != 0 else ", no mono output produced"))


def worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            run_job(job_id)
        except Exception as e:  # keep the single worker alive no matter what
            try:
                update_meta(job_id, status="failed", finished_at=time.time(),
                            error=f"internal: {e}")
            except Exception:
                pass


def cleanup_loop() -> None:
    while True:
        now = time.time()
        try:
            for d in JOBS_DIR.iterdir():
                if not d.is_dir():
                    continue
                try:
                    if now - d.stat().st_mtime > JOB_TTL_SECONDS:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass
        time.sleep(3600)


def recover_jobs() -> None:
    """Re-enqueue jobs interrupted by a restart, oldest first."""
    pending = []
    for d in sorted(JOBS_DIR.iterdir() if JOBS_DIR.exists() else []):
        if not d.is_dir() or not RE_JOB_ID.match(d.name):
            continue
        meta = read_meta(d.name)
        if meta and meta.get("status") in ("queued", "processing"):
            write_meta(d.name, {**meta, "status": "queued"})
            pending.append((meta.get("created_at", 0), d.name))
    for _, job_id in sorted(pending):
        _job_queue.put(job_id)


# ---------------------------------------------------------------- multipart

def parse_multipart(body: bytes, content_type: str) -> dict:
    """Minimal multipart/form-data parser (stdlib cgi was removed in 3.13).

    Returns {field_name: bytes_value}. Good enough for our own trusted
    gateway client; not a general-purpose implementation.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("missing multipart boundary")
    boundary = b"--" + m.group(1).encode()
    fields = {}
    for part in body.split(boundary):
        if part in (b"", b"--", b"--\r\n") or part == b"\r\n":
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        header_blob, value = part.split(b"\r\n\r\n", 1)
        nm = re.search(r'name="([^"]+)"', header_blob.decode("utf-8", "replace"))
        if nm:
            fields[nm.group(1)] = value
    return fields


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "yll-babeldoc/1.0"

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    # -------- POST /translate
    def do_POST(self):
        if self.path.rstrip("/") != "/translate":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"error": "empty body"})
        if length > MAX_UPLOAD_BYTES:
            return self._json(413, {"error": "file too large"})
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._json(400, {"error": "expected multipart/form-data"})

        body = self.rfile.read(length)
        try:
            fields = parse_multipart(body, ctype)
        except ValueError as e:
            return self._json(400, {"error": str(e)})

        pdf = fields.get("file")
        if not pdf:
            return self._json(400, {"error": "missing 'file' field"})
        if not pdf.startswith(b"%PDF-"):
            return self._json(400, {"error": "'file' is not a PDF"})

        lang_out = fields.get("lang_out", b"zh").decode("utf-8", "replace").strip() or "zh"
        pages = fields.get("pages", b"").decode("utf-8", "replace").strip()
        qps_raw = fields.get("qps", b"2").decode("utf-8", "replace").strip() or "2"
        if not RE_LANG.match(lang_out):
            return self._json(400, {"error": "bad lang_out"})
        if pages and not RE_PAGES.match(pages):
            return self._json(400, {"error": "bad pages (use e.g. 1-3 or 1,3,5-)"})
        try:
            qps = min(max(int(qps_raw), 1), 10)
        except ValueError:
            return self._json(400, {"error": "bad qps"})
        # Optional per-request engine override (a pdf2zh engine flag name, e.g.
        # "google"). No allow-list here — an unknown engine simply fails the
        # CLI — and credentials still come ONLY from the deployment env.
        engine = fields.get("engine", b"").decode("utf-8", "replace").strip() or ENGINE
        if not RE_ENGINE.match(engine):
            return self._json(400, {"error": "bad engine"})

        job_id = uuid.uuid4().hex
        d = job_dir(job_id)
        d.mkdir(parents=True)
        (d / "input.pdf").write_bytes(pdf)
        write_meta(job_id, {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "lang_out": lang_out,
            "pages": pages,
            "qps": qps,
            "engine": engine,
            "size_bytes": len(pdf),
        })
        _job_queue.put(job_id)
        self._json(200, {"id": job_id, "status": "queued"})

    # -------- GET /status/<id>, /result/<id>, /health
    def do_GET(self):
        path, _, query = self.path.partition("?")
        parts = [p for p in path.split("/") if p]

        if not parts:
            # AGPL §13: users interacting over the network get the source.
            return self._json(200, {
                "service": "babeldoc-service",
                "description": "Open-source PDF paper translation microservice "
                               "(wrapper around BabelDOC / pdf2zh-next)",
                "source": "https://github.com/OpenImmersive/babeldoc-service",
                "upstream": ["https://github.com/funstory-ai/BabelDOC",
                             "https://github.com/PDFMathTranslate/PDFMathTranslate-next"],
                "license": "AGPL-3.0",
            })

        if parts == ["health"]:
            return self._json(200, {"ok": True, "queue_size": _job_queue.qsize()})

        if len(parts) == 2 and parts[0] in ("status", "result"):
            job_id = parts[1]
            if not RE_JOB_ID.match(job_id):
                return self._json(400, {"error": "bad job id"})
            meta = read_meta(job_id)
            if meta is None:
                return self._json(404, {"error": "unknown job"})

            if parts[0] == "status":
                return self._json(200, {
                    "id": job_id,
                    "status": meta.get("status"),
                    "created_at": meta.get("created_at"),
                    "started_at": meta.get("started_at"),
                    "finished_at": meta.get("finished_at"),
                    "error": meta.get("error"),
                    "outputs": meta.get("outputs"),
                    "log_tail": log_tail(job_id),
                })

            # /result/<id>
            if meta.get("status") != "done":
                return self._json(409, {"error": "job not done",
                                        "status": meta.get("status")})
            variant = "dual" if "variant=dual" in query else "mono"
            matches = sorted((job_dir(job_id) / "out").glob(f"*{variant}*.pdf"))
            if not matches:
                return self._json(404, {"error": f"no {variant} output"})
            data = matches[0].read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition",
                             f'attachment; filename="translated-{variant}.pdf"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    recover_jobs()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"yll-babeldoc wrapper on {HOST}:{PORT}  bin={PDF2ZH_BIN}  "
          f"engine={ENGINE}  data={DATA_DIR}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
