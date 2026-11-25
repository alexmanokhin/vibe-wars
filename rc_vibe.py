#!/usr/bin/env python3
"""Mistral Vibe CLI sessions for the panel (prototype, 25.11).

A Vibe session is `vibe` (the launcher execs the Rust TUI `vibe-rs`, which
starts one `vibe-app-server` child) in the same wrapper as a Claude session:
`script -q -F LOG <cmd>` with a FIFO on stdin. The panel gives it its own
VIBE_HOME, so the user's ~/.vibe is never touched:

  <home>/.env         MISTRAL_API_KEY=<key>  (mode 600; a Vibe-plan key bills
                      the $300 vibe_budget, a plain API key the $30 api_budget)
  <home>/config.toml  active_model = the DeepSeek model Mistral serves,
                      reasoning high, auto-approve, no update checks
  <home>/logs/session/unified/<id>/   the session store (see session_rows)
  <home>/logs/session/active/<id>.lock.json  {"process_id": <app-server pid>,
                      "session_id": <id>} written AT LAUNCH, before any prompt

Checked 25.11 with vibe 3.1.0 on a Vibe plan key:
- `--trust --auto-approve`: no trust question, no approval prompts.
- Typing like Claude works as is: plain text + Enter 0.2 s later submits; a
  multiline bracketed paste works too; a message sent mid-turn becomes the
  next turn. (No paste-burst trap like Codex.)
- The pty says its state: ESC]7501;state=idle|working|done:app=vibe.
- `vibe --resume <id>` opens the thread straight away, no dialog; history and
  context are kept and new turns go into the same store.
- Killing the wrapper's process group stops vibe-rs and its app-server.
"""

import glob
import json
import os
import re
import shlex
import time

HOME = os.path.expanduser("~")
ROWS, COLS = 50, 160
EFFORTS = ("off", "high")
OSC_STATE = re.compile(rb"\x1b\]7501;state=([a-z_]+)")
ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# The DeepSeek model Mistral serves to Vibe plans (the only deepseek id on
# /v1/models 25.11). It takes reasoning off/high; we use high.
MODELS = {
    "deepseek": {"name": "deepseek-v4-1-flash-1017", "display": "DeepSeek V4.1 Flash",
                 "thinking": "high", "context": 131072},
    "mistral": {"name": "mistral-vibe-cli-latest", "display": "Mistral Medium 3.5",
                "thinking": "high", "context": 262144},
}
DEFAULT_MODEL = "deepseek"

CONFIG = """# written by the panel (rc_vibe); the user's own ~/.vibe is not used
active_model = "{alias}"
default_agent = "auto-approve"
enable_update_checks = false
enable_auto_update = false
enable_notifications = false
show_greeting = false
ask_confirmation_on_exit = false
enable_mouse_capture = false
disable_welcome_banner_animation = true
cursor_blink = false
autocopy_to_clipboard = false
{models}"""

MODEL_BLOCK = """
[[models]]
name = "{name}"
provider = "mistral"
alias = "{alias}"
display_name = "{display}"
thinking = "{thinking}"
thinking_levels = ["off", "high"]
max_context_length = {context}
"""


def vibe_bin():
    for p in (os.path.join(HOME, ".local/bin/vibe"), "/opt/homebrew/bin/vibe", "/usr/local/bin/vibe"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    import shutil
    return shutil.which("vibe") or ""


def prepare_home(home, key, model=DEFAULT_MODEL):
    """Write <home>/.env and <home>/config.toml (both 600). Safe to call on
    every launch: a changed key or model takes effect on the next one."""
    os.makedirs(home, mode=0o700, exist_ok=True)
    alias = model if model in MODELS else DEFAULT_MODEL
    blocks = "".join(MODEL_BLOCK.format(alias=a, **m) for a, m in MODELS.items())
    for name, body in ((".env", "MISTRAL_API_KEY=%s\n" % key),
                       ("config.toml", CONFIG.format(alias=alias, models=blocks))):
        path = os.path.join(home, name)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
    return home


def launch_cmd(model=None, effort=None, resume_id=None, home=None, rows=ROWS, cols=COLS):
    """The command for rc_guardian.script_cmd(logf, argv), as one string.
    The model comes from <home>/config.toml (prepare_home): vibe's --model is
    headless-only. VIBE_HOME goes in front, so the key file is read from there."""
    argv = [vibe_bin()]
    if resume_id:
        argv += ["--resume", str(resume_id)]
    argv += ["--trust", "--auto-approve"]
    inner = "stty rows %d cols %d; exec %s" % (rows, cols, " ".join(shlex.quote(a) for a in argv))
    pre = ("VIBE_HOME=%s " % shlex.quote(home)) if home else ""
    return "/usr/bin/env %s/bin/bash -c %s" % (pre, shlex.quote(inner))


def _sessions_dir(home):
    return os.path.join(home, "logs", "session", "unified")


def path_for_id(session_id, home):
    d = os.path.join(_sessions_dir(home), session_id)
    return d if os.path.isdir(d) else ""


def _ppid(pid):
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def find_session(started_after_ts, cwd, home, pid=None):
    """(session dir, id) of the session a launch writes.

    pid (the vibe-rs process): exact, from active/<id>.lock.json, whose
    process_id is vibe-rs's app-server child; known right after launch.
    Without pid: the newest session whose meta names this cwd and a start at or
    after started_after_ts. meta.json appears only at the first prompt."""
    act = os.path.join(home, "logs", "session", "active")
    if pid:
        for f in glob.glob(os.path.join(act, "*.lock.json")):
            try:
                o = json.load(open(f))
            except (OSError, ValueError):
                continue
            ap = o.get("process_id")
            if ap and _ppid(ap) == int(pid):
                sid = o.get("session_id") or ""
                return os.path.join(_sessions_dir(home), sid), sid
    want = os.path.realpath(cwd)
    best = ("", "", 0.0)
    for d in glob.glob(os.path.join(_sessions_dir(home), "*")):
        try:
            m = json.load(open(os.path.join(d, "meta.json")))
        except (OSError, ValueError):
            continue
        t = _epoch(m.get("start_time"))
        wd = ((m.get("environment") or {}).get("working_directory") or "")
        if t < started_after_ts - 5 or os.path.realpath(wd) != want:
            continue
        if t > best[2]:
            best = (d, m.get("session_id") or os.path.basename(d), t)
    return best[0], best[1]


def _epoch(ts):
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _local(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000.0)) if ms else ""


def _entries(sdir):
    """(entries, turn status) of a session store: the projection of the current
    generation, then the journal records after it (append/replace/remove)."""
    try:
        cur = json.load(open(os.path.join(sdir, "CURRENT")))
        gen = os.path.join(sdir, "generations", cur["generation"])
        man = json.load(open(os.path.join(gen, "manifest.json")))
    except (OSError, ValueError, KeyError):
        return [], ""
    out = []
    for c in (man.get("projection_state") or {}).get("chunks") or []:
        try:
            out += json.load(open(os.path.join(sdir, "chunks", c + ".json")))
        except (OSError, ValueError):
            pass
    status = ""
    try:
        snap = json.load(open(os.path.join(gen, "projection-state.json"))).get("snapshot") or {}
        status = (snap.get("latestTurn") or {}).get("status") or ""
    except (OSError, ValueError):
        pass
    first = (man.get("recovery_journal_segment") or {}).get("first_sequence") or 0
    for f in sorted(glob.glob(os.path.join(sdir, "journal", "*.jsonl"))):
        try:
            fh = open(f, errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("sequence", 0) < first or o.get("type") != "projection_delta":
                    continue
                for d in (o.get("payload") or {}).get("delta") or []:
                    op = d.get("op")
                    if op == "append_entry":
                        out.append(d.get("entry") or {})
                    elif op == "replace_entry":
                        out = [d.get("entry") or e if e.get("id") == d.get("id") else e for e in out]
                    elif op == "remove_entry":
                        out = [e for e in out if e.get("id") != d.get("id")]
                    elif op == "set_envelope":
                        status = ((d.get("state") or {}).get("latestTurn") or {}).get("status") or status
    return out, status


def _text(content):
    return "\n".join(c.get("text") or "" for c in (content or [])
                     if isinstance(c, dict) and c.get("type") == "text").strip()


def session_rows(sdir):
    """The session as the panel's feed rows: {role: user|assistant|step|think,
    ts, tse, text[, tool, path]}."""
    rows = []
    for e in _entries(sdir)[0]:
        ms = e.get("createdAt") or 0
        base = {"ts": _local(ms), "tse": ms / 1000.0 if ms else None}
        t, role = e.get("type"), e.get("role")
        if t == "message" and role in ("user", "assistant"):
            txt = _text(e.get("content"))
            if txt:
                rows.append(dict(base, role=role, text=txt))
        elif t == "effect":
            det = e.get("detail") or {}
            inp = det.get("input") or {}
            label = inp.get("command") or inp.get("path") or inp.get("file_path") or \
                ((det.get("display") or {}).get("message") or "")
            rows.append(dict(base, role="step", tool=det.get("toolName") or "tool",
                             text=str(label)[:300], path=inp.get("path") or inp.get("file_path") or ""))
        elif t == "reasoning" and e.get("text"):
            rows.append(dict(base, role="think", text=e["text"][:4000]))
    return rows


def _tail(path, size=65536):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            n = fh.tell()
            fh.seek(max(0, n - size))
            return fh.read()
    except OSError:
        return b""


def tui_state(logf):
    """The last OSC 7501 state the TUI reported (idle, working, done) or ''."""
    m = OSC_STATE.findall(_tail(logf))
    return m[-1].decode() if m else ""


def is_busy(logf, sdir=None):
    st = tui_state(logf)
    if st:
        return st == "working"
    return bool(sdir) and _entries(sdir)[1] == "in_progress"


def limits(home):
    """Mistral budgets for the key in <home>/.env: {vibe: %, api: %, resets}.
    One cheap GET; a Vibe-plan key moves only `vibe`."""
    import urllib.request
    key = ""
    try:
        for line in open(os.path.join(home, ".env")):
            if line.startswith("MISTRAL_API_KEY="):
                key = line.split("=", 1)[1].strip()
    except OSError:
        return {}
    if not key:
        return {}
    req = urllib.request.Request("https://api.mistral.ai/v1/billing/budget",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception:
        return {}
    vb, ab = d.get("vibe_budget") or {}, d.get("api_budget") or {}
    return {"vibe": vb.get("usage_percentage"), "api": ab.get("usage_percentage"),
            "resets": vb.get("reset_at") or ab.get("reset_at")}


def title(sdir):
    try:
        return (json.load(open(os.path.join(sdir, "meta.json"))).get("title") or "").strip()
    except (OSError, ValueError):
        return ""


def session_model(sdir):
    """The model alias (see MODELS) a session runs, from its meta.json."""
    try:
        with open(os.path.join(sdir, "meta.json")) as fh:
            return ((json.load(fh).get("config") or {}).get("active_model") or "")
    except (OSError, ValueError):
        return ""
