#!/usr/bin/env python3
"""Codex CLI sessions for the panel (prototype, 25.11; not wired yet).

A Codex session is driven the same way as a Claude one: an interactive TUI in a
`script` pty, its stdin a FIFO the wrapper holds open, its screen in the pty
log. What differs, all checked against codex-cli 0.164.0 on the Cyprus mini:

- ⛔ The pty must get a size. A detached `script` pty is 0x0: Claude Code draws
  anyway, Codex runs (it even reports state=idle) but renders nothing, so the
  log stays at ~1.5 KB of escape codes and looks hung. launch_cmd() wraps the
  binary in `bash -c 'stty rows R cols C; exec codex …'`, so `ps` still shows
  the codex argv.
- ⛔ --no-daemon. Without it the TUI hands the thread to a shared
  `codex app-server` daemon (a child of whichever TUI started first, it
  survives it): that daemon holds the rollout and runs the tools, so killing
  the session's process group does not stop the work and one process is not
  one session. With --no-daemon the TUI pid itself holds the rollout open
  (lsof maps pid -> session id).
- No approval prompts: --dangerously-bypass-approvals-and-sandbox (the TUI
  says «permissions: YOLO mode»). No trust question in a non-git folder.
- Effort: -c model_reasoning_effort=high (rollout turn_context shows
  "effort":"high"). Model: -m <id>; the ChatGPT Plus default is gpt-6.1-terra.
- Typing: the panel's _type_into works unchanged: DEL x N to clear, the text
  (multiline as a bracketed paste ESC[200~ … ESC[201~), then "\r". A message
  typed while a turn runs is steered into that turn (recorded as a user
  message at once), not queued.
- Rollout: ~/.codex/sessions/YYYY/MM/DD/rollout-<local launch time>-<id>.jsonl,
  created at the FIRST prompt (like the DeepSeek case: type first, then look
  for it). First line session_meta {id, timestamp (UTC launch time), cwd}.
  `codex resume <id>` appends to the same file.
- Busy/idle: Codex writes OSC 7501 `state=idle|working|done:app=codex` into
  the pty on every change. The last one in the log is the TUI's own status.
  The rollout agrees: event_msg task_started … task_complete/turn_aborted.
- Resume: `codex resume <id> --no-daemon …` opens straight into the thread
  with its history drawn, no dialog to answer (unlike Claude's resume menu).
- Limits: every event_msg token_count carries rate_limits {primary: 5 h
  window, secondary: weekly, used_percent, resets_at, plan_type} for free.
- Titles: ~/.codex/session_index.jsonl {id, thread_name} (Codex names threads).
"""

import glob
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
SESSIONS = os.path.join(CODEX_HOME, "sessions")
INDEX = os.path.join(CODEX_HOME, "session_index.jsonl")
ROWS, COLS = 50, 160
EFFORTS = ("low", "medium", "high", "xhigh")
OSC_STATE = re.compile(rb"\x1b\]7501;state=([a-z_]+)")
ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# user messages Codex injects itself; not something the person typed
INJECTED = ("<environment_context>", "<user_instructions>", "<permissions",
            "<skills_instructions>", "<turn_aborted>", "<multi_agent")


def codex_bin():
    """RC_CODEX, else the newest bundled CLI, else the app's, else PATH."""
    env = os.environ.get("RC_CODEX")
    if env and os.access(env, os.X_OK):
        return env
    rel = sorted(glob.glob(os.path.join(CODEX_HOME, "packages/app-server-daemon/releases/*/bin/codex")),
                 key=_ver_key)
    for c in reversed(rel):
        if os.access(c, os.X_OK):
            return c
    for c in ("/Applications/Codex.app/Contents/Resources/codex",
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex", os.path.join(HOME, ".local/bin/codex")):
        if os.access(c, os.X_OK):
            return c
    import shutil
    return shutil.which("codex") or ""


def _ver_key(path):
    m = re.search(r"/releases/(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def launch_cmd(model=None, effort=None, resume_id=None, rows=ROWS, cols=COLS):
    """The command for rc_guardian.script_cmd(logf, argv): one shell word list
    as a string. resume_id reopens a thread by id."""
    argv = [codex_bin()]
    if resume_id:
        argv += ["resume", str(resume_id)]
    argv += ["--no-daemon", "--dangerously-bypass-approvals-and-sandbox",
             "-c", "check_for_update_on_startup=false",
             # the user config's notify hook launches the Computer Use client
             # after every turn; a panel session has no use for it
             "-c", "notify=[]",
             "-c", "model_reasoning_effort=%s" % (effort if effort in EFFORTS else "high")]
    if model:
        argv += ["-m", str(model)]
    inner = "stty rows %d cols %d; exec %s" % (rows, cols, " ".join(shlex.quote(a) for a in argv))
    return "/bin/bash -c %s" % shlex.quote(inner)


def rollout_id(path):
    m = ID_RE.search(path or "")
    return m.group(1) if m else ""


def _meta(path):
    try:
        with open(path, errors="ignore") as fh:
            o = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    return (o.get("payload") or {}) if o.get("type") == "session_meta" else {}


def _epoch(ts):
    if not isinstance(ts, str) or len(ts) < 19:
        return 0.0
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def rollout_of_pid(pid):
    """(path, id) of the rollout a --no-daemon TUI holds open, or ("", "")."""
    try:
        out = subprocess.run(["lsof", "-p", str(int(pid)), "-Fn"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return "", ""
    for line in out.splitlines():
        if line.startswith("n") and "/rollout-" in line and line.endswith(".jsonl"):
            return line[1:], rollout_id(line)
    return "", ""


def find_rollout(started_after_ts, cwd, pid=None):
    """(path, id) of the thread a session launched at started_after_ts in cwd
    writes. Exists only after the first prompt. pid (the codex TUI) is exact;
    without it: the newest rollout of the last two days whose session_meta
    names this cwd and a launch time at or after started_after_ts."""
    if pid:
        got = rollout_of_pid(pid)
        if got[0]:
            return got
    want = os.path.realpath(cwd)
    best = ("", "", 0.0)
    for day in (0, 1):
        d = time.localtime(time.time() - day * 86400)
        for path in glob.glob(os.path.join(SESSIONS, "%04d/%02d/%02d/rollout-*.jsonl"
                                           % (d.tm_year, d.tm_mon, d.tm_mday))):
            try:
                if os.path.getmtime(path) < started_after_ts - 5:
                    continue
            except OSError:
                continue
            m = _meta(path)
            t = _epoch(m.get("timestamp"))
            if t < started_after_ts - 5 or os.path.realpath(m.get("cwd") or "") != want:
                continue
            if t > best[2]:
                best = (path, m.get("id") or rollout_id(path), t)
    return best[0], best[1]


def path_for_id(thread_id):
    hits = glob.glob(os.path.join(SESSIONS, "*/*/*/rollout-*-%s.jsonl" % thread_id))
    return hits[0] if hits else ""


def _local(ts):
    t = _epoch(ts)
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def _texts(content):
    return "\n".join(c.get("text") or "" for c in (content or [])
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text", "Text")).strip()


def _step(item):
    """A tool step from an item_completed item: (tool, text, path)."""
    t = item.get("type")
    if t == "CommandExecution":
        cmd = item.get("command") or []
        if isinstance(cmd, list):
            cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(cmd)
        return "Bash", str(cmd)[:300], ""
    if t == "FileChange":
        paths = list((item.get("changes") or {}).keys())
        return "Edit", ", ".join(os.path.basename(p) for p in paths)[:300], paths[0] if paths else ""
    if t == "McpToolCall":
        return "%s.%s" % (item.get("server") or "mcp", item.get("tool") or "?"), json.dumps(item.get("arguments"), ensure_ascii=False)[:200], ""
    if t == "Extension":
        return item.get("kind") or "extension", str(item.get("query") or "")[:200], ""
    if t == "ImageView":
        p = str(item.get("path") or "").replace("file://", "")
        return "View", os.path.basename(p), p
    if t == "SubAgentActivity":
        return "Agent", "%s %s" % (item.get("kind") or "", item.get("agent_path") or ""), ""
    if t == "ContextCompaction":
        return "Compact", "context compacted", ""
    return None


def rollout_rows(path):
    """The thread as the panel's feed rows: {role: user|assistant|step|stop,
    ts (local), tse (epoch), text[, tool, path]}. Developer prompts and the
    environment blocks Codex injects as user messages are left out."""
    rows = []
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return rows
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload")
            if not isinstance(p, dict):
                continue
            ts = o.get("timestamp")
            base = {"ts": _local(ts), "tse": _epoch(ts)}
            kind, pt = o.get("type"), p.get("type")
            if kind == "response_item" and pt == "message" and p.get("role") in ("user", "assistant"):
                txt = _texts(p.get("content"))
                if not txt or (p["role"] == "user" and txt.lstrip().startswith(INJECTED)):
                    continue
                rows.append(dict(base, role=p["role"], text=txt))
            elif kind == "event_msg" and pt == "item_completed":
                st = _step(p.get("item") or {})
                if st:
                    rows.append(dict(base, role="step", tool=st[0], text=st[1], path=st[2]))
            elif kind == "event_msg" and pt == "turn_aborted":
                rows.append(dict(base, role="stop", text="stopped"))
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


def turn_open(rollout):
    """True while the rollout's last turn has started and not ended."""
    last = ""
    for line in _tail(rollout, 262144).splitlines():
        if b'"task_started"' in line:
            last = "start"
        elif b'"task_complete"' in line or b'"turn_aborted"' in line:
            last = "end"
    return last == "start"


def is_busy(logf, rollout=None):
    """The TUI's own state when its log has one; else the rollout's turn."""
    st = tui_state(logf) if logf else ""
    if st:
        return st == "working"
    return turn_open(rollout) if rollout else False


def limits(rollout):
    """rate_limits of the last token_count in the rollout: {five_h, week (used %),
    five_h_resets, week_resets (epoch), plan} or {}."""
    for line in reversed(_tail(rollout, 524288).splitlines()):
        if b'"rate_limits"' not in line:
            continue
        try:
            rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
        except ValueError:
            continue
        pr, se = rl.get("primary") or {}, rl.get("secondary") or {}
        return {"five_h": pr.get("used_percent"), "five_h_resets": pr.get("resets_at"),
                "week": se.get("used_percent"), "week_resets": se.get("resets_at"),
                "plan": rl.get("plan_type")}
    return {}


def title(thread_id):
    """Codex's own name for the thread (session_index.jsonl), or ''."""
    name = ""
    try:
        with open(INDEX, errors="ignore") as fh:
            for line in fh:
                if thread_id in line:
                    try:
                        name = json.loads(line).get("thread_name") or name
                    except ValueError:
                        pass
    except OSError:
        pass
    return name


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        p = sys.argv[1] if sys.argv[1].endswith(".jsonl") else path_for_id(sys.argv[1])
        for r in rollout_rows(p):
            print(r["ts"][11:], r["role"], (r.get("tool") or ""), r["text"][:100].replace("\n", " / "))
        print("busy(rollout):", turn_open(p), "limits:", limits(p), "title:", title(rollout_id(p)))
    else:
        print(launch_cmd())
