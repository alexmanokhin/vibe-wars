#!/usr/bin/env python3
"""Claude accounts this Mac can move to by itself, without a browser.

20.11 the owner: «когда кончается лимит аккаунта, меня никак не зовут ... должен был
спамить в ТГ, чтобы я перезашёл, либо перезаходить автоматически в новый
аккаунт». On 19.11 from 10:00 one account was out of its weekly limit. The bot
sent one «100%» line and went quiet. 457 calls died on «You've hit your weekly
limit» (reel builders, deploys, the a bot job auto-fix) until a manual
sign-in at 02:39 the next night, 16 hours later.

How a switch works
- The CLI keeps the signed-in account in the Keychain item «Claude Code-credentials»
  (claudeAiOauth: accessToken, refreshToken, scopes, accountUuid,
  refreshTokenExpiresAt). On Linux it is ~/.claude/.credentials.json. Every guardian
  pass copies refreshToken + scopes of the CURRENT account into our own Keychain item
  «rc-panel-account» (account = email); on Linux a 0600 file. Signing in to another
  account does not revoke the one it replaces, so the copy stays usable.
- Switching = `claude auth login --claudeai` with CLAUDE_CODE_OAUTH_REFRESH_TOKEN and
  CLAUDE_CODE_OAUTH_SCOPES set. That is the CLI's own non-interactive sign-in: it
  spends the token (one use), stores the new pair and the profile itself, and puts the
  old credentials back if the server refuses. The guardian's next pass sees the new
  account and migrates the sessions, as after any switch.
- An account can be saved without moving the box: the panel's sign-in in «keep» mode
  runs `auth login` with CLAUDE_CONFIG_DIR=<own dir>. The CLI then writes to a separate
  Keychain item («Claude Code-credentials-<sha256(dir)[:8]>»); adopt_iso() moves it
  into the stash and deletes the rest.
- A sign-in lives about 30 days, and renewing the access token does not extend it
  (the CLI says so itself). The stash keeps refreshTokenExpiresAt; a reminder goes to
  Telegram 4 days before it runs out.

Policy (tick(), once per guardian pass)
- blocked = the current account's own /usage reading (fresh, taken after the switch)
  says week >= 100, or a transcript written in the last 10 min carries
  apiError usage_limit_reached (seven_day / five_hour) after the switch.
- blocked + a saved account with room: switch to the emptiest one, at most once per
  10 min. An account that failed is skipped for an hour, an empty one until its reset.
- blocked + nothing to switch to: Telegram every 10 min until it is unblocked, then one
  «работает снова».

No token ever goes to argv, a log or the panel: argv is readable by every process
(ps), and the panel's /file can read the home folder.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import re
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CLAUDE_BIN = CFG.CLAUDE
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
SECURITY = "/usr/bin/security"
MAC = sys.platform == "darwin"
CLI_ITEM = "Claude Code-credentials"
STASH_ITEM = "rc-panel-account"
STASH_FILE = CFG.state("rc-accounts-stash.json")   # Linux only
STATE = CFG.state("rc-accounts.json")              # no secrets in it
LOCK = CFG.state("rc-accounts.lock")
LEDGER = CFG.state("rc-usage-accounts.json")       # rc_control writes it
NOAUTO = CFG.state("rc-accounts-noauto")
RUN_CWD = CFG.state("rc-usage-cwd")
PROJECTS = os.path.join(HOME, ".claude/projects")
TG = os.path.join(CFG.HERE, "rc_notify.py")
DEVICES = CFG.state("rc-devices.json")

HIT_WINDOW_S = 600          # a limit refusal this recent means «blocked now»
GRACE_S = 120               # after a switch, the old account's stragglers still write
READING_FRESH_S = 900       # a /usage reading older than this says nothing
AUTO_EVERY_S = 600          # never two automatic switches closer than this
BAD_SKIP_S = 3600           # an account whose switch failed rests this long
ALARM_EVERY_S = 600         # Telegram while blocked with nowhere to go
EXPIRY_WARN_D = 4
ROOM_MAX_WEEK = 97          # a candidate must have more room than this
BLOCKING_TYPES = ("seven_day", "five_hour")


# ------------------------------------------------------------------ helpers

def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, data, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _sha(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _scrub(text):
    """CLI output without anything that looks like a credential."""
    t = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;;[^\x07]*\x07?", " ", text or "")
    t = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "sk-ant-…", t)
    t = re.sub(r"[A-Za-z0-9_\-]{40,}", "…", t)
    return re.sub(r"\s+", " ", t).strip()


def machine_name():
    for d in _load(DEVICES, []) or []:
        if (d or {}).get("id") == "local" and d.get("name"):
            return d["name"]
    return os.uname()[1].split(".")[0]


def _state():
    st = _load(STATE, {})
    st.setdefault("accounts", {})
    return st


def auto_on():
    return not os.path.exists(NOAUTO)


def set_auto(on):
    if on:
        try:
            os.unlink(NOAUTO)
        except OSError:
            pass
    else:
        with open(NOAUTO, "w") as fh:
            fh.write(time.strftime("%F %T") + "\n")
    return auto_on()


# ------------------------------------------------------------ secret store

def _kc_get(svc, acct=None):
    cmd = [SECURITY, "find-generic-password", "-s", svc]
    if acct:
        cmd += ["-a", acct]
    try:
        r = subprocess.run(cmd + ["-w"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _kc_put(svc, acct, secret):
    # `security -i` reads the command from stdin: the secret never shows in argv.
    # base64 keeps its own parser away from quotes and spaces in the JSON.
    b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    cmd = 'add-generic-password -U -s "%s" -a "%s" -l "%s" -w "%s"\n' % (
        svc, acct, "RC panel saved sign-in", b64)
    r = subprocess.run([SECURITY, "-i"], input=cmd, capture_output=True, text=True,
                       timeout=20)
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        raise RuntimeError("keychain write failed: %s" % _scrub(r.stderr)[:200])


def _kc_del(svc, acct=None):
    cmd = [SECURITY, "delete-generic-password", "-s", svc]
    if acct:
        cmd += ["-a", acct]
    try:
        subprocess.run(cmd, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def _iso_items(config_dir):
    """Keychain names the CLI may have used for CLAUDE_CONFIG_DIR=config_dir:
    «Claude Code-credentials-» + sha256(dir)[:8]. The dir as the CLI saw it is
    the env value; the variants cover a trailing slash or a resolved symlink."""
    seen = []
    for d in (config_dir, os.path.normpath(config_dir), os.path.realpath(config_dir),
              config_dir.rstrip("/") + "/"):
        n = CLI_ITEM + "-" + _sha(d)[:8]
        if n not in seen:
            seen.append(n)
    return seen


def cli_creds(config_dir=None):
    """claudeAiOauth of the CLI signed in under config_dir (None = the main one)."""
    if MAC:
        raw = None
        for svc in (_iso_items(config_dir) if config_dir else [CLI_ITEM]):
            raw = _kc_get(svc)
            if raw:
                break
    else:
        path = os.path.join(config_dir or os.path.join(HOME, ".claude"), ".credentials.json")
        try:
            with open(path) as fh:
                raw = fh.read()
        except OSError:
            raw = None
    try:
        return (json.loads(raw) or {}).get("claudeAiOauth") or {} if raw else {}
    except ValueError:
        return {}


def profile(config_dir=None):
    path = os.path.join(config_dir, ".claude.json") if config_dir else CLAUDE_JSON
    return (_load(path, {}) or {}).get("oauthAccount") or {}


def current_email():
    return profile().get("emailAddress") or ""


def _stash_get(email):
    if MAC:
        raw = _kc_get(STASH_ITEM, email)
        if not raw:
            return None
        try:
            return json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception:
            return None
    return (_load(STASH_FILE, {}) or {}).get(email)


def _stash_put(email, payload):
    if MAC:
        _kc_put(STASH_ITEM, email, json.dumps(payload))
        return
    d = _load(STASH_FILE, {}) or {}
    d[email] = payload
    _save(STASH_FILE, d)


def _stash_drop(email):
    if MAC:
        _kc_del(STASH_ITEM, email)
        return
    d = _load(STASH_FILE, {}) or {}
    if d.pop(email, None) is not None:
        _save(STASH_FILE, d)


# ---------------------------------------------------------------- saving

def _remember(st, email, o, via):
    rt = o.get("refreshToken")
    h = _sha(rt)[:16]
    a = st["accounts"].setdefault(email, {})
    if a.get("rt_hash") == h:
        return False
    _stash_put(email, {"rt": rt, "scopes": " ".join(o.get("scopes") or []),
                       "uuid": o.get("accountUuid") or "",
                       "rt_exp": o.get("refreshTokenExpiresAt")})
    a.update({"rt_hash": h, "saved_at": time.time(), "via": via,
              "rt_exp": o.get("refreshTokenExpiresAt"),
              "uuid": o.get("accountUuid") or ""})
    for k in ("bad_at", "bad_why"):
        a.pop(k, None)
    return True


def _save_current(st):
    """Copy the signed-in account's refresh token into the stash. Returns email."""
    o = cli_creds()
    prof = profile()
    email = prof.get("emailAddress") or ""
    if not o.get("refreshToken") or not email:
        return email
    # mid-switch the Keychain and ~/.claude.json can name different accounts for a
    # moment; filing one's token under the other's email would poison both
    if o.get("accountUuid") and prof.get("accountUuid") and o["accountUuid"] != prof["accountUuid"]:
        return email
    _remember(st, email, o, "current")
    return email


def save_current():
    with _lock():
        st = _state()
        email = _save_current(st)
        _save(STATE, st)
        return email


def adopt_iso(config_dir):
    """A «keep» sign-in finished in its own config dir: move it into the stash.
    Returns the email it saved, or "" when the dir holds no finished sign-in."""
    o = cli_creds(config_dir)
    email = profile(config_dir).get("emailAddress") or ""
    if not o.get("refreshToken") or not email:
        return ""
    with _lock():
        st = _state()
        _remember(st, email, o, "keep")
        _save(STATE, st)
    drop_iso(config_dir)
    return email


def drop_iso(config_dir):
    if MAC:
        for svc in _iso_items(config_dir):
            _kc_del(svc)
    shutil.rmtree(config_dir, ignore_errors=True)


def forget(email):
    with _lock():
        st = _state()
        st["accounts"].pop(email, None)
        _stash_drop(email)
        _save(STATE, st)


# --------------------------------------------------------------- switching

def _signin_running():
    try:
        out = subprocess.run(["/bin/ps", "-Ao", "args="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(re.match(r"\s*(?:\S*/)?(?:claude|[\d.]+)\s+auth\s+login\b", l)
               for l in out.splitlines())


def _clean_env():
    return dict((k, v) for k, v in os.environ.items()
                if not k.startswith("CLAUDE_CODE_OAUTH") and k not in (
                    "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))


# ⛔ 20.11: the reading kept for an idle account is only as fresh as the last time
# the box ran on it. a saved account showed 29% (17.11) while it really stood at
# 47%. Choosing by that, the box can move onto an account that is already empty,
# and every move restarts all sessions. So a candidate is signed in to first in a
# throwaway CLI config (its own Keychain item, the box does not move) and asked
# for /usage; the rotated token goes back to the stash. Idle accounts get the
# same check every PROBE_EVERY_S, which also shows a broken saved sign-in long
# before the day it is needed.
PROBE_DIR = CFG.state("rc-accounts-probe")
PROBE_EVERY_S = 3 * 3600


def _parse_usage(txt):
    out = {}
    for label, key in (("Current week (all models)", "week"), ("Current week (Fable)", "week_fable"),
                       ("Current session", "session")):
        m = re.search(re.escape(label) + r":\s*(\d+)%\s*used(?:\s*.\s*resets\s*([^\n]+))?", txt or "")
        if m:
            out[key] = int(m.group(1))
            out[key + "_resets"] = (m.group(2) or "").strip()
    return out


def _recover_probe():
    """A probe killed between spending the token and saving the new one leaves
    the new one in PROBE_DIR's Keychain item: file it before anything else."""
    if not os.path.isdir(PROBE_DIR):
        return ""
    o = cli_creds(PROBE_DIR)
    email = profile(PROBE_DIR).get("emailAddress") or ""
    if o.get("refreshToken") and email:
        with _lock():
            st = _state()
            _remember(st, email, o, "probe-recovered")
            _save(STATE, st)
    drop_iso(PROBE_DIR)
    return email


def probe(email, usage=True):
    """Sign in to a saved account in a throwaway CLI config, keep the rotated
    token, read its real limits. {"ok", "week", "week_resets", "why"}"""
    out = {"ok": False, "week": None, "week_resets": "", "why": ""}
    _recover_probe()
    env = _clean_env()
    env["CLAUDE_CONFIG_DIR"] = PROBE_DIR
    with _lock():
        p = _stash_get(email)
        if not p or not p.get("rt") or not p.get("scopes"):
            out["why"] = "no saved sign-in"
            return out
        drop_iso(PROBE_DIR)
        os.makedirs(PROBE_DIR, 0o700, exist_ok=True)
        os.makedirs(RUN_CWD, exist_ok=True)
        e1 = dict(env, CLAUDE_CODE_OAUTH_REFRESH_TOKEN=p["rt"], CLAUDE_CODE_OAUTH_SCOPES=p["scopes"])
        try:
            r = subprocess.run([CLAUDE_BIN, "auth", "login", "--claudeai"], env=e1, cwd=RUN_CWD,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            txt, code = _scrub((r.stdout or "") + " " + (r.stderr or "")), r.returncode
        except subprocess.TimeoutExpired:
            txt, code = "auth login hung for 60 s", -1
        o = cli_creds(PROBE_DIR)
        got = profile(PROBE_DIR).get("emailAddress") or ""
        st = _state()
        a = st["accounts"].setdefault(email, {})
        a["probed_at"] = time.time()
        if o.get("refreshToken") and got == email:
            _remember(st, email, o, "probe")
            out["ok"] = True
        elif o.get("refreshToken") and got:
            _remember(st, got, o, "probe")          # a token filed under the wrong email: keep it where it belongs
            a["bad_at"], a["bad_why"] = time.time(), "the saved sign-in belongs to %s" % got
            out["why"] = a["bad_why"]
        else:
            a["bad_at"] = time.time()
            a["bad_why"] = ("exit %s: %s" % (code, txt))[-300:]
            out["why"] = a["bad_why"]
        _save(STATE, st)
    if out["ok"] and usage:
        try:
            u = subprocess.run([CLAUDE_BIN, "-p", "/usage"], env=env, cwd=RUN_CWD,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
            got_u = _parse_usage((u.stdout or "") + (u.stderr or ""))
        except (OSError, subprocess.SubprocessError):
            got_u = {}
        if "week" in got_u:
            out["week"], out["week_resets"] = got_u["week"], got_u.get("week_resets", "")
            led = _load(LEDGER, {}) or {}
            rec = dict(led.get(email) or {})
            rec.update(got_u)
            rec["ts"] = time.time()
            led[email] = rec
            _save(LEDGER, led, 0o644)
            if got_u["week"] > ROOM_MAX_WEEK:
                rs = reset_ts(got_u.get("week_resets"))
                with _lock():
                    st = _state()
                    st["accounts"].setdefault(email, {})["empty_until"] = rs or (time.time() + 3600)
                    _save(STATE, st)
    # the stash holds the new token now; the probe's Keychain item and dir go
    drop_iso(PROBE_DIR)
    return out


def _probe_idle(st, cur, now, log):
    """At most one idle saved account per pass, each every PROBE_EVERY_S."""
    due = sorted((a.get("probed_at") or 0, e) for e, a in st["accounts"].items()
                 if e != cur and a.get("rt_hash")
                 and now - (a.get("probed_at") or 0) >= PROBE_EVERY_S
                 and not (a.get("bad_at") and now - a["bad_at"] < BAD_SKIP_S))
    if not due:
        return
    email = due[0][1]
    r = probe(email)
    log("accounts: checked saved %s: %s" % (
        email, ("week %s%%" % r["week"]) if r["ok"] else ("FAILED " + r["why"][:120])))


def switch_to(email, why="manual"):
    """Sign the main CLI in to a saved account. Returns (ok, message)."""
    with _lock():
        st = _state()
        cur = _save_current(st)            # the last token of the account we leave
        _save(STATE, st)
        if cur == email:
            return True, "already on %s" % email
        p = _stash_get(email)
        if not p or not p.get("rt"):
            return False, "no saved sign-in for %s" % email
        if not p.get("scopes"):
            return False, "the saved sign-in for %s has no scopes" % email
        env = _clean_env()
        env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = p["rt"]
        env["CLAUDE_CODE_OAUTH_SCOPES"] = p["scopes"]
        os.makedirs(RUN_CWD, exist_ok=True)
        try:
            r = subprocess.run([CLAUDE_BIN, "auth", "login", "--claudeai"], env=env,
                               cwd=RUN_CWD, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120)
            out = _scrub((r.stdout or "") + " " + (r.stderr or ""))
            code = r.returncode
        except subprocess.TimeoutExpired:
            out, code = "auth login hung for 120 s", -1
        now_email = ""
        for _ in range(15):
            now_email = current_email()
            if now_email == email:
                break
            time.sleep(1)
        st = _state()
        a = st["accounts"].setdefault(email, {})
        if now_email == email:
            _save_current(st)              # the token just spent was replaced: keep the new one
            a = st["accounts"].setdefault(email, {})
            a["switched_in_at"] = time.time()
            a["switched_in_why"] = why
            st["last_switch"] = {"at": time.time(), "from": cur, "to": email, "why": why}
            _save(STATE, st)
            return True, "switched %s -> %s" % (cur, email)
        a["bad_at"] = time.time()
        a["bad_why"] = ("exit %s: %s" % (code, out))[-300:]
        _save(STATE, st)
        return False, "could not switch to %s (exit %s): %s" % (email, code, out[-200:])


# ------------------------------------------------------------- limit signal

def reset_ts(s, now=None):
    """«Nov 21 at 6am (Asia/Nicosia)» -> epoch seconds, None if unreadable."""
    m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2})(?:,?\s*(\d{4}))?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
                 r"(?:\s*\(([^)]+)\))?", s or "")
    if not m:
        return None
    now = now or time.time()
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
               "Oct", "Nov", "Dec"].index(m.group(1)) + 1
        h = int(m.group(4)) % 12 + (12 if m.group(6) == "pm" else 0)
        tz = ZoneInfo(m.group(7)) if m.group(7) else None
        base = _dt.datetime.fromtimestamp(now, tz)
        year = int(m.group(3)) if m.group(3) else base.year
        at = _dt.datetime(year, mon, int(m.group(2)), h, int(m.group(5) or 0), tzinfo=tz)
        if not m.group(3) and at.timestamp() - now > 200 * 86400:
            at = at.replace(year=year - 1)
        return at.timestamp()
    except Exception:
        return None


def _iso_ts(s):
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def limit_hits(since, window=HIT_WINDOW_S):
    """Refusals for a used-up limit written lately by any claude on this Mac:
    [{"ts", "resets", "type", "file"}]. Only files touched within the window are
    opened, and only their last 128 KB."""
    now = time.time()
    floor = max(since or 0, now - window)
    out = []
    try:
        dirs = list(os.scandir(PROJECTS))
    except OSError:
        return out
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            files = list(os.scandir(d.path))
        except OSError:
            continue
        for f in files:
            if not f.name.endswith(".jsonl"):
                continue
            try:
                if f.stat().st_mtime < floor:
                    continue
                with open(f.path, "rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 131072))
                    chunk = fh.read()
            except OSError:
                continue
            if b"usage_limit_reached" not in chunk:
                continue
            for line in chunk.splitlines():
                if b"usage_limit_reached" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = _iso_ts(r.get("timestamp"))
                if not ts or ts < floor:
                    continue
                info = ((r.get("apiErrorParams") or {}).get("rate_limit_info")
                        or r.get("quotaLimits") or {})
                out.append({"ts": ts, "resets": info.get("resetsAt"),
                            "type": info.get("rateLimitType") or "", "file": f.path})
    return out


def is_limit_stop(rec):
    """A transcript record that is Claude Code's «you are out of limit» reply."""
    return (rec.get("type") == "assistant"
            and rec.get("apiError") == "usage_limit_reached")


# ------------------------------------------------------------------ policy

def _fmt_ts(ts):
    if not ts:
        return "?"
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def _kind_ru(kind):
    return {"week": "недельный лимит", "seven_day": "недельный лимит",
            "five_hour": "5-часовой лимит"}.get(kind, "лимит")


def blocked_reasons(st, cur, now=None):
    """Why the signed-in account counts as used up now ([] = it does not).

    ⛔ After a switch, processes started under the old account (a builder, a
    `claude -p` already in flight, a /usage poll that began a second before) can
    still write the OLD account's refusal or reading for a while. Taken at face
    value that would «block» the new account and bounce the box again. So: a
    grace period after the switch; a refusal whose reset time is another
    account's known reset is that account's; and refusals alone need two
    different transcripts, one is enough only next to a 100% reading."""
    now = now or time.time()
    since = (st.get("current_since") or 0) + GRACE_S
    out = []
    led = (_load(LEDGER, {}) or {}).get(cur) or {}
    ts = led.get("ts") or 0
    if (now - ts < READING_FRESH_S and ts > since
            and led.get("week") is not None and led["week"] >= 100):
        rs = reset_ts(led.get("week_resets"), now)
        if not rs or rs > now:
            out.append({"kind": "week", "resets": rs, "since": ts})
    theirs = [a.get("empty_until") for e, a in st["accounts"].items()
              if e != cur and a.get("empty_until")]
    hits = []
    for h in limit_hits(since):
        if h["type"] not in BLOCKING_TYPES:
            continue
        r = h.get("resets")
        if r and r <= now:
            continue
        if r and any(abs(r - x) < 180 for x in theirs):
            continue
        hits.append(h)
    if hits and (out or len(set(h["file"] for h in hits)) >= 2):
        h = min(hits, key=lambda x: x["ts"])
        out.append({"kind": h["type"], "resets": h.get("resets"), "since": h["ts"]})
    return out


def candidates(st, cur, now=None):
    """Saved accounts with room, emptiest first: [(email, week or None)]."""
    now = now or time.time()
    led = _load(LEDGER, {}) or {}
    out = []
    for email, a in st["accounts"].items():
        if email == cur or not a.get("rt_hash"):
            continue
        if a.get("bad_at") and now - a["bad_at"] < BAD_SKIP_S:
            continue
        if a.get("rt_exp") and a["rt_exp"] / 1000.0 < now:
            continue
        if a.get("empty_until") and a["empty_until"] > now:
            continue
        r = led.get(email) or {}
        wk = r.get("week")
        rs = reset_ts(r.get("week_resets"), now)
        if rs and rs <= now:
            wk = 0
        if wk is not None and wk > ROOM_MAX_WEEK:
            continue
        out.append((email, wk))
    out.sort(key=lambda x: (x[1] if x[1] is not None else 60))
    return out


def why_not(st, cur, now=None):
    """One line per other known account: why it cannot be switched to."""
    now = now or time.time()
    led = _load(LEDGER, {}) or {}
    names = list(dict.fromkeys(list(st["accounts"]) + list(led)))
    lines = []
    for email in names:
        if email == cur or not email or "@" not in email:
            continue
        a = st["accounts"].get(email) or {}
        r = led.get(email) or {}
        rs = reset_ts(r.get("week_resets"), now)
        if not a.get("rt_hash"):
            lines.append("%s: вход не сохранён" % email)
        elif a.get("rt_exp") and a["rt_exp"] / 1000.0 < now:
            lines.append("%s: сохранённый вход истёк" % email)
        elif a.get("bad_at") and now - a["bad_at"] < BAD_SKIP_S:
            lines.append("%s: вход не принят (%s)" % (email, (a.get("bad_why") or "")[:80]))
        elif a.get("empty_until") and a["empty_until"] > now:
            lines.append("%s: лимит до %s" % (email, _fmt_ts(a["empty_until"])))
        elif r.get("week") is not None and r["week"] > ROOM_MAX_WEEK and not (rs and rs <= now):
            lines.append("%s: неделя %s%%, сброс %s" % (email, r["week"], _fmt_ts(rs)))
        else:
            lines.append("%s: можно" % email)
    return lines


def hq_url():
    """https://<this Mac's tailnet name>/play, or "" when it cannot be told.
    The name is the reverse DNS of the address the panel binds to (MagicDNS);
    `tailscale serve` publishes the panel there."""
    bind = os.environ.get("RC_PANEL_BIND") or ""
    if not bind:
        try:
            import plistlib
            with open(os.path.join(HOME, "Library/LaunchAgents/%s.rc-panel.plist" % CFG.LABEL_PREFIX), "rb") as fh:
                bind = (plistlib.load(fh).get("EnvironmentVariables") or {}).get("RC_PANEL_BIND") or ""
        except Exception:
            bind = ""
    if not bind or bind.startswith("127."):
        return ""
    try:
        import socket
        name = socket.gethostbyaddr(bind)[0].rstrip(".")
    except Exception:
        return ""
    return "https://%s/play#acc" % name if name.endswith(".ts.net") else ""


def _signin_hint(st, cur, now):
    """Which account to sign in to, and where."""
    led = _load(LEDGER, {}) or {}
    best = None
    for email, r in led.items():
        if email == cur or "@" not in email:
            continue
        wk = r.get("week")
        rs = reset_ts(r.get("week_resets"), now)
        if rs and rs <= now:
            wk = 0
        if wk is None or wk > ROOM_MAX_WEEK:
            continue
        if best is None or wk < best[1]:
            best = (email, wk)
    url = hq_url()
    where = ("HQ (%s) → кнопка A" % url) if url else "HQ → кнопка A"
    if best:
        return ("Войди в %s (неделя %d%%): %s → у него SWITCH TO IT → ссылка → код"
                % (best[0], best[1], where))
    return "Войди в аккаунт с запасом: %s → SIGN IN → ссылка → код" % where


def tick(log=print, notify=None, dry=False):
    """One policy pass. The guardian calls it every minute; never raises."""
    notify = notify or _notify
    now = time.time()
    got = _recover_probe()
    if got:
        log("accounts: filed the token a broken-off check of %s left behind" % got)
    with _lock():
        st = _state()
        cur = _save_current(st)
        _save(STATE, st)
    if not cur:
        return
    st = _state()
    if st.get("current") != cur:
        if st.get("alarm_n"):
            notify("Claude на %s снова работает: аккаунт %s. Сессии переезжают на него, "
                   "остановленные лимитом получат «продолжай»." % (machine_name(), cur))
        st["current"] = cur
        st["current_since"] = now
        for k in ("blocked_since", "alarm_at", "alarm_n", "block_kind", "block_resets"):
            st.pop(k, None)
        _save(STATE, st)
        return                       # this pass's readings belong to the old account

    reasons = blocked_reasons(st, cur, now)
    if not reasons:
        if st.get("blocked_since"):
            if st.get("alarm_n"):
                notify("Claude на %s снова работает: лимит аккаунта %s восстановился."
                       % (machine_name(), cur))
            log("accounts: %s is not blocked any more" % cur)
            for k in ("blocked_since", "alarm_at", "alarm_n", "block_kind", "block_resets"):
                st.pop(k, None)
        _expiry_reminders(st, cur, now, notify, log)
        _save(STATE, st)
        if not dry:
            _probe_idle(_state(), cur, now, log)
        return

    first = min(r["since"] for r in reasons)
    resets = max([r["resets"] for r in reasons if r.get("resets")] or [0]) or None
    kind = reasons[0]["kind"]
    if not st.get("blocked_since"):
        st["blocked_since"] = first
        log("accounts: %s is out of its %s (resets %s)" % (cur, kind, _fmt_ts(resets)))
    st["block_kind"], st["block_resets"] = kind, resets
    a = st["accounts"].setdefault(cur, {})
    if resets:
        a["empty_until"] = resets

    switched = False
    if (auto_on() and not dry and now - (st.get("auto_at") or 0) >= AUTO_EVERY_S
            and not _signin_running()):
        cands = candidates(st, cur, now)
        _save(STATE, st)
        for email, wk in cands[:3]:
            st["auto_at"] = now
            _save(STATE, st)
            pr = probe(email)
            log("accounts: before switching, %s: %s" % (
                email, ("week %s%%" % pr["week"]) if pr["ok"] else ("FAILED " + pr["why"][:120])))
            st = _state()
            if not pr["ok"] or (pr["week"] is not None and pr["week"] > ROOM_MAX_WEEK):
                continue
            if pr["week"] is not None:
                wk = pr["week"]
            ok, msg = switch_to(email, why="limit")
            log("accounts: auto-switch %s -> %s: %s" % (cur, email, msg))
            st = _state()
            if ok:
                switched = True
                notify("Аккаунт %s на %s исчерпал %s (сброс %s). Сам переключил на %s "
                       "(неделя %s). Сессии переезжают по 3 в минуту, остановленные лимитом "
                       "получат «продолжай»." % (
                           cur, machine_name(), _kind_ru(kind), _fmt_ts(resets), email,
                           ("%s%%" % wk) if wk is not None else "без свежих цифр"))
                st["alarm_n"] = 0
                break
    if switched:
        _save(STATE, st)
        return

    if not dry and now - (st.get("alarm_at") or 0) >= ALARM_EVERY_S:
        mins = int((now - st["blocked_since"]) / 60)
        others = why_not(st, cur, now)
        txt = ("Claude на %s стоит: аккаунт %s исчерпал %s (сброс %s), сессии и сборки "
               "не работают%s.\n" % (machine_name(), cur, _kind_ru(kind), _fmt_ts(resets),
                                      (" уже %d мин" % mins) if mins >= 2 else ""))
        if not auto_on():
            txt += "Автопереключение выключено.\n"
        txt += ("Переключиться самому не на что:\n" + "\n".join("· " + l for l in others) + "\n"
                if others else "Других аккаунтов я не знаю.\n")
        txt += ("%s. После входа сессии переедут и продолжат сами.\n"
                "Чтобы дальше переключалось без тебя: там же у каждого аккаунта SAVE SIGN-IN.\n"
                "Пишу каждые 10 минут, пока не войдёшь." % _signin_hint(st, cur, now))
        notify(txt)
        st["alarm_at"] = now
        st["alarm_n"] = (st.get("alarm_n") or 0) + 1
        log("accounts: alarm %d sent, %s blocked %d min" % (st["alarm_n"], cur, mins))
    _save(STATE, st)


def _expiry_reminders(st, cur, now, notify, log):
    today = time.strftime("%F", time.localtime(now))
    for email, a in st["accounts"].items():
        exp = (a.get("rt_exp") or 0) / 1000.0
        if not exp or not a.get("rt_hash"):
            continue
        days = (exp - now) / 86400.0
        if days > EXPIRY_WARN_D or a.get("expiry_warned") == today:
            continue
        a["expiry_warned"] = today
        if email == cur:
            how = ("HQ → A → SIGN IN с этим же адресом (сессии никуда не переезжают)")
        else:
            how = ("HQ → A → у этого аккаунта SAVE SIGN-IN (%s останется на %s)"
                   % (machine_name(), cur))
        if days <= 0:
            line = "Сохранённый вход в %s на %s истёк." % (email, machine_name())
        else:
            line = ("Вход в %s на %s закончится через %d дн. (%s)." % (
                email, machine_name(), max(1, int(days + 0.5)), _fmt_ts(exp)))
        notify(line + " Обнови его: " + how + ".")
        log("accounts: expiry reminder for %s (%.1f d)" % (email, days))


def _notify(text):
    if not os.path.exists(TG):
        return
    subprocess.run([sys.executable, TG, text], capture_output=True, timeout=30)


def public_state():
    """What the panel may show: no tokens, no hashes."""
    st = _state()
    now = time.time()
    out = {}
    for email, a in st["accounts"].items():
        out[email] = {
            "saved": bool(a.get("rt_hash")),
            "saved_at": a.get("saved_at"),
            "saved_exp": (a.get("rt_exp") or 0) / 1000.0 or None,
            "saved_bad": (a.get("bad_why") or "")[:160]
                         if a.get("bad_at") and now - a["bad_at"] < BAD_SKIP_S else "",
            "empty_until": a.get("empty_until") if (a.get("empty_until") or 0) > now else None,
        }
    return {"accounts": out, "auto": auto_on(),
            "blocked_since": st.get("blocked_since"), "block_kind": st.get("block_kind"),
            "block_resets": st.get("block_resets"), "last_switch": st.get("last_switch")}


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--status"]:
        print(json.dumps(public_state(), indent=1, ensure_ascii=False))
        st = _state()
        cur = current_email()
        print("current:", cur)
        print("blocked:", blocked_reasons(st, cur))
        print("candidates:", candidates(st, cur))
        print("\n".join(why_not(st, cur)))
    elif a[:1] == ["--save"]:
        print(save_current())
    elif a[:1] == ["--switch"] and len(a) > 1:
        print(switch_to(a[1], why="cli"))
    elif a[:1] == ["--probe"] and len(a) > 1:
        print(probe(a[1]))
    elif a[:1] == ["--tick-dry"]:
        tick(dry=True)
    else:
        print("usage: rc_accounts.py --status | --save | --switch <email> | --probe <email> | --tick-dry")
