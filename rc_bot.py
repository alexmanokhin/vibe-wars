#!/usr/bin/env python3
"""rc_bot — session control over Telegram, the contour that survives a dead VPN.

The panel needs inbound reachability (Tailscale). This needs none: it long-polls
api.telegram.org outbound, so it keeps working from behind hotel NAT, mobile
tethering, or a network where the tailnet is throttled.

Only the owner's chat (RC_BOT_CHAT) may command it. The bot can end sessions, so an unauthenticated
one would hand that to anyone who guesses the bot name.

Telegram allows exactly ONE getUpdates consumer per token: if anything else
polls the same token, both drop half the updates.

Configuration: TELEGRAM_BOT_TOKEN and RC_BOT_CHAT in <state dir>/telegram.env
(or the environment). Without them the bot exits quietly.
"""

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rc_config as CFG  # noqa: E402

TOKEN, OWNER_CHAT = CFG.telegram()
if not TOKEN:
    print("Telegram is not configured: put TELEGRAM_BOT_TOKEN and RC_BOT_CHAT in %s"
          % CFG.TELEGRAM_ENV)
    sys.exit(0)

import rc_control as C  # noqa: E402

POLL_TIMEOUT = 50
API = "https://api.telegram.org/bot%s/%s"
HOST = socket.gethostname().split(".")[0]
# Files sent to the bot land here, in a sub-folder named after the caption.
INBOX = os.path.expanduser(os.environ.get("RC_BOT_INBOX") or os.path.join(CFG.STATE_DIR, "inbox"))


def api(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API % (TOKEN, method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode()[:200]}
    except Exception as e:                      # network blip: caller retries
        return {"ok": False, "description": str(e)[:200]}


def send(chat, text, keyboard=None):
    return api("sendMessage", chat_id=chat, text=text[:4000],
               parse_mode="HTML", disable_web_page_preview=True,
               reply_markup={"inline_keyboard": keyboard} if keyboard else None)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------- views

def status_text(snap=None):
    m = (snap or C.snapshot())["meta"]
    over = "  (above the cap)" if m["live_total"] > m["cap"] else ""
    u = C.usage()
    wk = u.get("week")
    lim = ("weekly limit <b>%d%%</b>, resets %s\n" % (wk, u.get("week_resets", "?"))
           if wk is not None else "weekly limit: not available\n")
    return ("<b>%s</b>  %s\n"
            "%s"
            "live <b>%d</b> / %d%s\n"
            "  of them %d started in the app\n"
            "queued <b>%d</b>, retired %d\n"
            "load %.2f (gate %g), swap %d MB, %s"
            % (esc(HOST), m["ts"][11:16], lim, m["live_total"], m["cap"], over, m["live_app"],
               m["queue"], m["retired"], m["load1"], m["load_gate"],
               m["swap_mb"], _mem_line(m)))


def _mem_line(m):
    """Memory as Activity Monitor shows it, plus kernel pressure. A bare "free"
    figure on macOS always looks alarming: the file cache is not counted."""
    if m.get("used_mb") is None or not m.get("mem_mb"):
        return "free %d MB" % m["free_mb"]
    pr = {1: "normal", 2: "elevated", 4: "critical"}.get(m.get("pressure") or 1, "?")
    return "memory %.1f/%.0f GB, pressure %s" % (m["used_mb"] / 1024.0, m["mem_mb"] / 1024.0, pr)


def live_view():
    snap = C.snapshot()
    rows = [r for r in snap["rows"] if r["live"]]
    lines = [status_text(snap), "", "<b>Live sessions</b>"]
    kb = []
    for r in rows[:12]:
        lines.append("<code>%s</code> %s" % (r["short"], esc(r["name"][:58])))
        kb.append([{"text": "kill %s" % r["short"], "callback_data": "k:%s" % r["short"]}])
    for a in snap["app"][:8]:
        lines.append("<code>%s</code> app chat, pid %s" % (a["cse"][:14], a["pid"]))
        kb.append([{"text": "kill pid %s" % a["pid"], "callback_data": "a:%s" % a["pid"]}])
    if not kb:
        lines.append("<i>none live</i>")
    return "\n".join(lines), kb


def queue_view():
    snap = C.snapshot()
    rows = [r for r in snap["rows"]
            if not r["live"] and not r["retired"] and r["state"] == "dead"]
    lines = ["<b>Queued: %d</b>" % len(rows)]
    kb = []
    for r in rows[:12]:
        lines.append("<code>%s</code> %s" % (r["short"], esc(r["name"][:58])))
        kb.append([{"text": "bring up %s" % r["short"], "callback_data": "u:%s" % r["short"]}])
    if not kb:
        lines.append("<i>queue is empty</i>")
    return "\n".join(lines), kb


HELP = ("<b>Session control</b>\n\n"
        "text or voice: starts a new session with it\n"
        "file, photo or video with a caption: saved to the inbox, caption = folder\n"
        "/status: load and counters\n"
        "/live: live sessions, with kill buttons\n"
        "/queue: queued sessions, with bring-up buttons\n"
        "/kill &lt;id&gt;: stop a session\n"
        "/up &lt;id&gt;: bring a session up in the app\n"
        "/retire &lt;id&gt;: remove from the queue\n"
        "/unretire &lt;id&gt;: put back in the queue\n\n"
        "<i>id = the first 8 characters of the session UUID</i>\n\n"
        "News and new versions: t.me/failblog\n"
        "Found a bug: t.me/alexmanokhin")


# ---------------------------------------------------------------- dispatch

def run_action(fn, arg):
    try:
        return "ok: " + fn(arg)
    except C.LockBusy as e:
        return "busy: %s" % e
    except Exception as e:
        return "failed: %s: %s" % (type(e).__name__, e)


def fetch_file(file_id):
    """Download a Telegram attachment: first the path, then the file."""
    got = api("getFile", file_id=file_id)
    path = ((got or {}).get("result") or {}).get("file_path")
    if not path:
        raise ValueError("Telegram did not return the file: %s"
                         % str((got or {}).get("description"))[:120])
    url = "https://api.telegram.org/file/bot%s/%s" % (TOKEN, path)
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.read(), os.path.basename(path)


def handle_voice(chat, msg):
    """Voice message -> transcribed on this machine -> a new session.

    Dictation on the go: Telegram reaches the bot from anywhere, with no
    tailnet and no open ports, and transcription still runs locally.
    """
    v = msg.get("voice") or msg.get("audio") or msg.get("video_note") or {}
    dur = v.get("duration") or 0
    send(chat, "got %s s of audio, transcribing..." % dur)
    try:
        blob, name = fetch_file(v.get("file_id"))
        got = C.ingest(audio=blob, name=name)
    except Exception as exc:
        send(chat, "failed: %s" % esc(str(exc)[:300]))
        return
    send(chat, "<b>Transcript:</b>\n%s\n\n%s"
         % (esc(got["text"][:1500]), esc(got["msg"])))
    follow_link(chat, got)


def save_file(msg):
    """Any document, photo or video -> <inbox>/<caption folder>/<name>; returns (rel path, KB).

    Bot API serves files up to 20 MB only.
    """
    folder = re.sub(r"[^a-z0-9_-]+", "-", (msg.get("caption") or "").lower()).strip("-") or "_inbox"
    if msg.get("photo"):
        fid, name = msg["photo"][-1].get("file_id"), "photo-%s.jpg" % time.strftime("%Y%m%d-%H%M%S")
    else:
        d = msg.get("document") or msg.get("video") or {}
        fid = d.get("file_id")
        name = d.get("file_name") or ("file-" + time.strftime("%Y%m%d-%H%M%S")
                                      + (".mp4" if msg.get("video") else ""))
        if (d.get("file_size") or 0) > 20 * 1048576:
            raise ValueError("%.0f MB: the Bot API serves files up to 20 MB only"
                             % (d["file_size"] / 1048576.0))
    name = re.sub(r"[^\w.\-() ]+", "_", name)
    blob, _n = fetch_file(fid)
    dst_dir = os.path.join(INBOX, folder)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    if os.path.exists(dst):
        base, ext = os.path.splitext(name)
        dst = os.path.join(dst_dir, "%s-%s%s" % (base, time.strftime("%H%M%S"), ext))
    with open(dst, "wb") as fh:
        fh.write(blob)
    return os.path.join(folder, os.path.basename(dst)), len(blob) // 1024


def handle_file(chat, msg):
    try:
        rel, kb = save_file(msg)
    except Exception as exc:
        send(chat, "file not saved: %s" % esc(str(exc)[:300]))
        return
    send(chat, "saved %s (%d KB)" % (esc(rel), kb))


def handle_message(msg):
    chat = msg.get("chat", {}).get("id")
    if chat != OWNER_CHAT or (msg.get("from") or {}).get("id") != OWNER_CHAT:
        return                                   # silent: do not confirm we exist; only the owner, in their own chat
    if msg.get("photo") or msg.get("document") or msg.get("video"):
        handle_file(chat, msg)
        return
    if msg.get("voice") or msg.get("audio") or msg.get("video_note"):
        handle_voice(chat, msg)
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        # Plain text starts a session too: dictation from the keyboard.
        if text:
            try:
                got = C.ingest(text=text)
                send(chat, esc(got["msg"]))
                follow_link(chat, got)
            except Exception as exc:
                send(chat, "failed: %s" % esc(str(exc)[:300]))
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/start", "/help"):
        send(chat, HELP)
    elif cmd == "/status":
        send(chat, status_text())
    elif cmd == "/live":
        t, kb = live_view()
        send(chat, t, kb)
    elif cmd == "/queue":
        t, kb = queue_view()
        send(chat, t, kb)
    elif cmd in ("/kill", "/up", "/retire", "/unretire"):
        if not arg:
            send(chat, "needs an id: <code>%s 767e5629</code>" % cmd)
            return
        fn = {"/kill": C.act_kill, "/up": C.act_up,
              "/retire": C.act_retire, "/unretire": C.act_unretire}[cmd]
        send(chat, run_action(fn, arg))
    else:
        send(chat, "unknown command. /help")


def handle_callback(cb):
    chat = cb.get("message", {}).get("chat", {}).get("id")
    cbid = cb.get("id")
    # A tap runs only when the owner presses it in their own chat with the bot, so a forwarded
    # copy of a message with buttons can never act for whoever received it
    if chat != OWNER_CHAT or (cb.get("from") or {}).get("id") != OWNER_CHAT:
        api("answerCallbackQuery", callback_query_id=cbid, text="not allowed")
        return
    data = cb.get("data") or ""
    kind, _, target = data.partition(":")
    fn = {"k": C.act_kill, "u": C.act_up, "a": C.act_kill_app}.get(kind)
    if not fn:
        api("answerCallbackQuery", callback_query_id=cbid, text="?")
        return
    api("answerCallbackQuery", callback_query_id=cbid, text="working...")
    send(chat, run_action(fn, target))


ALERT_STATE = CFG.state("rc-bot-alert.json")
THRESHOLDS = (75, 90, 95, 100)
USAGE_EVERY = 900


def check_usage():
    """Warn once per threshold crossing, and re-arm when the week resets.

    The weekly limit is the one that bites, and most of the spend comes from
    sessions running in parallel. Crossing it silently is how a working day ends.
    """
    u = C.usage()
    wk = u.get("week")
    if wk is None:
        return
    try:
        with open(ALERT_STATE) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    last = st.get("level", 0)
    if wk < last:                                  # week reset — arm again
        last = 0
    level = max([t for t in THRESHOLDS if wk >= t] or [0])
    if level > last:
        send(OWNER_CHAT,
             "<b>Weekly limit: %d%%</b>\nresets %s\n\n"
             "Live sessions now: %d. Parallel sessions drive most of the spend; "
             "close the ones you do not need with /live." %
             (wk, u.get("week_resets", "?"), C.snapshot()["meta"]["live_total"]))
    if level != last:
        try:
            with open(ALERT_STATE, "w") as fh:
                json.dump({"level": level, "wk": wk}, fh)
        except OSError:
            pass


# ------------------------------------------------- files the sessions create
#
# Sessions write files on this machine while the owner is usually elsewhere,
# so every newly created file is forwarded to Telegram.
#
# Only `Write` (file created) counts, not `Edit`: forwarding every edit of an
# existing file would flood the chat.

SENT_PATH = CFG.state("rc-sentfiles.json")
FILES_EVERY = 90
FILES_FRESH = 45 * 60           # older files are no longer interesting
FILES_MAX_BYTES = 20 * 1024 * 1024
FILES_PER_SWEEP = 4             # a burst must not fire a volley of messages
FILES_SKIP = ("/.claude/", "/node_modules/", "/.git/", "/dist/", "/build/",
              "/.next/", "/venv/", "/.venv/", "/__pycache__/", "/tmp/")
FILES_SKIP_EXT = (".jsonl", ".log", ".lock", ".pyc", ".map")


def _sent_index():
    try:
        with open(SENT_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def follow_link(chat, got):
    """A new session's app link is known only after its first screen: the
    guardian reads it within a minute. Send it then, as the reply promised."""
    m = re.match(r"new session ([0-9a-f]{8}) launched", (got or {}).get("msg") or "")
    if not m:
        return
    who = m.group(1)

    def link():
        # the launch log has it seconds after start; the registry gets it on
        # the guardian's next archive pass (up to 5 min)
        import glob
        for f in glob.glob("/tmp/rc-new-*.uuid"):
            try:
                if os.stat(f).st_uid != os.getuid():   # /tmp is shared
                    continue
                with open(f) as fh:
                    if not fh.read().strip().startswith(who):
                        continue
            except OSError:
                continue
            got = C.G.head_link(f[:-len(".uuid")] + ".log")
            if got:
                return got
        reg = C.G.load(C.G.REG_PATH, {})
        for uuid, rec in (reg.get("sessions") or {}).items():
            if uuid.startswith(who) and rec.get("app_session"):
                return rec["app_session"]
        return ""

    def run():
        for _ in range(36):
            time.sleep(5)
            got = link()
            if got:
                send(chat, "session %s \u2192 https://claude.ai/code/%s" % (who, esc(got)))
                return
    threading.Thread(target=run, daemon=True).start()


def send_document(chat, path, caption):
    """Upload a file. Multipart by hand: urllib has no multipart helper and
    one call does not justify a dependency on requests."""
    boundary = "----rc%d" % int(time.time() * 1000)
    with open(path, "rb") as fh:
        blob = fh.read()
    parts = []
    for k, v in (("chat_id", str(chat)), ("caption", caption[:1000]),
                 ("parse_mode", "HTML")):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, k, v)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="document";'
                  ' filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                  % (boundary, os.path.basename(path))).encode("utf-8"))
    parts.append(blob)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        API % (TOKEN, "sendDocument"), data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def files_sweep():
    sent = _sent_index()
    fresh = time.time() - FILES_FRESH
    found = []
    for uuid, rec in (C.G.load(C.G.REG_PATH, {}).get("sessions") or {}).items():
        if rec.get("kind") != "rc":
            continue
        if rec.get("last_active_ts", 0) < fresh:
            continue
        path = os.path.join(C.G.PROJ, uuid + ".jsonl")
        try:
            st = os.stat(path)
        except OSError:
            continue
        for r in C._tail_records(path, st.st_size):
            c = (r.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                if b.get("name") != "Write":
                    continue
                fp = (b.get("input") or {}).get("file_path")
                if isinstance(fp, str) and fp.startswith("/"):
                    found.append((fp, rec.get("name") or uuid[:8]))

    done = 0
    for fp, sess in found:
        if done >= FILES_PER_SWEEP:
            break
        if any(x in fp for x in FILES_SKIP) or fp.endswith(FILES_SKIP_EXT):
            continue
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st.st_mtime < fresh or st.st_size == 0 or st.st_size > FILES_MAX_BYTES:
            continue
        key = "%s|%d" % (fp, int(st.st_mtime))
        if key in sent:
            continue
        try:
            send_document(OWNER_CHAT, fp,
                          "<b>%s</b>\n%s" % (esc(os.path.basename(fp)), esc(sess[:80])))
            sent[key] = int(time.time())
            done += 1
        except Exception as exc:
            sys.stderr.write("[files] %s: %s\n" % (fp, str(exc)[:150]))
            sys.stderr.flush()
            sent[key] = int(time.time())     # do not retry the same file forever
    if done or len(sent) > len(_sent_index()):
        # trim the index, or it grows forever
        if len(sent) > 4000:
            for k in sorted(sent, key=sent.get)[:2000]:
                del sent[k]
        tmp = SENT_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sent, fh)
        os.replace(tmp, SENT_PATH)
    return done


def files_refresher():
    def loop():
        # The first pass waits: on start the index is empty, and without the
        # pause it would send everything the sessions wrote in the last hour.
        time.sleep(FILES_EVERY)
        while True:
            try:
                files_sweep()
            except Exception:
                import traceback
                traceback.print_exc()
            time.sleep(FILES_EVERY)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def main():
    api("deleteWebhook")                          # getUpdates needs the webhook off
    r = api("getMe")
    sys.stderr.write("rc-bot as @%s\n" % (r.get("result", {}).get("username", "?")))
    sys.stderr.flush()
    files_refresher()
    offset = None
    next_usage = 0.0
    while True:
        if time.time() >= next_usage:
            next_usage = time.time() + USAGE_EVERY
            try:
                check_usage()
            except Exception:
                import traceback
                traceback.print_exc()
        r = api("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(5)                         # transient: back off, keep polling
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception:
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    main()
