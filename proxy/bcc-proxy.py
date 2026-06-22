#!/usr/bin/env python3
"""
BCC Backend Proxy v3 — Batty Command Center ↔ OpenClaw.

Baut auf der laufenden v2 auf (--local, absoluter Bin-Pfad, cwd, payloads-Parsing,
Projekt-Kontext-Injection) und ergänzt:
  - GET  /setup-api/projects        : jetzt mit updatedAt, prio, pinned, archived, openTasks
  - POST /setup-api/project-create  : legt projects/<slug>/ + VISION/STATUS/TASKS/data.json an
  - POST /setup-api/chat-files      : Chat mit Datei-/Bild-Anhängen (multipart)
  - POST /setup-api/project         : done(+alle Tasks)|reset|prio|name|pinned|archived
  - Async-Chat (POST /chat -> msgId, GET /chat-result) wie gehabt.

Env (Defaults sinnvoll):
  BCC_PORT=8888  BCC_AGENT=main  BCC_THINKING=medium
  BCC_OPENCLAW_BIN=/home/clawbox/.npm-global/bin/openclaw
  BCC_OPENCLAW_CWD=/home/clawbox/.openclaw
  BCC_PROJECTS_DIR=/home/clawbox/.openclaw/workspace/projects
  BCC_STATIC_DIR=/home/clawbox/clawbox/data/webapps/task-central
  BCC_AGENT_TIMEOUT=150  BCC_QUEUE_DIR=/tmp/bcc
"""
import json, os, re, sys, time, uuid, shutil, threading, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import default as email_default

PORT          = int(os.environ.get("BCC_PORT", "8888"))
AGENT         = os.environ.get("BCC_AGENT", "main")
THINKING      = os.environ.get("BCC_THINKING", "medium")
OPENCLAW_BIN  = os.environ.get("BCC_OPENCLAW_BIN", "/home/clawbox/.npm-global/bin/openclaw")
OPENCLAW_CWD  = os.environ.get("BCC_OPENCLAW_CWD", "/home/clawbox/.openclaw")
AGENT_TIMEOUT = int(os.environ.get("BCC_AGENT_TIMEOUT", "150"))
QUEUE_DIR     = os.environ.get("BCC_QUEUE_DIR", "/tmp/bcc")
AUDIO_DIR     = f"{QUEUE_DIR}/audio"

# --- BCC Version (sichtbar in der App über „Batty Command" + GET /whereami) ---
BCC_VERSION   = "6.6.3"
BCC_BUILD     = time.strftime("%Y-%m-%d", time.localtime())

# --- V6.7: Optionales Auth-Gate via Cookie/Header/Key-Parameter -----------
BCC_AUTH_TOKEN = os.environ.get("BCC_AUTH_TOKEN", "").strip()
if not BCC_AUTH_TOKEN:
    try:
        _secrets_path = Path(os.path.expanduser("~/.openclaw/secrets.json"))
        if _secrets_path.exists():
            _secrets_data = json.loads(_secrets_path.read_text())
            BCC_AUTH_TOKEN = (_secrets_data.get("bcc") or {}).get("authToken", "")
    except Exception:
        pass
AUTH_SKIP_PREFIXES = ["/widerruf", "/health", "/whereami",
                      "/manifest.json", "/service-worker.js",
                      "/icon-180.png", "/icon-192.png", "/icon-512.png", "/icon-512-maskable.png"]  # immer offen
if not BCC_AUTH_TOKEN:
    sys.stderr.write(f"[bcc {time.strftime('%H:%M:%S')}] WARNUNG: BCC_AUTH_TOKEN nicht gesetzt — Legacy-Modus, KEIN Auth-Gate!\n")
    sys.stderr.flush()

# --- Project Cache (V6.5.7p1) — entlastet /projects bei 36+ Projekten ---
_PROJECTS_CACHE = None
_PROJECTS_CACHE_TS = 0
_PROJECTS_CACHE_TTL = int(os.environ.get("BCC_CACHE_TTL", "5"))
_PROJECTS_CACHE_LOCK = threading.Lock()

def invalidate_projects_cache(slug=None):
    global _PROJECTS_CACHE, _PROJECTS_CACHE_TS
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE = None
        _PROJECTS_CACHE_TS = 0

# --- Robuste Pfad-Auflösung -------------------------------------------------
# Problem (Heiner, 16.6.): ClawBox-Tools rooten in /home/clawbox/clawbox/, NICHT /home/clawbox/.
# Wir lösen Projekt- und Static-Verzeichnis aus einer Kandidatenliste auf und
# melden den tatsächlich benutzten Pfad über GET /whereami. So gibt es keine
# stille Pfad-Drift mehr: die App zeigt selbst, woher sie liest.
def _first_existing(candidates, need_children=False):
    for c in candidates:
        if not c:
            continue
        p = Path(os.path.expanduser(c))
        if p.exists() and p.is_dir():
            if need_children and not any(x.is_dir() for x in p.iterdir()):
                continue
            return str(p)
    # nichts gefunden -> ersten nicht-leeren Kandidaten zurückgeben (zum Anlegen)
    for c in candidates:
        if c:
            return str(Path(os.path.expanduser(c)))
    return ""

_PROJ_CANDIDATES = [
    os.environ.get("BCC_PROJECTS_DIR"),
    "/home/clawbox/clawbox/.openclaw/workspace/projects",
    "/home/clawbox/clawbox/workspace/projects",
    "/home/clawbox/clawbox/data/projects",
    "/home/clawbox/.openclaw/workspace/projects",
]
_STATIC_CANDIDATES = [
    os.environ.get("BCC_STATIC_DIR"),
    "/home/clawbox/clawbox/data/webapps/task-central",
    "/home/clawbox/clawbox/data/webapps/batty-command",
    "/home/clawbox/clawbox/data/code-projects/batty-command",
    "/home/clawbox/clawbox/webapps/batty-command",
    "/home/clawbox/data/webapps/task-central",
]
PROJECTS_DIR  = _first_existing(_PROJ_CANDIDATES, need_children=True)
STATIC_DIR    = _first_existing(_STATIC_CANDIDATES)
PROJECTS_DIR_RESOLVED = os.environ.get("BCC_PROJECTS_DIR") is None
STATIC_DIR_RESOLVED   = os.environ.get("BCC_STATIC_DIR") is None

os.makedirs(QUEUE_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
def strip_ansi(s): return ANSI.sub("", s or "").strip()
def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip()).strip("-")
    return s or ("projekt-" + uuid.uuid4().hex[:6])

EXEC    = ThreadPoolExecutor(max_workers=4)
RESULTS = {}
RLOCK   = threading.Lock()

def _prune(max_age=900):
    now = time.time()
    with RLOCK:
        for k in [k for k, v in RESULTS.items() if now - v.get("ts", now) > max_age]:
            RESULTS.pop(k, None)

def get_result(msg_id):
    with RLOCK:
        return dict(RESULTS.get(msg_id, {"status": "unknown"}))

# ---------------------------------------------------------------- OpenClaw CLI
def _extract_reply(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            for src in (j.get("result") if isinstance(j.get("result"), dict) else None, j):
                if not src:
                    continue
                for p in (src.get("payloads") or []):
                    t = p.get("text") if isinstance(p, dict) else None
                    if isinstance(t, str) and t.strip():
                        return t.strip()
            for key in ("reply", "response", "text", "message", "output", "content"):
                v = j.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except json.JSONDecodeError:
        pass
    return None

def _project_context(project_slug):
    pdir = Path(PROJECTS_DIR) / project_slug
    out = []
    for fn, lim in (("VISION.md", 500), ("STATUS.md", 3000), ("TASKS.md", 2000)):
        f = pdir / fn
        if f.exists():
            out.append(f"\n{fn}:\n{f.read_text()[:lim]}\n")
    return "".join(out)

def call_openclaw(message, session_id, project_slug=None, msg_id=None, extra_note=None):
    prompt = message
    if project_slug:
        ctx = _project_context(project_slug)
        prompt = (f"[PROJEKT-KONTEXT: {project_slug}]\n{ctx}[ENDE KONTEXT]\n\n"
                  f"Nutze diese echten Projektdaten. Lies bei Bedarf die Dateien im Projektordner.\n\n"
                  f"NUTZER-FRAGE: {message}")
    if extra_note:
        prompt = f"{prompt}\n\n{extra_note}"
    cmd = [OPENCLAW_BIN, "agent", "--agent", AGENT,
           "--session-id", f"bcc-{session_id}", "--thinking", THINKING,
           "--local", "--json", "--message", prompt]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=AGENT_TIMEOUT, cwd=OPENCLAW_CWD)
    except subprocess.TimeoutExpired:
        return ("AlphaBatty denkt noch — der Turn hat das Zeitlimit überschritten. Frag gleich nochmal.", False)
    except FileNotFoundError:
        return (f"OpenClaw-CLI nicht gefunden ('{OPENCLAW_BIN}'). BCC_OPENCLAW_BIN prüfen.", False)
    reply = (_extract_reply(out.stdout) or _extract_reply(out.stderr)
             or strip_ansi(out.stdout) or strip_ansi(out.stderr)
             or "AlphaBatty hat keine Antwort zurückgegeben.")
    tasks_updated = bool(project_slug) or bool(re.search(r"\b(task|erledigt|hinzugefügt|abgehakt|status)\b", reply, re.I))
    return (reply, tasks_updated)

# ---------------------------------------------------------------- Jobs
def _apply_verdict_to_task(session_id, reply, project_slug):
    """V6.3.1+ — Verdict aus normalen Chats. NIE Exception nach außen (V6.3.3)."""
    try:
        if not session_id or not session_id.startswith("task-"):
            return False
        task_id = session_id[len("task-"):]
        v = parse_status(reply)
        if not v:
            return False
        slug = project_slug
        if not slug:
            p, _t = _find_task(task_id)
            slug = p["slug"] if p else None
        if not slug:
            return False
        mapping = {"DONE": "done", "NEEDS_INPUT": "blocked", "BLOCKED": "blocked"}
        new_status = mapping.get(v)
        if not new_status:
            return False
        reason = None
        if new_status == "blocked":
            m = re.search(r"STATUS:\s*(?:NEEDS_INPUT|BLOCKED)\s*[-—:]?\s*(.+)", reply or "", re.I)
            if m:
                reason = m.group(1).strip().split("\n")[0][:160]
        try:
            _set_task_status(slug, task_id, new_status)
        except Exception as e:
            bcc_log(f"verdict set_status failed {slug}/{task_id}: {e}", "verdict", slug)
        if reason:
            try:
                mutate_task(task_id, {"reason": reason})
            except Exception as e:
                bcc_log(f"verdict reason mutate failed: {e}", "verdict", slug)
        return True
    except Exception as e:
        bcc_log(f"_apply_verdict_to_task crashed (swallowed): {e}", "verdict")
        return False

def submit_chat(message, session_id, project_slug, extra_note=None):
    msg_id = uuid.uuid4().hex[:10]
    with RLOCK:
        RESULTS[msg_id] = {"status": "pending", "ts": time.time()}
    def job():
        """V6.3.3: Worker darf NIEMALS sterben. Egal was passiert — Result wird gesetzt."""
        try:
            reply, tu = call_openclaw(message, session_id, project_slug, msg_id, extra_note)
        except Exception as e:
            bcc_log(f"call_openclaw raised: {e}", "chat")
            reply, tu = f"⚠️ Interner Fehler: {e}", False
        try:
            verdict_applied = _apply_verdict_to_task(session_id, reply, project_slug)
        except Exception as e:
            bcc_log(f"verdict hook raised: {e}", "chat")
            verdict_applied = False
        try:
            with RLOCK:
                RESULTS[msg_id] = {"status": "done", "reply": reply,
                                   "tasksUpdated": bool(tu or verdict_applied),
                                   "ts": time.time()}
            # V6.4.7.1: Projekt-Chats in KV persistieren (nicht nur RAM)
            if session_id and not session_id.startswith("task-"):
                key = f"bcc:chat:{session_id}"
                hist = kv_get(key) or []
                hist.append({"role": "heiner", "text": message, "ts": int(time.time())})
                hist.append({"role": "batty", "text": reply, "ts": int(time.time())})
                kv_set(key, hist[-200:])  # max 200 Nachrichten
        except Exception as e:
            bcc_log(f"result store failed: {e}", "chat")
    fut = EXEC.submit(job); _prune()
    # Crash-Detection: wenn der Worker doch stirbt, sehen wir's wenigstens im Log
    def _audit(f):
        exc = f.exception()
        if exc:
            bcc_log(f"EXEC worker died: {exc}", "chat")
    fut.add_done_callback(_audit)
    return msg_id

def submit_audio(audio_path, ctype, session_id, project_slug):
    msg_id = uuid.uuid4().hex[:10]
    with RLOCK:
        RESULTS[msg_id] = {"status": "pending", "ts": time.time()}
    def job():
        text = transcribe(audio_path, ctype)
        if not text:
            with RLOCK:
                RESULTS[msg_id] = {"status": "done", "tasksUpdated": False, "ts": time.time(),
                                   "reply": "🎤 Konnte das Audio nicht transkribieren. Ist `whisper` installiert?"}
            return
        reply, tu = call_openclaw(text, session_id, project_slug, msg_id)
        verdict_applied = _apply_verdict_to_task(session_id, reply, project_slug)
        with RLOCK:
            RESULTS[msg_id] = {"status": "done", "reply": f"🎤 „{text}“\n\n{reply}",
                               "transcript": text,
                               "tasksUpdated": tu or verdict_applied, "ts": time.time()}
    EXEC.submit(job); _prune()
    return msg_id

def transcribe(path, ctype):
    if shutil.which("whisper"):
        out_dir = os.path.dirname(path)
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            subprocess.run(["whisper", path, "--model", "base", "--language", "de",
                            "--output_format", "txt", "--output_dir", out_dir],
                           capture_output=True, text=True, timeout=180)
        except Exception:
            return None
        cand = os.path.join(out_dir, base + ".txt")
        if os.path.exists(cand):
            return Path(cand).read_text().strip()
    return None

# ---------------------------------------------------------------- Projekte lesen
# V6.3: BCC ist Fenster & Spiegel der echten Ordner. TASKS.md ist Source of Truth
# für Tasks; data.json (falls vorhanden) ist nur ein Overlay für Felder, die
# Markdown nicht ausdrückt (workspace, city, prio, vision-pro-task, stabile id).

_STATUS_SECTION = [
    (("DONE", "ERLEDIGT", "FERTIG", "✅"), "done"),
    (("IN_PROGRESS", "IN ARBEIT", "LÄUFT", "🔵", "🔄"), "in_progress"),
    (("BLOCKED", "BLOCKIERT", "❌"), "blocked"),
    (("WAITING", "WARTET", "WARTEND", "⏳"), "waiting"),
    (("TODAY", "HEUTE", "DRINGEND", "🔴"), "today"),
    (("IDEA", "IDEE", "BACKLOG", "💡"), "idea"),
    (("PENDING", "OFFEN", "TODO", "TO DO"), "pending"),
]
_EMOJI_STATUS = {"🔴": "today", "⏳": "waiting", "❌": "blocked", "🚫": "blocked",
                 "💡": "idea", "🔵": "in_progress", "🔄": "in_progress", "✅": "done", "✔": "done"}

def _stable_tid(slug, title):
    import hashlib
    h = hashlib.md5(title.strip().lower().encode("utf-8")).hexdigest()[:4]
    return f"{slug[:6]}-{h}"

def parse_tasks_md(text, slug):
    """Robuster TASKS.md-Parser. Erkennt Sektions-Header, Checkboxen, Emoji-Status,
    Einrückung (=Subtask), optionale {id}-Tags und '> vision'-Zeilen."""
    tasks = []
    section = "pending"
    last_top = None
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        # Sektions-Header (## DONE / ### IN_PROGRESS ...)
        if line.lstrip().startswith("#"):
            head = line.lstrip("#").strip().upper()
            for keys, val in _STATUS_SECTION:
                if any(k in head for k in keys):
                    section = val
                    break
            last_top = None
            continue
        # Vision-Zeile zu vorigem Task: '> ...' oder '  vision: ...'
        m_vis = re.match(r'\s*(?:>|vision:)\s*(.+)', line, re.I)
        if m_vis and tasks:
            if not tasks[-1].get("vision"):
                tasks[-1]["vision"] = m_vis.group(1).strip()
            continue
        # Checkbox-Zeile
        m = re.match(r'(\s*)[-*]\s*\[([ xX~\-])\]\s*(.*)', line)
        if not m:
            continue
        indent, box, rest = m.group(1), m.group(2), m.group(3).strip()
        # Emoji-Status-Prefix (zuerst, damit {id} dahinter noch erkannt wird)
        status = None
        for emo, st in _EMOJI_STATUS.items():
            if emo in rest:
                status = st
                rest = rest.replace(emo, "").strip()
        # {id}-Tag (an beliebiger Position)
        tid = None
        m_id = re.search(r'\{([a-z0-9\-]+)\}', rest, re.I)
        if m_id:
            tid = m_id.group(1)
            rest = (rest[:m_id.start()] + rest[m_id.end():]).strip()
        # inline vision via ' :: '
        vision = None
        if "::" in rest:
            rest, vision = [x.strip() for x in rest.split("::", 1)]
        # Status aus Checkbox / Sektion
        if box in ("x", "X"):
            status = "done"
        elif box == "~":
            status = status or "in_progress"
        else:
            status = status or section
        title = rest.strip()
        if not title:
            continue
        tid = tid or _stable_tid(slug, title)
        is_sub = len(indent) >= 2 and last_top is not None
        task = {"id": tid, "title": title, "status": status, "vision": vision,
                "context": vision,  # V6.3.5: das Inline-`:: …` ist auch der Kurz-Kontext
                "parentTaskId": last_top["id"] if is_sub else None,
                "projectSlug": slug, "who": "batty", "prio": "yellow", "dependsOn": []}
        tasks.append(task)
        if not is_sub:
            last_top = task
    return tasks

def _fallback_from_md(slug, vision_md, status_md):
    name = slug.replace("-", " ").title()
    vision = (vision_md.split("\n", 1)[0] if vision_md else "")[:200]
    done = any(k in status_md.upper() for k in ("LIVE", "FERTIG", "ABGESCHLOSSEN", "DONE"))
    return {"slug": slug, "name": name, "vision": vision,
            "statusLabel": "Abgeschlossen" if done else "", "done": done, "tasks": []}

def _dir_mtime(pdir):
    newest = 0.0
    for fn in ("data.json", "STATUS.md", "TASKS.md", "VISION.md"):
        f = pdir / fn
        if f.exists():
            newest = max(newest, f.stat().st_mtime)
    return int(newest or pdir.stat().st_mtime)

def _read_all_projects():
    """Roh-Leser (ungecached). read_projects() cached via read_projects_cached()."""
    res = []
    root = Path(PROJECTS_DIR)
    if not root.exists():
        return res
    for pdir in sorted(root.iterdir()):
        if not pdir.is_dir():
            continue
        slug = pdir.name
        if slug.startswith("."):
            continue
        try:
            res.append(_read_one_project(pdir, slug))
        except Exception as e:
            # V6.4.0: EIN kaputtes Projekt darf NIE alle anderen verschwinden lassen.
            bcc_log(f"read_projects: Projekt {slug} übersprungen: {e}", "read_projects", slug)
            res.append({"slug": slug, "name": slug.replace("-", " ").title(),
                        "tasks": [], "workspaces": [], "city": None, "tags": [],
                        "statusLabel": "⚠️ Lesefehler", "done": False, "prio": "red",
                        "pinned": False, "archived": False, "parentFolder": None,
                        "openTasks": 0, "updatedAt": 0, "md": {"vision": "", "status": "", "tasks": ""},
                        "hasTasksMd": False, "readError": str(e)[:200]})
    return res

def read_projects():
    """Cached project list (TTL via BCC_CACHE_TTL env, default 5s)."""
    global _PROJECTS_CACHE, _PROJECTS_CACHE_TS
    now = time.time()
    with _PROJECTS_CACHE_LOCK:
        if _PROJECTS_CACHE is not None and (now - _PROJECTS_CACHE_TS) < _PROJECTS_CACHE_TTL:
            return _PROJECTS_CACHE
    data = _read_all_projects()
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE = data
        _PROJECTS_CACHE_TS = time.time()
    return data

def _read_one_project(pdir, slug):
        vision_md = (pdir / "VISION.md").read_text() if (pdir / "VISION.md").exists() else ""
        status_md = (pdir / "STATUS.md").read_text() if (pdir / "STATUS.md").exists() else ""
        tasks_md  = (pdir / "TASKS.md").read_text()  if (pdir / "TASKS.md").exists()  else ""
        # 1) Markdown = Source of Truth
        p = _fallback_from_md(slug, vision_md, status_md)
        md_tasks = parse_tasks_md(tasks_md, slug)
        p["tasks"] = md_tasks
        # 2) data.json = optionales Overlay (workspace, city, prio, task-vision/id)
        df = pdir / "data.json"
        overlay = {}
        if df.exists():
            try:
                raw = json.loads(df.read_text())
                overlay = raw if isinstance(raw, dict) else {}
            except Exception:
                overlay = {}
        for k in ("name", "workspaces", "city", "tags", "prio", "pinned", "archived",
                  "done", "statusLabel", "vision", "parentFolder", "emoji", "color"):
            if overlay.get(k) not in (None, "", [], {}):
                p[k] = overlay[k]
        # Task-Overlay per Titel-Match (vision/prio/who/dependsOn/calendarDate/reason)
        if overlay.get("tasks"):
            safe_tasks = [t for t in overlay["tasks"] if isinstance(t, dict)]
            by_title = {(t.get("title") or "").strip().lower(): t for t in safe_tasks}
            by_id = {t.get("id"): t for t in safe_tasks if t.get("id")}
            for t in p["tasks"]:
                o = by_id.get(t.get("id")) or by_title.get((t.get("title") or "").strip().lower())
                if o:
                    # V6.3.5: 'context' + 'summary' mitnehmen (das ist „worum geht's + Stand + offene Fragen")
                    for k in ("vision", "context", "summary", "prio", "who", "dependsOn", "calendarDate", "reason", "id"):
                        if o.get(k) not in (None, "", []):
                            t[k] = o[k]
            # rein in data.json definierte Tasks (ohne MD-Pendant) ergänzen
            if not md_tasks:
                p["tasks"] = [t for t in overlay["tasks"] if isinstance(t, dict)]
        # V6.3.5: optionale Detail-Datei tasks/<id>.md pro Task einlesen (langer Kontext)
        tdir = pdir / "tasks"
        if tdir.is_dir():
            for t in p["tasks"]:
                tid = t.get("id")
                if not tid:
                    continue
                tf = tdir / f"{tid}.md"
                try:
                    if tf.exists():
                        t["contextDetail"] = tf.read_text()[:4000]
                except Exception:
                    pass
        # Defaults
        p.setdefault("slug", slug)
        p.setdefault("name", slug.replace("-", " ").title())
        p.setdefault("tasks", [])
        p.setdefault("statusLabel", "")
        p.setdefault("prio", None)
        p.setdefault("pinned", False)
        p.setdefault("archived", False)
        p.setdefault("done", False)
        p.setdefault("workspaces", [])
        p.setdefault("city", None)
        p.setdefault("tags", [])
        p.setdefault("parentFolder", None)
        p.setdefault("emoji", None)
        p.setdefault("color", None)
        for t in p["tasks"]:
            t.setdefault("projectSlug", slug)
            t.setdefault("parentTaskId", None)
            t.setdefault("dependsOn", [])
            t.setdefault("vision", None)
            t.setdefault("context", None)
            t.setdefault("summary", None)
            t.setdefault("who", "batty")
            t.setdefault("prio", "yellow")
            t["running"] = t.get("id") in RUNNING
        # „Task mit ≥2 Subtasks = Projekt" (Heiners Modell) — Flag für UI
        sub_count = {}
        for t in p["tasks"]:
            if t.get("parentTaskId"):
                sub_count[t["parentTaskId"]] = sub_count.get(t["parentTaskId"], 0) + 1
        for t in p["tasks"]:
            t["isProjectLike"] = sub_count.get(t.get("id"), 0) >= 2
        p["openTasks"] = sum(1 for t in p["tasks"] if t.get("status") not in ("done", "idea"))
        p["updatedAt"] = overlay.get("updatedAt") or _dir_mtime(pdir)
        p["md"] = {"vision": vision_md, "status": status_md, "tasks": tasks_md}
        p["hasTasksMd"] = bool(tasks_md.strip())
        return p

# ---------------------------------------------------------------- Schreiben
def _write_data(slug, mutate):
    """V6.3.3: per-slug-lock + atomic write (tmp→rename), nie korrupte data.json."""
    with _slug_lock(slug):
        df = Path(PROJECTS_DIR) / slug / "data.json"
        try:
            p = json.loads(df.read_text()) if df.exists() else {"slug": slug, "tasks": []}
        except Exception:
            p = {"slug": slug, "tasks": []}
        try:
            mutate(p)
        except Exception as e:
            bcc_log(f"_write_data mutate failed for {slug}: {e}", "write_data", slug)
            return False
        p["updatedAt"] = int(time.time())
        df.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = df.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2))
            os.replace(str(tmp), str(df))
        except Exception as e:
            bcc_log(f"_write_data write failed for {slug}: {e}", "write_data", slug)
            return False
        return True

_BOX_FOR = {"done": "x", "in_progress": "~", "pending": " ", "today": " ",
            "waiting": " ", "blocked": " ", "idea": " "}
_EMOJI_FOR = {"today": "🔴", "waiting": "⏳", "blocked": "❌", "idea": "💡"}

def update_tasks_md(slug, task_id, new_status):
    """V6.3.3: per-slug-lock + atomic + harte Fehlertoleranz. Wirft NIE."""
    try:
        f = Path(PROJECTS_DIR) / slug / "TASKS.md"
        if not f.exists():
            return False
        # V6.4.7.1: BATTY-PROTECT — Dateien mit diesem Marker werden NIE automatisch modifiziert
        first_line = f.read_text().split("\n")[0] if f.exists() else ""
        if "BATTY-PROTECT" in first_line:
            return False
        with _slug_lock(slug):
            lines = f.read_text().split("\n")
            out, changed = [], False
            for raw in lines:
                m = re.match(r'(\s*)([-*])\s*\[([ xX~\-])\]\s*(.*)', raw)
                if not m:
                    out.append(raw); continue
                indent, bullet, box, rest = m.groups()
                body = rest
                tid_tag = re.search(r'\{([a-z0-9\-]+)\}', body, re.I)
                title_for_id = re.sub(r'\{[a-z0-9\-]+\}', '', body, flags=re.I)
                for emo in _EMOJI_STATUS:
                    title_for_id = title_for_id.replace(emo, "")
                title_for_id = title_for_id.split("::")[0].strip()
                this_id = tid_tag.group(1) if tid_tag else _stable_tid(slug, title_for_id)
                if this_id == task_id and not changed:
                    newbox = _BOX_FOR.get(new_status, " ")
                    clean = body
                    id_keep = (tid_tag.group(0) + " ") if tid_tag else ""
                    clean = re.sub(r'\{[a-z0-9\-]+\}', '', clean, flags=re.I)
                    for emo in _EMOJI_STATUS:
                        clean = clean.replace(emo, "")
                    clean = clean.strip()
                    emo_new = _EMOJI_FOR.get(new_status, "")
                    body2 = f"{id_keep}{(emo_new+' ') if emo_new else ''}{clean}".strip()
                    out.append(f"{indent}{bullet} [{newbox}] {body2}")
                    changed = True
                else:
                    out.append(raw)
            if changed:
                tmp = f.with_suffix(".md.tmp")
                tmp.write_text("\n".join(out))
                os.replace(str(tmp), str(f))
            return changed
    except Exception as e:
        bcc_log(f"update_tasks_md failed for {slug}/{task_id}: {e}", "update_tasks_md", slug)
        return False

def _find_task(task_id):
    for p in read_projects():
        for t in p.get("tasks", []):
            if t.get("id") == task_id:
                return p, t
    return None, None

def mutate_task(task_id, patch):
    p, t = _find_task(task_id)
    if not p:
        return False
    slug = p["slug"]
    new_status = "pending" if patch.get("reset") else patch.get("status")
    if new_status:
        update_tasks_md(slug, task_id, new_status)
    # Overlay (prio/vision/reason/status-id-Map) in data.json sichern
    def m(pr):
        pr.setdefault("tasks", [])
        row = next((x for x in pr["tasks"] if x.get("id") == task_id), None)
        if not row:
            row = {"id": task_id, "title": t.get("title")}
            pr["tasks"].append(row)
        if patch.get("reset"):
            row["status"] = "pending"
        for k in ("status", "prio", "vision", "context", "summary", "reason", "who", "title"):
            if k in patch:
                row[k] = patch[k]
    _write_data(slug, m)
    # V6.4.0: Titel-Änderung auch in TASKS.md durchschreiben (über stabile {id})
    if patch.get("title"):
        try:
            _rename_task_in_md(slug, task_id, patch["title"])
        except Exception as e:
            bcc_log(f"title rewrite failed {slug}/{task_id}: {e}", "mutate_task", slug)
    return True

def _rename_task_in_md(slug, task_id, new_title):
    f = Path(PROJECTS_DIR) / slug / "TASKS.md"
    if not f.exists():
        return
    with _slug_lock(slug):
        lines = f.read_text().split("\n")
        out, changed = [], False
        for raw in lines:
            m = re.match(r'(\s*[-*]\s*\[[ xX~\-]\]\s*)(.*)', raw)
            if not m or changed:
                out.append(raw); continue
            prefix, bodytext = m.groups()
            tid_tag = re.search(r'\{([a-z0-9\-]+)\}', bodytext, re.I)
            this_id = tid_tag.group(1) if tid_tag else _stable_tid(slug, re.sub(r'\{[a-z0-9\-]+\}','',bodytext.split("::")[0]).strip())
            if this_id == task_id:
                id_part = (tid_tag.group(0)+" ") if tid_tag else (f"{{{task_id}}} ")
                vis = ""
                if "::" in bodytext:
                    vis = " :: " + bodytext.split("::",1)[1].strip()
                out.append(f"{prefix}{id_part}{new_title}{vis}")
                changed = True
            else:
                out.append(raw)
        if changed:
            tmp = f.with_suffix(".md.tmp"); tmp.write_text("\n".join(out)); os.replace(str(tmp), str(f))

def mutate_project(slug, patch):
    def m(p):
        if patch.get("reset"):
            p["done"] = False; p["statusLabel"] = "Läuft"
        elif patch.get("done"):
            p["done"] = True; p["statusLabel"] = "✓ Abgeschlossen"
            for t in p.get("tasks", []):
                if t.get("status") != "idea":
                    t["status"] = "done"
        if "prio" in patch:     p["prio"] = patch["prio"]
        if "name" in patch:     p["name"] = patch["name"]
        if "pinned" in patch:   p["pinned"] = bool(patch["pinned"])
        if "archived" in patch: p["archived"] = bool(patch["archived"])
        if "parentFolder" in patch: p["parentFolder"] = patch["parentFolder"] or None
        if "workspaces" in patch and isinstance(patch["workspaces"], list): p["workspaces"] = patch["workspaces"]
        # V6.6.0: Struktur deploy-fest in data.json — Emoji & manuelle Farbe (status6) persistieren
        if "emoji" in patch: p["emoji"] = patch["emoji"] or None
        if "color" in patch: p["color"] = patch["color"] or None
    return _write_data(slug, m)

def delete_project(slug):
    """V6.5.5: sicheres Löschen — Ordner wandert nach .trash/, wird NICHT hart vernichtet."""
    pdir = Path(PROJECTS_DIR) / slug
    if not pdir.is_dir():
        return {"ok": False, "error": "not found"}
    trash = Path(PROJECTS_DIR) / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{slug}-{int(time.time())}"
    try:
        shutil.move(str(pdir), str(dest))
        return {"ok": True, "trashed": str(dest)}
    except Exception as e:
        bcc_log(f"delete_project failed for {slug}: {e}", "delete_project", slug)
        return {"ok": False, "error": str(e)}

def create_project(name, vision="", prio=None, workspaces=None, city=None, tags=None, quiet=False, parent_folder=None):
    slug = slugify(name)
    pdir = Path(PROJECTS_DIR) / slug
    if pdir.exists():
        slug = slug + "-" + uuid.uuid4().hex[:4]
        pdir = Path(PROJECTS_DIR) / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "VISION.md").write_text(f"# Vision — {name}\n\n{vision or 'Noch zu definieren.'}\n")
    (pdir / "STATUS.md").write_text(f"# Status — {name}\n\n**Aktueller Stand:** Neu angelegt.\n")
    (pdir / "TASKS.md").write_text(f"# Tasks — {name}\n\n_Noch keine Tasks._\n")
    data = {"slug": slug, "name": name, "vision": vision, "statusLabel": "Neu",
            "prio": prio, "pinned": False, "archived": False, "done": False,
            "workspaces": workspaces or [], "city": city, "tags": tags or [],
            "parentFolder": parent_folder or None,
            "tasks": [], "updatedAt": int(time.time())}
    (pdir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    if not quiet:
        note = (f"Es wurde ein neues Projekt '{slug}' angelegt (Name: {name}; Ziel: {vision or '—'}). "
                f"Ergänze VISION.md, STATUS.md, TASKS.md und data.json sinnvoll und konsistent.")
        submit_chat(note, f"proj-{slug}", slug)
    return data

def ensure_project(slug, name=None):
    pdir = Path(PROJECTS_DIR) / slug
    if (pdir / "data.json").exists():
        return slug
    return create_project(name or slug.replace("-", " ").title(), quiet=True)["slug"]

# ---------------------------------------------------------------- KV (serverseitig, dateibasiert)
KV_FILE = Path(QUEUE_DIR) / "kv.json"
KVLOCK = threading.Lock()
def kv_get(key):
    with KVLOCK:
        try:
            return json.loads(KV_FILE.read_text()).get(key)
        except Exception:
            return None
def kv_set(key, value):
    with KVLOCK:
        try:
            store = json.loads(KV_FILE.read_text())
        except Exception:
            store = {}
        store[key] = value
        KV_FILE.write_text(json.dumps(store, ensure_ascii=False))
    return True

# ---------------------------------------------------------------- Task-Mobilität / PLAY
def find_task(task_id):
    for pdir in Path(PROJECTS_DIR).iterdir():
        if not pdir.is_dir() or not (pdir / "data.json").exists():
            continue
        try:
            d = json.loads((pdir / "data.json").read_text())
        except Exception:
            continue
        for t in d.get("tasks", []):
            if t.get("id") == task_id:
                return pdir.name, t
    return None, None

def task_move(task_id, to_slug):
    from_slug, task = find_task(task_id)
    if not task or not (Path(PROJECTS_DIR) / to_slug / "data.json").exists() or from_slug == to_slug:
        return False
    _write_data(from_slug, lambda p: p.__setitem__("tasks", [t for t in p["tasks"] if t.get("id") != task_id]))
    task["projectSlug"] = to_slug
    _write_data(to_slug, lambda p: p.setdefault("tasks", []).append(task))
    return True

def merge_project(from_slug, to_slug):
    """V6.4.0: ganzes Projekt einbetten — Tasks (TASKS.md + data.json) + STATUS-Verweis ins Ziel,
    Quelle wird ARCHIVIERT (archived:true), NIEMALS gelöscht. Reversibel über Archiv."""
    if not from_slug or not to_slug or from_slug == to_slug:
        return {"error": "ungültige Slugs"}
    src_dir = Path(PROJECTS_DIR) / from_slug
    dst_dir = Path(PROJECTS_DIR) / to_slug
    if not src_dir.is_dir() or not dst_dir.is_dir():
        return {"error": "Projekt nicht gefunden"}
    src = _read_one_project(src_dir, from_slug)
    moved = 0
    # 1) Tasks rüber: ans Ziel-TASKS.md anhängen + data.json-Overlay übernehmen
    src_tasks = src.get("tasks", [])
    if src_tasks:
        dst_tasks_md = dst_dir / "TASKS.md"
        existing = dst_tasks_md.read_text() if dst_tasks_md.exists() else "# TASKS\n"
        lines = [f"\n## Eingebettet aus {src.get('name', from_slug)} ({time.strftime('%Y-%m-%d')})"]
        for t in src_tasks:
            box = {"done": "x", "in_progress": "~"}.get(t.get("status"), " ")
            emo = {"today": "🔴", "waiting": "⏳", "blocked": "❌", "idea": "💡"}.get(t.get("status"), "")
            ctx = t.get("context") or t.get("vision")
            tid = t.get("id") or _stable_tid(to_slug, t.get("title", ""))
            line = f"- [{box}] {{{tid}}} {(emo+' ') if emo else ''}{t.get('title','(ohne Titel)')}"
            if ctx:
                line += f" :: {ctx}"
            lines.append(line)
            moved += 1
        with _slug_lock(to_slug):
            tmp = dst_tasks_md.with_suffix(".md.tmp")
            tmp.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n")
            os.replace(str(tmp), str(dst_tasks_md))
        # data.json-Overlay der Tasks ans Ziel hängen (für context/prio/who)
        def add_overlay(pr):
            pr.setdefault("tasks", [])
            have = {x.get("id") for x in pr["tasks"]}
            for t in src_tasks:
                if t.get("id") and t["id"] not in have:
                    pr["tasks"].append({k: t.get(k) for k in ("id", "title", "status", "prio", "who", "vision", "context", "summary") if t.get(k) is not None})
        _write_data(to_slug, add_overlay)
    # 2) Quelle archivieren (NICHT löschen)
    _write_data(from_slug, lambda p: (p.__setitem__("archived", True), p.__setitem__("statusLabel", f"→ eingebettet in {to_slug}")))
    # 3) STATUS.md der Quelle mit Hinweis versehen
    try:
        sf = src_dir / "STATUS.md"
        note = f"\n\n---\n**{time.strftime('%Y-%m-%d')}: Dieses Projekt wurde in `{to_slug}` eingebettet (archiviert).**\n"
        sf.write_text((sf.read_text() if sf.exists() else f"# STATUS — {from_slug}\n") + note)
    except Exception:
        pass
    return {"ok": True, "movedTasks": moved, "from": from_slug, "to": to_slug}

# ---------------- Cron-Jobs (V6.4.0) ----------------
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)

# ---------------- Projekt-Dateien (V6.4.1 Daten-Vorschau) ----------------
_PREVIEW_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".md", ".txt", ".html", ".htm", ".csv", ".json"}
_PREVIEW_DIRS = ["outputs", "uploads", "content", "assets", "review-packages", "data", "exports", "kontakte"]

def project_files(slug):
    """Listet vorschaubare Dateien eines Projekts (Bilder/PDF/MD/HTML/TXT/CSV/JSON)
    aus dem Projektordner + typischen Unterordnern. Nur Metadaten, kein Inhalt."""
    if not slug:
        return {"files": []}
    pdir = Path(PROJECTS_DIR) / slug
    if not pdir.is_dir():
        return {"files": []}
    files = []
    seen = set()
    def add(fp):
        try:
            if fp.suffix.lower() not in _PREVIEW_EXT:
                return
            rel = str(fp.relative_to(pdir))
            if rel in seen:
                return
            seen.add(rel)
            st = fp.stat()
            files.append({
                "name": fp.name, "rel": rel,
                "ext": fp.suffix.lower().lstrip("."),
                "size": st.st_size, "mtime": int(st.st_mtime),
                "kind": ("image" if fp.suffix.lower() in (".png",".jpg",".jpeg",".webp",".gif")
                         else "pdf" if fp.suffix.lower()==".pdf"
                         else "doc"),
                "url": f"/setup-api/project-file?slug={slug}&rel={rel}"
            })
        except Exception:
            pass
    # Top-Level (ohne die 5 Kern-Dateien)
    core = {"VISION.md", "PROJECT.md", "STATUS.md", "TASKS.md", "data.json"}
    try:
        for fp in sorted(pdir.iterdir()):
            if fp.is_file() and fp.name not in core:
                add(fp)
    except Exception:
        pass
    # typische Unterordner (max. 2 Ebenen, gedeckelt)
    for sub in _PREVIEW_DIRS:
        d = pdir / sub
        if d.is_dir():
            try:
                for fp in sorted(d.rglob("*")):
                    if fp.is_file():
                        add(fp)
                    if len(files) > 60:
                        break
            except Exception:
                pass
        if len(files) > 60:
            break
    files.sort(key=lambda f: -f["mtime"])
    return {"files": files[:60]}

def cron_list():
    """Liest die echte User-Crontab. Nur lesen."""
    try:
        out = _run(["crontab", "-l"])
        raw = out.stdout if out.returncode == 0 else ""
    except Exception as e:
        return {"jobs": [], "error": str(e)[:120]}
    jobs = []
    for i, line in enumerate(raw.split("\n")):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # BCC-Jobs tragen optional ein Label: "# bcc:<id>:<label>" in der Vorzeile — simpel: ID = Zeilennummer-Hash
        parts = s.split(None, 5)
        if len(parts) >= 6:
            sched = " ".join(parts[:5]); cmd = parts[5]
        elif s.startswith("@"):
            p2 = s.split(None, 1); sched = p2[0]; cmd = p2[1] if len(p2) > 1 else ""
        else:
            sched = ""; cmd = s
        jobs.append({"id": f"cron-{i}", "schedule": sched, "command": cmd, "raw": s})
    return {"jobs": jobs}

def cron_save(d):
    """Fügt einen Job hinzu oder ersetzt einen bestehenden (per raw-Match). Sicher: schreibt komplette Crontab atomar."""
    sched = (d.get("schedule") or "").strip()
    cmd = (d.get("command") or "").strip()
    old_raw = (d.get("raw") or "").strip()
    if not sched or not cmd:
        return {"error": "schedule und command nötig"}
    try:
        cur = _run(["crontab", "-l"])
        lines = cur.stdout.split("\n") if cur.returncode == 0 else []
    except Exception as e:
        return {"error": str(e)[:120]}
    new_line = f"{sched} {cmd}"
    if old_raw:
        lines = [new_line if l.strip() == old_raw else l for l in lines]
        if new_line not in [l.strip() for l in lines]:
            lines.append(new_line)
    else:
        lines.append(new_line)
    payload = "\n".join([l for l in lines if l.strip() != ""]) + "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=payload, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            return {"error": proc.stderr[:160] or "crontab schreibgeschützt"}
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"ok": True}

def cron_delete(job_id):
    """Entfernt EINEN Job per id (Zeilenindex). Konservativ."""
    if not job_id:
        return {"error": "id nötig"}
    try:
        cur = _run(["crontab", "-l"])
        if cur.returncode != 0:
            return {"error": "keine Crontab"}
        lines = [l for l in cur.stdout.split("\n")]
    except Exception as e:
        return {"error": str(e)[:120]}
    # job_id = "cron-<n>" → n-te nicht-leere/nicht-kommentar Zeile
    try:
        idx = int(str(job_id).split("-")[-1])
    except Exception:
        return {"error": "ungültige id"}
    keep = []
    for i, l in enumerate(lines):
        if i == idx:
            continue
        keep.append(l)
    payload = "\n".join([l for l in keep if l.strip() != ""]) + "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=payload, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0:
            return {"error": proc.stderr[:160]}
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"ok": True}

def _set_task_status(slug, task_id, status):
    # Auch TASKS.md (Source of Truth) zurückschreiben — egal ob aus Loop, Chat-Verdict oder UI
    try:
        update_tasks_md(slug, task_id, status)
    except Exception:
        pass
    def m(p):
        for t in p.get("tasks", []):
            if t.get("id") == task_id:
                t["status"] = status
                if status == "done":
                    t["doneAt"] = int(time.time())
                if status == "blocked" and "doneAt" in t:
                    t.pop("doneAt", None)
    ok = _write_data(slug, m)
    if ok:
        invalidate_projects_cache(slug)

# ---- Lauf-Zustand: welche Tasks/Projekte laufen, welche sollen stoppen ----
RUNNING   = set()      # task_ids, die gerade in einem Loop sind
STOP_TASK = set()      # task_ids, die nach dem aktuellen Turn stoppen sollen
STOP_PROJ = set()      # slugs, deren Projekt-Loop stoppen soll
RUNLOCK   = threading.Lock()
MAX_TURNS = int(os.environ.get("BCC_MAX_TURNS", "6"))      # Sicherheitsdeckel pro Task
PLAY_CONCURRENCY = int(os.environ.get("BCC_PLAY_CONCURRENCY", "2"))

# V6.3.3: Per-Slug-File-Lock gegen Schreib-Races (TASKS.md + data.json gleichzeitig)
FILELOCKS_LOCK = threading.Lock()
FILELOCKS = {}
def _slug_lock(slug):
    with FILELOCKS_LOCK:
        lk = FILELOCKS.get(slug)
        if lk is None:
            lk = threading.RLock()
            FILELOCKS[slug] = lk
        return lk

def parse_status(reply):
    """Liest das Verdikt, das AlphaBatty an jede Runde anhängt."""
    m = re.search(r"STATUS:\s*(DONE|CONTINUE|NEEDS_INPUT|BLOCKED)", reply or "", re.I)
    if m:
        return m.group(1).upper()
    low = (reply or "").lower()
    if re.search(r"\b(fertig|erledigt|abgeschlossen|done|abgehakt)\b", low):
        return "DONE"
    if re.search(r"\b(brauche|braucht|soll ich|welche[rs]?|bitte (bestätige|freigabe)|entscheide|unklar|freigabe)\b", low) or (reply or "").strip().endswith("?"):
        return "NEEDS_INPUT"
    return "CONTINUE"

def _tc_append(task_id, role, text):
    """Hängt eine Nachricht an den serverseitigen Task-Chat (gleicher KV-Key wie das Frontend liest)."""
    key = "bcc:chat:task-" + task_id
    hist = kv_get(key) or []
    hist.append({"role": role, "text": text})
    kv_set(key, hist)

FIRST_PROMPT = (
    "Arbeite den folgenden Task eigenständig und in mehreren Schritten ab — so weit du kommst.\n"
    "TASK: {title}\nPROJEKT-SLUG: {slug}\nZIEL/VISION: {vision}\n\n"
    "WICHTIG: Alle Dateien NUR in projects/{slug}/ schreiben — KEINEN neuen Ordner anlegen.\n"
    "Nutze deine Werkzeuge und die Projektdateien. Brauchst du Heiners Input/Freigabe oder bist du blockiert, "
    "frag KONKRET und stoppe — rate nicht.\n"
    "Audio-Dateien findest du in /tmp/bcc/audio/ (nicht im Workspace oder Shared).\n"
    "Beende JEDE Antwort mit GENAU EINER Zeile:\n"
    "STATUS: DONE        (Task vollständig erledigt)\n"
    "STATUS: CONTINUE    (du machst im nächsten Schritt weiter)\n"
    "STATUS: NEEDS_INPUT (du brauchst Heiner — mit konkreter Frage darüber)\n"
    "STATUS: BLOCKED     (Abhängigkeit/Blockade)")
CONT_PROMPT = ("Mach mit dem Task weiter. Wenn fertig: STATUS: DONE. "
               "Brauchst du Heiner: STATUS: NEEDS_INPUT (mit konkreter Frage). Sonst STATUS: CONTINUE.")

def run_task_loop(slug, task_id, max_turns=None):
    """Echter Mehrschritt-Loop: arbeitet bis DONE, bis Heiner gebraucht wird (-> blocked/rot),
    bis Stopp, oder bis zum Sicherheitsdeckel. Session-Kontinuität über die Task-Session."""
    max_turns = max_turns or MAX_TURNS
    _slug, task = find_task(task_id)
    slug = _slug or slug
    if not task:
        return "missing"
    with RUNLOCK:
        RUNNING.add(task_id); STOP_TASK.discard(task_id)
    _set_task_status(slug, task_id, "in_progress")
    _tc_append(task_id, "batty", "▶ Starte autonome Bearbeitung …")
    final = "blocked"
    try:
        for i in range(max_turns):
            with RUNLOCK:
                stop = task_id in STOP_TASK
            if stop:
                _set_task_status(slug, task_id, "pending")
                _tc_append(task_id, "batty", "⏹ Gestoppt.")
                final = "stopped"; break
            msg = (FIRST_PROMPT.format(title=task.get("title"), slug=slug, vision=task.get("vision") or "—")
                   if i == 0 else CONT_PROMPT)
            reply, _ = call_openclaw(msg, f"task-{task_id}", slug)
            _tc_append(task_id, "batty", reply)
            v = parse_status(reply)
            if v == "DONE":
                _set_task_status(slug, task_id, "done"); final = "done"; break
            if v in ("NEEDS_INPUT", "BLOCKED"):
                _set_task_status(slug, task_id, "blocked")  # rot = braucht Heiner
                final = "blocked"; break
            # CONTINUE -> nächste Runde
        else:
            _set_task_status(slug, task_id, "blocked")  # Deckel erreicht -> Bestätigung nötig (rot)
            _tc_append(task_id, "batty", "⏸ Mehrere Schritte gemacht — bitte schau drüber und gib frei. STATUS: NEEDS_INPUT")
            final = "blocked"
    finally:
        with RUNLOCK:
            RUNNING.discard(task_id); STOP_TASK.discard(task_id)
    return final

def submit_task_play(task_id):
    _slug, task = find_task(task_id)
    if not task:
        return {"error": "Task nicht gefunden."}
    threading.Thread(target=run_task_loop, args=(_slug, task_id), daemon=True).start()
    return {"runId": "t-" + task_id, "status": "running"}

def run_project_loop(slug):
    """Geht die offenen Tasks des Projekts durch, respektiert dependsOn,
    bis zu PLAY_CONCURRENCY parallel. Tasks, die Heiner brauchen, bleiben rot und werden übersprungen."""
    with RUNLOCK:
        STOP_PROJ.discard(slug)
    started = set()
    while True:
        with RUNLOCK:
            if slug in STOP_PROJ:
                break
        try:
            data = json.loads((Path(PROJECTS_DIR) / slug / "data.json").read_text())
        except Exception:
            break
        tasks = data.get("tasks", [])
        done_ids = {t["id"] for t in tasks if t.get("status") == "done"}
        open_tasks = [t for t in tasks if t.get("status") not in ("done", "idea", "blocked")]
        with RUNLOCK:
            running_here = [t for t in tasks if t["id"] in RUNNING]
        runnable = [t for t in open_tasks if t["id"] not in started and t["id"] not in RUNNING
                    and all(d in done_ids for d in (t.get("dependsOn") or []))]
        if not open_tasks and not running_here:
            break
        if not runnable and not running_here:
            break  # Rest hängt an Abhängigkeiten/Input -> Schluss
        with RUNLOCK:
            free = PLAY_CONCURRENCY - len([t for t in tasks if t["id"] in RUNNING])
        for t in runnable[:max(0, free)]:
            started.add(t["id"])
            threading.Thread(target=run_task_loop, args=(slug, t["id"]), daemon=True).start()
        time.sleep(2)
    with RUNLOCK:
        STOP_PROJ.discard(slug)

def submit_project_play(slug):
    if not (Path(PROJECTS_DIR) / slug / "data.json").exists():
        return {"error": "Projekt nicht gefunden."}
    threading.Thread(target=run_project_loop, args=(slug,), daemon=True).start()
    return {"runId": "p-" + slug, "status": "running"}

def task_stop(task_id):
    with RUNLOCK:
        STOP_TASK.add(task_id)
    if task_id not in RUNNING:
        slug, task = find_task(task_id)
        if task and task.get("status") == "in_progress":
            _set_task_status(slug, task_id, "pending")
    return True

def project_stop(slug):
    with RUNLOCK:
        STOP_PROJ.add(slug)
        for t in list(RUNNING):
            STOP_TASK.add(t)
    return True

# ---------------------------------------------------------------- Task anlegen (v6.1+)
def create_task(slug, title, vision=None, prio="yellow", status="today", who="batty",
                parent=None, depends_on=None, calendar_date=None):
    pdir = Path(PROJECTS_DIR) / slug
    if not pdir.exists():
        return {"error": "Projekt nicht gefunden."}
    tid = _stable_tid(slug, title)
    # 1) In TASKS.md anhängen (Source of Truth) — unter eine passende Sektion
    f = pdir / "TASKS.md"
    emo = _EMOJI_FOR.get(status, "")
    indent = "  " if parent else ""
    line = f"{indent}- [ ] {{{tid}}} {(emo+' ') if emo else ''}{title}" + (f" :: {vision}" if vision else "")
    if f.exists():
        txt = f.read_text().rstrip()
        f.write_text(txt + "\n" + line + "\n")
    else:
        f.write_text(f"# TASKS — {slug}\n\n## OFFEN\n{line}\n")
    # 2) Overlay (vision/prio/who) in data.json
    task = {"id": tid, "title": title, "status": status, "prio": prio, "who": who,
            "projectSlug": slug, "parentTaskId": parent, "dependsOn": depends_on or [],
            "vision": vision, "calendarDate": calendar_date}
    _write_data(slug, lambda p: p.setdefault("tasks", []).append(task))
    return task

# ---------------------------------------------------------------- System-Health (echte Box-Daten, F1)
def _cpu_pct():
    try:
        def snap():
            with open("/proc/stat") as f:
                p = f.readline().split()[1:]
            p = list(map(int, p)); idle = p[3] + (p[4] if len(p) > 4 else 0); return sum(p), idle
        t1, i1 = snap(); time.sleep(0.12); t2, i2 = snap()
        dt, di = t2 - t1, i2 - i1
        return round(100 * (dt - di) / dt, 1) if dt > 0 else 0.0
    except Exception:
        return None

def system_health():
    h = {"ts": int(time.time())}
    h["cpu"] = _cpu_pct()
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()[:3]; h["load"] = [float(x) for x in la]
    except Exception:
        h["load"] = None
    try:
        h["cores"] = os.cpu_count()
    except Exception:
        h["cores"] = None
    try:
        m = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":"); m[k.strip()] = int(v.strip().split()[0])  # kB
        tot, free = m["MemTotal"], m.get("MemAvailable", m["MemFree"])
        h["ram"] = {"usedPct": round(100 * (tot - free) / tot, 1),
                    "usedGB": round((tot - free) / 1048576, 1), "totalGB": round(tot / 1048576, 1)}
        st, sf = m.get("SwapTotal", 0), m.get("SwapFree", 0)
        h["swap"] = {"usedPct": round(100 * (st - sf) / st, 1) if st else 0,
                     "usedGB": round((st - sf) / 1048576, 1), "totalGB": round(st / 1048576, 1)}
    except Exception:
        h["ram"] = h["swap"] = None
    try:
        du = shutil.disk_usage("/")
        h["disk"] = {"usedPct": round(100 * du.used / du.total, 1),
                     "usedGB": round(du.used / 1e9), "totalGB": round(du.total / 1e9)}
    except Exception:
        h["disk"] = None
    h["temp"] = None
    for z in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            h["temp"] = round(int(open(z).read().strip()) / 1000, 1); break
        except Exception:
            pass
    try:
        h["uptime"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        h["uptime"] = None
    # Top-Prozesse (best effort)
    try:
        out = subprocess.run(["ps", "-eo", "comm,%cpu,%mem", "--sort=-%cpu"],
                             capture_output=True, text=True, timeout=3).stdout.splitlines()[1:6]
        h["top"] = [{"name": p[0][:18], "cpu": p[1], "mem": p[2]} for p in (l.split(None, 2) for l in out) if len(p) >= 3]
    except Exception:
        h["top"] = []
    # Ampel: schlechtester Wert
    level = "ok"
    if h.get("swap") and h["swap"]["usedPct"] > 70: level = "crit"
    if h.get("ram") and h["ram"]["usedPct"] > 85 and level != "crit": level = "warn"
    if h.get("temp") and h["temp"] > 75: level = "crit"
    if h.get("temp") and h["temp"] > 65 and level == "ok": level = "warn"
    if h.get("cores") and h.get("load") and h["load"][0] > h["cores"] * 1.5 and level == "ok": level = "warn"
    h["level"] = level
    # Parallel-Empfehlung aus freiem RAM (F5)
    try:
        free_gb = h["ram"]["totalGB"] - h["ram"]["usedGB"]
        h["maxParallel"] = max(1, int(free_gb // 0.8))
    except Exception:
        h["maxParallel"] = None
    # Token (V6.3.4): Live-Daten vom OpenClaw CLI (sessions list) — kein stale file mehr.
    # ZWEI-TIER: 1) token-status.json falls frisch (<15 Min)  2) CLI-Subprocess-Fallback
    h["tokens"] = None
    h["tokensStale"] = None
    _got_tokens = False
    # Tier 1: token-status.json (Schnellpfad, falls AlphaBatty/Cron-Job es aktuell hält)
    _tok_paths = [os.environ.get("BCC_TOKEN_FILE"),
                  str(Path.home() / "bcc" / "token-status.json"),
                  "/home/clawbox/bcc/token-status.json",
                  "/home/clawbox/clawbox/bcc/token-status.json"]
    for tp in _tok_paths:
        if not tp: continue
        try:
            p = Path(tp)
            if not p.exists(): continue
            data = json.loads(p.read_text())
            age = int(time.time()) - int(data.get("updatedAt") or 0)
            data["ageSec"] = age
            data["stale"] = age > 900
            if not data["stale"]:
                h["tokens"] = data
                h["tokensStale"] = False
                _got_tokens = True
            break
        except Exception as e:
            bcc_log(f"token-status read failed ({tp}): {e}", "system_health")
    # Tier 2: Live-Subprocess — openclaw sessions list (kein Stale-Risiko)
    if not _got_tokens:
        try:
            _oc_bin = os.environ.get("BCC_OPENCLAW_BIN", OPENCLAW_BIN)
            _oc_cwd = os.environ.get("BCC_OPENCLAW_CWD", OPENCLAW_CWD)
            result = subprocess.run(
                [_oc_bin, "sessions", "list", "--agent", "main", "--json", "--limit", "all"],
                capture_output=True, text=True, timeout=10, cwd=_oc_cwd
            )
            if result.returncode == 0 and result.stdout.strip():
                sl = json.loads(result.stdout)
                sessions = sl.get("sessions", [])
                if sessions:
                    total_in = sum(s.get("inputTokens", 0) or 0 for s in sessions)
                    total_out = sum(s.get("outputTokens", 0) or 0 for s in sessions)
                    ctx_max = sessions[0].get("contextTokens", 1_000_000) or 1_000_000
                    # Kontext-Usage: main-Session totalTokens als Proxy
                    main_s = next((s for s in sessions if s.get("key") == "agent:main:main"), None)
                    ctx_used = main_s.get("totalTokens", 0) or 0 if main_s else (sessions[0].get("totalTokens", 0) or 0)
                    ctx_pct = round(100 * ctx_used / ctx_max) if ctx_max else 0
                    h["tokens"] = {
                        "tokensIn": total_in,
                        "tokensOut": total_out,
                        "contextUsed": ctx_used,
                        "contextMax": ctx_max,
                        "contextPct": ctx_pct,
                        "cacheHitPct": 0,
                        "cost": 0,
                        "updatedAt": int(time.time()),
                        "ageSec": 0,
                        "stale": False,
                    }
                    h["tokensStale"] = False
        except Exception as e:
            bcc_log(f"token CLI fallback failed: {e}", "system_health")
    return h

# ---------------------------------------------------------------- Refresh-Hook (E1) + Error-Log (G1)
def refresh_status(slug=None):
    # Echte STATUS.md-Regeneration aus sessions_list ist Agenten-Arbeit.
    # Hier: data.json frisch einlesen (touch) + Zeitstempel zurück.
    n = 0
    for pdir in Path(PROJECTS_DIR).iterdir():
        if pdir.is_dir() and (pdir / "data.json").exists() and (slug is None or pdir.name == slug):
            n += 1
    return {"ok": True, "projects": n, "ts": int(time.time()),
            "note": "data.json neu eingelesen. STATUS.md-Regeneration aus sessions_list = Agenten-Hook."}

def bcc_log(message, where="proxy", project_slug=None):
    """V6.4.7: Schreibt Fehler nach stderr UND als Task ins bcc-debugging Projekt."""
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    prefix = f"[bcc {ts}]"
    if where:
        prefix += f" [{where}]"
    if project_slug:
        prefix += f" [{project_slug}]"
    sys.stderr.write(f"{prefix} {message}\n")
    sys.stderr.flush()
    # Als Task ins debugging-Projekt (best effort, NIE crashen)
    try:
        _error_to_task(message, 1, where, is_new=True)
    except Exception:
        pass

def _error_to_task(msg, count, where, is_new):
    """V6.4.7: Schreibt/aktualisiert Fehler-Tasks in bcc-debugging/TASKS.md.
    Neue Fehler -> neuer Task mit {err-XXXXX}. Bekannte -> Count im Titel."""
    try:
        slug = ensure_project("bcc-debugging", "BCC Debugging")
        f = Path(PROJECTS_DIR) / slug / "TASKS.md"
        txt = f.read_text() if f.exists() else "# TASKS — BCC Debugging\n"
        import hashlib as _hl
        tag_id = f"err-{_hl.md5(msg.encode()).hexdigest()[:6]}"
        # Pruefen ob dieser Fehler-Task schon existiert
        if tag_id in txt:
            if not is_new:
                # Count im Titel updaten (regex: erkennt (n×...))
                import re as _re
                lines = []
                for l in txt.split('\n'):
                    m = _re.match(r'(\s*[-*]\s*\[[ xX~\-]\].*\{' + _re.escape(tag_id) + r'\}\s*)(.+)', l)
                    if m:
                        rest = m.group(2)
                        m2 = _re.match(r'(.*?)\(\d+×.*?\)(.*)', rest)
                        if m2:
                            l = m.group(1) + m2.group(1) + f"({count}×{m2.group(2)}"
                        else:
                            l = l
                    lines.append(l)
                f.write_text('\n'.join(lines))
            return
        # Neuer Task: vor <!-- ERRORS_WILL_BE_APPENDED_HERE --> einfuegen
        marker = "<!-- ERRORS_WILL_BE_APPENDED_HERE -->"
        w = f", {where}" if where else ""
        task_line = f"- [ ] 🔴 {{{tag_id}}} {msg} ({count}×{w})"
        if marker in txt:
            txt = txt.replace(marker, task_line + "\n" + marker)
        else:
            txt = txt.rstrip() + "\n" + marker + "\n"
            txt = txt.replace(marker, task_line + "\n" + marker)
        f.write_text(txt)
    except Exception as e:
        sys.stderr.write(f"[bcc] _error_to_task failed: {e}\n")  # KEIN bcc_log — Rekursion vermeiden

def log_error(payload):
    key = "bcc:errors"
    log = kv_get(key) or []
    msg = (payload.get("message") or "")[:300]
    now = int(time.time())
    where = payload.get("where", "")
    for e in log:
        if e.get("message") == msg and now - e.get("ts", 0) < 3600:
            e["count"] = e.get("count", 1) + 1; e["ts"] = now
            kv_set(key, log[-100:])
            _error_to_task(msg, e["count"], where, is_new=False)
            return {"ok": True, "deduped": True}
    log.append({"message": msg, "stack": (payload.get("stack") or "")[:600],
                "where": where, "ts": now, "count": 1})
    kv_set(key, log[-100:])
    _error_to_task(msg, 1, where, is_new=True)
    return {"ok": True}

# ---------------------------------------------------------------- Multipart
def parse_multipart(content_type, body):
    msg = BytesParser(policy=email_default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields, files = {}, []
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append({"field": name, "data": payload,
                          "ctype": part.get_content_type(), "filename": filename})
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files

def save_uploads(files, project_slug):
    base = (Path(PROJECTS_DIR) / project_slug / "uploads") if project_slug \
           else Path(QUEUE_DIR) / "uploads"
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f["filename"] or "datei")
        dest = base / f"{uuid.uuid4().hex[:6]}-{safe}"
        dest.write_bytes(f["data"])
        paths.append(str(dest))
    return paths

# ---------------------------------------------------------------- HTTP
STATIC_FILES = {
    "/manifest.json": "application/json",
    "/service-worker.js": "application/javascript",
    "/icon-180.png": "image/png", "/icon-192.png": "image/png",
    "/icon-512.png": "image/png", "/icon-512-maskable.png": "image/png",
    "/marketing-dashboard.html": "text/html; charset=utf-8",
    "/data.js": "application/javascript",
    "/chart.umd.js": "application/javascript",
}

class BCCProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def handle_one_request(self):
        # V6.3.3: Client-Abbrüche (Browser schließt Long-Poll) NICHT als Crash loggen.
        try:
            return BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
            return

    def _headers(self, code=200, ctype="application/json", length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _serve_project_file(self, slug, rel):
        # V6.4.1: Datei aus Projektordner sicher ausliefern (kein Verzeichnis-Ausbruch).
        if not slug or not rel:
            return self._json({"error": "slug+rel nötig"}, 400)
        try:
            base = (Path(PROJECTS_DIR) / slug).resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base) + os.sep):
                return self._json({"error": "verboten"}, 403)
            if not target.is_file():
                return self._json({"error": "not found"}, 404)
            import mimetypes
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._file(str(target), ctype)
        except Exception as e:
            return self._json({"error": str(e)[:120]}, 500)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self._headers(code, "application/json", len(data))
        self.wfile.write(data)

    def _file(self, filepath, ctype):
        try:
            data = Path(filepath).read_bytes()
            self._headers(200, ctype, len(data))
            self.wfile.write(data)
        except Exception:
            self._json({"error": "not found"}, 404)

    # --- V6.7: Auth Gate -------------------------------------------------
    def _check_auth(self):
        """Returns True if request is authorized (or auth is disabled)."""
        if not BCC_AUTH_TOKEN:
            return True
        # skip auth for public paths
        path = self.path.split("?")[0]
        for prefix in AUTH_SKIP_PREFIXES:
            if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
                return True
        # check cookie
        cookie_hdr = self.headers.get("Cookie", "")
        for cookie in cookie_hdr.split(";"):
            if cookie.strip().startswith("bcc_auth="):
                if cookie.strip().split("=", 1)[1].strip() == BCC_AUTH_TOKEN:
                    return True
        # check X-BCC-Token header
        if self.headers.get("X-BCC-Token", "") == BCC_AUTH_TOKEN:
            return True
        # check ?key=... parameter
        qp = parse_qs(self.path.split("?")[1] if "?" in self.path else "")
        if qp.get("key", [None])[0] == BCC_AUTH_TOKEN:
            # set cookie and redirect without key
            self.send_response(302)
            self.send_header("Location", path)
            self.send_header("Set-Cookie",
                             f"bcc_auth={BCC_AUTH_TOKEN}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=31536000")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return False  # handled, but not authorized in this request (redirect)
        return False

    def _auth_locked_page(self):
        """Simple HTML page for unauthorized requests."""
        html = """<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BCC · Locked</title>
<style>body{background:#14141f;color:#e0e0e0;font-family:system-ui;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}
div{text-align:center;max-width:420px;padding:32px}
h1{color:#f97316;font-size:1.6rem;margin:0 0 8px}
p{color:#8b8fa3;line-height:1.5}
code{background:#1e1e2e;padding:2px 8px;border-radius:4px;font-size:.85rem}</style></head>
<body><div><h1>🔒 Batty Command Center</h1><p>Zugriff nur mit gültigem Access-Key.<br>
<code>?key=●●●</code> an die URL anhängen.</p></div></body></html>"""
        return self._file_str(html, "text/html; charset=utf-8", 401)

    def _file_str(self, content, ctype, code=200):
        data = content.encode("utf-8")
        self._headers(code, ctype, len(data))
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._headers(204, "text/plain", 0)

    def do_GET(self):
        if not self._check_auth():
            return self._auth_locked_page()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            return self._file(f"{STATIC_DIR}/index.html", "text/html; charset=utf-8")
        if path in STATIC_FILES:
            return self._file(f"{STATIC_DIR}{path}", STATIC_FILES[path])
        if path in ("/projects", "/setup-api/projects"):
            return self._json(read_projects())
        if path in ("/chat-result", "/setup-api/chat-result"):
            return self._json(get_result((q.get("msgId") or [""])[0]))
        if path in ("/kv", "/setup-api/kv"):
            return self._json({"value": kv_get((q.get("key") or [""])[0])})
        if path in ("/system-health", "/setup-api/system-health"):
            return self._json(system_health())
        if path in ("/errors", "/setup-api/errors"):
            return self._json(kv_get("bcc:errors") or [])
        if path in ("/cron", "/setup-api/cron"):
            return self._json(cron_list())
        if path in ("/project-files", "/setup-api/project-files"):
            slug = (q.get("slug") or [""])[0]
            return self._json(project_files(slug))
        if path in ("/project-file", "/setup-api/project-file"):
            return self._serve_project_file((q.get("slug") or [""])[0], (q.get("rel") or [""])[0])
        if path in ("/whereami", "/setup-api/whereami"):
            root = Path(PROJECTS_DIR)
            projs = [d.name for d in sorted(root.iterdir()) if d.is_dir()] if root.exists() else []
            return self._json({
                "version": BCC_VERSION, "build": BCC_BUILD, "agent": AGENT,
                "projectsDir": PROJECTS_DIR, "projectsDirAutoResolved": PROJECTS_DIR_RESOLVED,
                "projectsDirExists": root.exists(), "projectCount": len(projs),
                "projects": projs[:50],
                "staticDir": STATIC_DIR, "staticDirAutoResolved": STATIC_DIR_RESOLVED,
                "staticIndexExists": Path(f"{STATIC_DIR}/index.html").exists(),
                "openclawBin": OPENCLAW_BIN, "openclawBinExists": Path(OPENCLAW_BIN).exists(),
                "kvFile": str(KV_FILE)})
        if path == "/health":
            return self._json({"ok": True, "agent": AGENT, "version": BCC_VERSION,
                               "pending": sum(1 for v in RESULTS.values() if v.get("status") == "pending")})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._check_auth():
            return self._auth_locked_page()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if path in ("/chat", "/setup-api/chat"):
            try:
                d = json.loads(body or b"{}")
                mid = submit_chat(d.get("message", ""), d.get("sessionId", "main"), d.get("projectSlug"))
                return self._json({"msgId": mid, "status": "pending"})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/chat-files", "/setup-api/chat-files"):
            if "multipart/form-data" not in ctype:
                return self._json({"error": "multipart erwartet"}, 400)
            fields, files = parse_multipart(ctype, body)
            slug = fields.get("projectSlug") or None
            paths = save_uploads(files, slug)
            note = ("Angehängte Dateien (im Projektordner gespeichert):\n" +
                    "\n".join(f"- {p}" for p in paths)) if paths else None
            mid = submit_chat(fields.get("message", ""), fields.get("sessionId", "main"), slug, note)
            return self._json({"msgId": mid, "status": "pending", "saved": paths})

        if path in ("/chat-audio", "/setup-api/chat-audio"):
            if "multipart/form-data" not in ctype:
                return self._json({"error": "multipart erwartet"}, 400)
            fields, files = parse_multipart(ctype, body)
            audio_file = next((f for f in files if f["field"] == "audio"), files[0] if files else None)
            if not audio_file:
                return self._json({"error": "kein audio"}, 400)
            ext = "mp4" if ("mp4" in audio_file["ctype"] or "aac" in audio_file["ctype"]) else "webm"
            apath = f"{AUDIO_DIR}/{uuid.uuid4().hex[:10]}.{ext}"
            Path(apath).write_bytes(audio_file["data"])
            mid = submit_audio(apath, audio_file["ctype"], fields.get("sessionId", "main"), fields.get("projectSlug"))
            # V6.6.3: Audio-Pfad im KV speichern, damit Agenten die Datei finden können
            session_id = fields.get("sessionId", "main")
            try:
                import subprocess as _sp
                dur = ""
                try:
                    r = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", apath], capture_output=True, text=True, timeout=5)
                    secs = float(r.stdout.strip())
                    mins = int(secs // 60); ds = int(secs % 60)
                    dur = f"{mins}:{ds:02d}"
                except Exception:
                    pass
                key = f"bcc:chat:{session_id}"
                hist = kv_get(key) or []
                hist.append({"audio": True, "role": "me", "file": apath, "dur": dur,
                             "text": "(Audio gesendet)", "ts": int(time.time())})
                kv_set(key, hist[-200:])
            except Exception as e:
                bcc_log(f"audio KV write failed: {e}", "chat")
            return self._json({"msgId": mid, "status": "pending", "audioPath": apath, "duration": dur})

        if path in ("/task", "/setup-api/task"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": mutate_task(d.get("id"), d)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project", "/setup-api/project"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": mutate_project(d.get("slug"), d)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/task-create", "/setup-api/task-create"):
            try:
                d = json.loads(body or b"{}")
                if not d.get("slug") or not d.get("title"):
                    return self._json({"error": "slug/title fehlt"}, 400)
                return self._json(create_task(d["slug"], d["title"], d.get("vision"), d.get("prio", "yellow"),
                                              d.get("status", "today"), d.get("who", "batty"),
                                              d.get("parentTaskId"), d.get("dependsOn"), d.get("calendarDate")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/refresh", "/setup-api/refresh"):
            try:
                d = json.loads(body or b"{}")
                return self._json(refresh_status(d.get("slug")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/log-error", "/setup-api/log-error"):
            try:
                return self._json(log_error(json.loads(body or b"{}")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project-delete", "/setup-api/project-delete"):
            try:
                d = json.loads(body or b"{}")
                if not d.get("slug"):
                    return self._json({"error": "slug fehlt"}, 400)
                return self._json(delete_project(d["slug"]))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project-create", "/setup-api/project-create"):
            try:
                d = json.loads(body or b"{}")
                if not d.get("name"):
                    return self._json({"error": "name fehlt"}, 400)
                return self._json(create_project(d["name"], d.get("vision", ""), d.get("prio"),
                                                 d.get("workspaces"), d.get("city"), d.get("tags"),
                                                 parent_folder=d.get("parentFolder")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/kv", "/setup-api/kv"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": kv_set(d.get("key"), d.get("value"))})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/task-move", "/setup-api/task-move"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": task_move(d.get("id"), d.get("toSlug"))})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project-merge", "/setup-api/project-merge"):
            try:
                d = json.loads(body or b"{}")
                return self._json(merge_project(d.get("from"), d.get("to")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/cron-save", "/setup-api/cron-save"):
            try:
                d = json.loads(body or b"{}")
                return self._json(cron_save(d))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/cron-delete", "/setup-api/cron-delete"):
            try:
                d = json.loads(body or b"{}")
                return self._json(cron_delete(d.get("id")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/task-play", "/setup-api/task-play"):
            try:
                d = json.loads(body or b"{}")
                return self._json(submit_task_play(d.get("id")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/task-stop", "/setup-api/task-stop"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": task_stop(d.get("id"))})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project-play", "/setup-api/project-play"):
            try:
                d = json.loads(body or b"{}")
                return self._json(submit_project_play(d.get("slug")))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/project-stop", "/setup-api/project-stop"):
            try:
                d = json.loads(body or b"{}")
                return self._json({"ok": project_stop(d.get("slug"))})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    print(f"BCC Proxy v{BCC_VERSION} ({BCC_BUILD}) :{PORT} | agent={AGENT} thinking={THINKING}")
    print(f"  PROJECTS_DIR = {PROJECTS_DIR}  (auto={PROJECTS_DIR_RESOLVED}, exists={Path(PROJECTS_DIR).exists()})")
    print(f"  STATIC_DIR   = {STATIC_DIR}  (auto={STATIC_DIR_RESOLVED}, index={Path(STATIC_DIR+'/index.html').exists()})")
    print(f"  OPENCLAW_BIN = {OPENCLAW_BIN}  (exists={Path(OPENCLAW_BIN).exists()})")
    ThreadingHTTPServer(("0.0.0.0", PORT), BCCProxy).serve_forever()
