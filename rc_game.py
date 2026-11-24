"""rc_game — the game layer of the panel: XP, levels, daily quests, achievements.

The /play page ("HQ") turns session control into a game. This module is
the source of truth for progress. The server sees every /act (a send, a new
session, a wake-up, a closed mission), and a watcher thread sees sessions
finish their turns and ship things (commits, deploys, artifacts) even when no
page is open. The page only draws what /api/game returns.

State: ~/.claude/rc-game.json, written atomically, at most every few seconds.
Python 3.9 (launchd /usr/bin/python3): no match, no X | Y.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import rc_config as CFG  # noqa: E402
import rc_control as C
import rc_guardian as G

try:
    from zoneinfo import ZoneInfo as _ZI
    PLAN_TZ = _ZI("Europe/Madrid")
except Exception:
    PLAN_TZ = None

STATE_PATH = CFG.state("rc-game.json")
ACT_LOG = CFG.state("rc-panel-act.log")
EVENTS_KEEP = 300
FRESH_S = 1800            # a finished turn stays "your move" this long
QUICK_S = 120             # a reply this soon after a finish is a quick draw
TURN_MIN_S = 15           # busy blips shorter than this are not a turn
DONE_CONFIRM = 2          # not-busy passes in a row before a turn counts as done
POLL_ACTIVE_S = 4         # someone has the page open
POLL_IDLE_S = 12
CLIENT_ACTIVE_S = 90
SNAP_TTL_S = 2.0          # /api/state and the watcher share one snapshot
LOOT_READ_MAX = 4 << 20   # bytes of new transcript read per session per pass
SAVE_EVERY_S = 3
STREAK_MIN_CMDS = 5

XP = {"say": 5, "answer": 5, "new": 15, "up": 5, "retire": 8, "quick": 10,
      "commit": 15, "push": 10, "deploy": 40, "publish": 30, "artifact": 25,
      "quest": 60, "dailies": 100, "ach": 50, "goal": 100, "won": 150}

# 10-16: the commander's portrait in HQ follows these (play.js CMDR), from the
# desert kid in the pink cap to a grand master.
RANKS = ((1, "Desert Kid"), (3, "Crew Mechanic"), (5, "Crew Pilot"),
         (8, "Smuggler"), (12, "Padawan"), (16, "Master Knight"),
         (20, "Crew General"), (25, "Master Master"), (30, "Grand Master"))

# key: (icon, title, how to get it)
ACHIEVEMENTS = (
    ("first_blood", "🗡", "First Blood", "Send a command while HQ keeps score"),
    ("juggler", "🤹", "Juggler", "3 agents working at once"),
    ("octopus", "🐙", "Octopus", "6 agents working at once"),
    ("swarm", "🐝", "Swarm", "10 agents working at once"),
    ("quick_draw", "⚡", "Quick Draw", "Reply within 30 s of a finished turn"),
    ("night_owl", "🦉", "Night Owl", "Send a command between 1 and 5 am"),
    ("early_bird", "🐓", "Early Bird", "Send a command between 5 and 7 am"),
    ("shipper", "🚀", "Shipper", "An agent deployed something"),
    ("ship_storm", "🌪", "Ship Storm", "5 deploys in one day"),
    ("committed", "📦", "Committed", "10 commits in one day"),
    ("marathon", "🏃", "Marathon", "An agent worked 30 min on one turn"),
    ("clean_desk", "🧹", "Clean Desk", "Complete 5 missions in one day"),
    ("recruiter", "🪖", "Recruiter", "Recruit 3 agents in one day"),
    ("voice", "🎙", "Voice Commander", "10 voice commands"),
    ("centurion", "💯", "Centurion", "100 commands"),
    ("thousand", "🏛", "Thousand Orders", "1000 commands"),
    ("streak3", "🔥", "On Fire", "3 days in a row with every must-do closed"),
    ("streak7", "☄", "Unstoppable", "7 days in a row with every must-do closed"),
    ("flow", "🌈", "Flow State", "Fill the flow meter to the top"),
    ("dailies", "🏆", "Day Won", "Close every must-do of a day"),
    ("big_brain", "🧠", "Big Brain", "An agent passed 500k tokens of context"),
)
ACH_BY_KEY = dict((a[0], a) for a in ACHIEVEMENTS)

# key: (title, day counter, goal). One quest from each pool per day.
QUESTS = {
    "turns8": ("Turn in 8 results", "turns", 8),
    "cmds20": ("Send 20 commands", "cmds", 20),
    "peak3": ("Run 3 agents at once", "peak", 3),
    "quick3": ("Quick draw ×3: reply < 2 min after a finish", "quick", 3),
    "deploy2": ("Ship it: 2 deploys", "deploys", 2),
    "commit3": ("Land 3 commits", "commits", 3),
    "voice3": ("3 voice commands", "voice", 3),
    "retire2": ("Complete 2 missions", "retired", 2),
    "new1": ("Recruit a new agent", "new", 1),
}
QUEST_POOLS = (("turns8", "cmds20"),
               ("peak3", "quick3", "deploy2", "commit3", "voice3"),
               ("retire2", "new1"))

# What agents ship. Matched against Bash commands in the transcript.
_GIT = r"\bgit\s+(?:-[cC]\s+\S+\s+)*"
LOOT_RX = (
    ("commit", re.compile(_GIT + r"commit\b")),
    ("push", re.compile(_GIT + r"push\b")),
    ("deploy", re.compile(r"\b(?:railway\s+up|vercel\b[^\n]*--prod|vercel\s+deploy|"
                          r"fly\s+deploy|wrangler\s+(?:deploy|publish)|"
                          r"supabase\s+functions\s+deploy|deploy[\w-]*\.sh|"
                          r"npm\s+run\s+deploy|launchctl\s+kickstart)")),
    ("publish", re.compile(r"\bpublish_(?:trial|safe)\.py\b")),
)
LOOT_COOLDOWN_S = 300     # one deploy per agent per 5 min: restarts come in bursts
LOOT_ICON = {"commit": "📦", "push": "⬆", "deploy": "🚀", "publish": "📣",
             "artifact": "📜"}
LOOT_WORD = {"commit": "committed", "push": "pushed", "deploy": "deployed",
             "publish": "published a video", "artifact": "published an artifact"}

_ASK_RX = re.compile(r"Enter\s*to\s*select")
_PROMPT_RX = re.compile(r"(?:bypass\s*permissions|for\s*shortcuts|auto-accept\s*edits|"
                        r"plan\s*mode\s*on)")
_WORK_RX = re.compile(r"(\d+)s\s*·\s*↓\s*([\d.]+k?)\s*tokens")

LOCK = threading.RLock()
_S = None
_DIRTY = [False, 0.0]
_CLIENT_TS = [0.0]
_SNAP = {"ts": 0.0, "snap": None, "gen": 0, "building": None, "build_gen": 0}
_SNAP_WAIT_S = 30.0
_SNAP_MU = threading.Lock()
_SAVE_MU = threading.Lock()
_ASK_MEMO = {}


# ------------------------------------------------------------------ state

def _today(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")


def _blank():
    return {"v": 1, "xp": 0, "seq": 0, "since": time.time(), "events": [],
            "days": {}, "ach": {}, "agents": {}, "flow": {"v": 0.0, "ts": time.time()},
            "quests_done": {}, "totals": {"cmds": 0, "voice": 0}}


def _load():
    global _S
    if _S is not None:
        return _S
    try:
        with open(STATE_PATH) as fh:
            _S = json.load(fh)
        for k, v in _blank().items():
            _S.setdefault(k, v)
    except (OSError, ValueError):
        _S = _blank()
        _backfill(_S)
        _mark()
    return _S


def _mark():
    _DIRTY[0] = True


def _save(force=False):
    if not _DIRTY[0] or (not force and time.time() - _DIRTY[1] < SAVE_EVERY_S):
        return
    with LOCK:
        S = _load()
        S["events"] = S["events"][-EVENTS_KEEP:]
        cut = _today(time.time() - 60 * 86400)
        for d in [d for d in S["days"] if d < cut]:
            S["days"].pop(d, None)
        blob = json.dumps(S, ensure_ascii=False)
        _DIRTY[0] = False
        _DIRTY[1] = time.time()
    with _SAVE_MU:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(blob)
        os.replace(tmp, STATE_PATH)


def _day(S, ts=None):
    d = S["days"].setdefault(_today(ts), {})
    for k in ("cmds", "turns", "quick", "deploys", "commits", "artifacts", "publishes",
              "new", "retired", "revived", "voice", "peak", "xp"):
        d.setdefault(k, 0)
    return d


def _event(S, kind, xp=0, sid="", title="", text="", icon="", ts=None):
    S["seq"] += 1
    S["events"].append({"id": S["seq"], "ts": ts or time.time(), "k": kind,
                        "xp": int(xp), "sid": sid[:8], "t": (title or "")[:80],
                        "x": (text or "")[:180], "i": icon})
    _mark()


def _gain(S, xp, ts=None):
    xp = int(round(xp))
    S["xp"] += xp
    _day(S, ts)["xp"] += xp
    _mark()
    return xp


def _unlock(S, key, ts=None):
    if key in S["ach"] or key not in ACH_BY_KEY:
        return
    S["ach"][key] = ts or time.time()
    a = ACH_BY_KEY[key]
    _gain(S, XP["ach"], ts)
    _event(S, "ach", XP["ach"], title=a[2], text=a[3], icon=a[1], ts=ts)


def _backfill(S):
    """Seed a new state from the panel's act log, so day one is not level 1 with
    nothing behind it. Only what the log proves: sends, new sessions, closes."""
    lines = []
    for p in (ACT_LOG + ".1", ACT_LOG):
        try:
            with open(p) as fh:
                lines.extend(fh.read().splitlines())
        except OSError:
            pass
    n = 0
    for ln in lines:
        f = ln.split(" ", 6)
        if len(f) < 5 or f[4] != "ok":
            continue
        try:
            ts = time.mktime(time.strptime(f[0] + " " + f[1], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        act = f[2]
        d = _day(S, ts)
        if act in ("say", "answer"):
            # a replayed retry is logged too, with the first try's answer and
            # 0.0 s: it is the same message, not a second one
            if f[5] == "0.0s":
                continue
            d["cmds"] += 1
            S["totals"]["cmds"] += 1
            _gain(S, XP["say"], ts)
            n += 1
        elif act == "new":
            d["new"] += 1
            _gain(S, XP["new"], ts)
        elif act in ("retire", "kill"):
            d["retired"] += 1
            _gain(S, XP["retire"], ts)
    if n:
        _event(S, "hello", 0, title="HQ online",
               text="%d commands from the panel log are already on your record" % n,
               icon="🛰")


# ------------------------------------------------------------------ helpers

def level_for(xp, k=60):
    """Cumulative XP to reach level L is k*(L-1)*L."""
    lv = 1
    while k * lv * (lv + 1) <= xp:
        lv += 1
    return lv, k * (lv - 1) * lv, k * lv * (lv + 1)


def rank_for(lv):
    name = RANKS[0][1]
    for at, nm in RANKS:
        if lv >= at:
            name = nm
    return name


def _flow(S, now=None):
    now = now or time.time()
    f = S["flow"]
    v = max(0.0, f.get("v", 0.0) - (now - f.get("ts", now)) / 15.0)
    return v


def _flow_add(S, pts):
    now = time.time()
    v = min(100.0, _flow(S, now) + pts)
    S["flow"] = {"v": v, "ts": now}
    if v >= 100:
        _unlock(S, "flow")
    _mark()


def _mult(v):
    return 2.0 if v >= 80 else (1.5 if v >= 50 else 1.0)


def _streak(S):
    days = S["days"]
    d = datetime.now()
    if (days.get(d.strftime("%Y-%m-%d")) or {}).get("cmds", 0) < STREAK_MIN_CMDS:
        d -= timedelta(days=1)
    n = 0
    while (days.get(d.strftime("%Y-%m-%d")) or {}).get("cmds", 0) >= STREAK_MIN_CMDS:
        n += 1
        d -= timedelta(days=1)
    return n


def _quests_today():
    seed = sum(ord(c) * (i + 7) for i, c in enumerate(_today()))
    return [pool[(seed // (j + 1)) % len(pool)] for j, pool in enumerate(QUEST_POOLS)]


def _check_quests(S):
    """The daily quests are gone; nothing to check without a goals source."""
    return


def _plan_day(ts=None):
    """The planner's day: 05:00 to 05:00 Madrid."""
    if PLAN_TZ is None:
        return datetime.fromtimestamp(ts or time.time()).date()
    return (datetime.fromtimestamp(ts or time.time(), PLAN_TZ) - timedelta(hours=5)).date()


def _touched(S):
    """Sessions the owner wrote to today (the planner's «focus: 6 or fewer»). Counted
    from browser sends; a day that began before this existed is seeded from the
    act log, minus the bracketed automation prompts ([planner …] and the like)."""
    day = _plan_day()
    key = day.isoformat()
    t = S.setdefault("touched", {})
    for k in [k for k in t if k < (day - timedelta(days=14)).isoformat()]:
        t.pop(k, None)
    if key not in t:
        seen = []
        if PLAN_TZ is not None:
            start = datetime.combine(day, datetime.min.time()).replace(
                hour=5, tzinfo=PLAN_TZ).timestamp()
            try:
                with open(ACT_LOG) as fh:
                    lines = fh.read().splitlines()[-4000:]
            except OSError:
                lines = []
            for ln in lines:
                f = ln.split(" ", 6)
                if len(f) < 7 or f[2] not in ("say", "answer") or f[4] != "ok":
                    continue
                try:
                    ts = time.mktime(time.strptime(f[0] + " " + f[1], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                if ts < start:
                    continue
                said = f[6].split(": ", 1)[1] if ": " in f[6] else ""
                if said.lstrip().startswith("[") or f[3] in seen:
                    continue
                seen.append(f[3])
        t[key] = seen
        _mark()
    return t[key]


def _agent(S, uuid, row=None):
    a = S["agents"].get(uuid)
    if a is None:
        # A veteran session starts with XP for the work already in it: a 130 MB
        # transcript is weeks of turns, and level 1 there would be a lie.
        seed = 0
        try:
            seed = int(os.path.getsize(os.path.join(G.PROJ, uuid + ".jsonl")) / 1e6 * 20)
        except OSError:
            pass
        a = S["agents"][uuid] = {"xp": min(seed, 6000), "turns": 0, "busy_since": 0,
                                 "idle_n": 0, "last_done": 0, "last_cmd": 0,
                                 "off": -1, "loot_ts": {}}
        _mark()
    return a


def _title(row):
    return (row.get("title") or row.get("name") or row.get("short") or "")[:60]


# ------------------------------------------------------------------ hooks

# actions that change what a row shows (its name, project, lane, or whether it
# is there at all)
_FRESH_AFTER = {"set_title", "set_project", "set_model", "set_effort", "retire", "unretire",
                "kill", "kill_requeue", "up", "bulk_retire", "bulk_unretire", "bulk_kill",
                "bulk_up", "bulk_restart"}


def on_act(req, out):
    """Called by the panel for every /act result. Never raises."""
    try:
        if (isinstance(req, dict) and isinstance(out, dict) and out.get("ok")
                and req.get("action") in _FRESH_AFTER):
            snap_stale()
        _on_act(req, out)
    except Exception:
        pass


def _ids(target):
    if isinstance(target, dict):
        ids = target.get("ids")
        if ids:
            return [str(x) for x in ids]
        return [str(target.get("id") or "")]
    if isinstance(target, list):
        return [str(x) for x in target]
    return [str(target or "")]


def _on_act(req, out):
    if not isinstance(req, dict) or not isinstance(out, dict):
        return
    if not out.get("ok") or out.get("replayed"):
        return
    # ⛔ 10-16: the planner and the watchers send through /act too; XP and the
    # day's session count are for what the owner does, i.e. requests from a browser
    if not req.get("_web"):
        return
    # 10-19: UNDO of an archive from a board card puts back what was there;
    # it is not a revival worth XP
    if req.get("quiet"):
        return
    act = req.get("action") or ""
    now = time.time()
    with LOCK:
        S = _load()
        d = _day(S)
        if act in ("say", "answer", "up", "new"):
            sid = _ids(req.get("target"))[0]
            if act == "new":
                mm = re.search(r"new session ([0-9a-f]{8})", str(out.get("msg") or ""))
                sid = mm.group(1) if mm else ""
            touched = _touched(S)
            if sid and sid[:8] not in touched:
                touched.append(sid[:8])
                _mark()
        if act in ("say", "answer"):
            sid = _ids(req.get("target"))[0]
            try:
                uuid = C.resolve(sid)
            except Exception:
                uuid = sid
            a = _agent(S, uuid)
            v = _flow(S, now)
            m = _mult(v)
            busy = sum(1 for x in S["agents"].values() if x.get("busy_since"))
            xp = _gain(S, XP[act] * m)
            d["cmds"] += 1
            S["totals"]["cmds"] = S["totals"].get("cmds", 0) + 1
            quick = (a.get("last_done") and a.get("last_cmd", 0) < a["last_done"]
                     and now - a["last_done"] <= QUICK_S)
            if quick:
                xp += _gain(S, XP["quick"] * m)
                d["quick"] += 1
                if now - a["last_done"] <= 30:
                    _unlock(S, "quick_draw")
            a["last_cmd"] = now
            _flow_add(S, 10 + (5 if busy >= 2 else 0))
            row = _row(uuid)
            _event(S, "cmd", xp, sid=uuid, title=_title(row) if row else sid,
                   text=("quick draw" + (" ×%g" % m if m > 1 else "")) if quick else
                   ("flow ×%g" % m if m > 1 else ""),
                   icon="⚡" if quick else "➤")
            hr = datetime.now().hour
            _unlock(S, "first_blood")
            if 1 <= hr < 5:
                _unlock(S, "night_owl")
            elif 5 <= hr < 7:
                _unlock(S, "early_bird")
            tot = S["totals"]["cmds"]
            if tot >= 100:
                _unlock(S, "centurion")
            if tot >= 1000:
                _unlock(S, "thousand")
        elif act == "new":
            mm = re.search(r"new session ([0-9a-f]{8})", str(out.get("msg") or ""))
            xp = _gain(S, XP["new"])
            d["new"] += 1
            _event(S, "new", xp, sid=mm.group(1) if mm else "",
                   title="New agent recruited", icon="🪖",
                   text=str((req.get("target") or {}).get("text") or "")[:120]
                   if isinstance(req.get("target"), dict) else "")
            if d["new"] >= 3:
                _unlock(S, "recruiter")
        elif act in ("up", "bulk_up", "unretire", "bulk_unretire"):
            n = len(_ids(req.get("target")))
            xp = _gain(S, XP["up"] * n)
            d["revived"] += n
            _event(S, "up", xp, sid=_ids(req.get("target"))[0] if n == 1 else "",
                   title="Agent back on duty" if n == 1 else "%d agents back on duty" % n,
                   icon="💫")
        elif act in ("retire", "kill", "bulk_retire", "bulk_kill"):
            n = len(_ids(req.get("target")))
            xp = _gain(S, XP["retire"] * n)
            d["retired"] += n
            row = _row(_ids(req.get("target"))[0]) if n == 1 else None
            _event(S, "retire", xp, sid=_ids(req.get("target"))[0] if n == 1 else "",
                   title="Mission complete" if n == 1 else "%d missions complete" % n,
                   text=_title(row) if row else "", icon="🏁")
            if d["retired"] >= 5:
                _unlock(S, "clean_desk")
        else:
            return
        _check_quests(S)
    _save()


def on_voice(ok):
    if not ok:
        return
    try:
        with LOCK:
            S = _load()
            _day(S)["voice"] += 1
            S["totals"]["voice"] = S["totals"].get("voice", 0) + 1
            if S["totals"]["voice"] >= 10:
                _unlock(S, "voice")
            _check_quests(S)
        _save()
    except Exception:
        pass


# ------------------------------------------------------------------ snapshot

def snapshot_cached(max_age=SNAP_TTL_S):
    """C.snapshot() costs ~0.5 s on the mini. /api/state (the classic page and
    the chat inside HQ) and the watcher share one copy for max_age seconds.

    ⛔ 10-30: one build at a time. Every expired poll built its own copy, and they
    queue on the locks inside C.snapshot(): 1 at once 0.67 s, 30 at once 27 s each,
    60 at once 66 s each. Under load a poll gave up and the next one started another
    build, so the API stayed dead after the load had gone. While a build is running
    a reader gets the old copy; after an action it waits for a build that started
    after that action, or starts one."""
    with _SNAP_MU:
        have = _SNAP["snap"]
        if have is not None and time.time() - _SNAP["ts"] < max_age:
            return have
        gen = _SNAP["gen"]
        ev = _SNAP["building"]
        if ev is not None and _SNAP["build_gen"] == gen:
            if have is not None and _SNAP["ts"] > 0:
                return have
        else:
            ev = None
            mine = _SNAP["building"] = threading.Event()
            _SNAP["build_gen"] = gen
    if ev is not None:
        ev.wait(_SNAP_WAIT_S)
        with _SNAP_MU:
            have = _SNAP["snap"]
        return have if have is not None else C.snapshot()
    try:
        snap = C.snapshot()
        with _SNAP_MU:
            # an action landed while this one was being read: keep no copy from before it
            if _SNAP["gen"] == gen:
                _SNAP["snap"] = snap
                _SNAP["ts"] = time.time()
    finally:
        with _SNAP_MU:
            if _SNAP["building"] is mine:
                _SNAP["building"] = None
        mine.set()
    return snap


def snap_stale():
    """A rename, a retire or a restore changed the registry: the next reader
    builds a fresh snapshot instead of showing the old name for 4 s more."""
    with _SNAP_MU:
        _SNAP["ts"] = 0.0
        _SNAP["gen"] += 1


def _row(uuid_or_short):
    snap = _SNAP["snap"]
    if not snap or not uuid_or_short:
        return None
    for r in snap["rows"]:
        if r["uuid"] == uuid_or_short or r["short"] == uuid_or_short[:8]:
            return r
    return None


# ------------------------------------------------------------------ watcher

def _asking(log):
    """An open AskUserQuestion menu is not in the transcript until answered
    (10-14, 72aab730 sat «idle» for an hour). The screen shows it: the menu
    footer «Enter to select» is drawn after the last prompt box."""
    if not log:
        return False
    try:
        st = os.stat(log)
    except OSError:
        return False
    key = (log, st.st_size, int(st.st_mtime))
    if key in _ASK_MEMO:
        return _ASK_MEMO[key]
    try:
        with open(log, "rb") as fh:
            if st.st_size > 40000:
                fh.seek(st.st_size - 40000)
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    flat = C._TUI_CHARSET.sub("", C._TUI_OSC.sub("", C._TUI_ANSI.sub("", blob)))

    def last(rx):
        p = -1
        for m in rx.finditer(flat):
            p = m.start()
        return p
    ask = last(_ASK_RX)
    out = ask >= 0 and ask > last(_WORK_RX) and ask > last(_PROMPT_RX)
    if len(_ASK_MEMO) > 500:
        _ASK_MEMO.clear()
    _ASK_MEMO[key] = out
    return out


def _turn_event(line):
    """What a transcript line means for the turn clock: ('end'|'start'|'human', ts)
    or None. 10-16: «минуты работы показывает время отдельного шага» — HQ timed
    the busy streak it had seen itself; now it reads the turn from the record."""
    if b'"turn_duration"' in line:
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        if rec.get("type") == "system" and rec.get("subtype") == "turn_duration":
            return ("end", C._epoch_ts(rec.get("timestamp")) or time.time())
        return None
    if b'"queue-operation"' in line and b'"enqueue"' in line:
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        return ("start", C._epoch_ts(rec.get("timestamp")) or time.time())
    if b'"stop_reason":"end_turn"' in line:
        # 10-18: a finished reply ends the turn too: turns end without
        # turn_duration now and then (23 of 1408 on 09-25), and the next
        # prompt then ran on the old clock.
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        msg = rec.get("message") or {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        if (rec.get("type") == "assistant" and not rec.get("isSidechain")
                and not rec.get("isApiErrorMessage")
                and not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)):
            return ("end", C._epoch_ts(rec.get("timestamp")) or time.time())
        return None
    if b'"type":"user"' not in line or b'"tool_use_id"' in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    ts = C._epoch_ts(rec.get("timestamp")) or time.time()
    content = (rec.get("message") or {}).get("content")
    text = content if isinstance(content, str) else " ".join(
        b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
    text = str(text or "").lstrip()
    if text.startswith("[Request interrupted"):
        return ("stop", ts)
    if text.startswith("<task-notification>"):
        return ("start", ts)
    if _is_human(line):
        return ("human", ts)
    return None


TURN_STREAK_S = 120     # a turn starting this soon after the last one ended continues it
TURN_V = 4              # bump to re-read every agent's turn clock (3: end_turn ends a turn, 4: not a compaction)
TURN_QUIET_S = 600      # not busy this long: whatever turn was open is over


def _turn_apply(a, ev):
    kind, ts = ev
    if kind == "stop":              # stopped by hand: the stretch is over
        a["turn_open"] = False
        a["turn_end"] = 0
    elif kind == "end":
        a["turn_open"] = False
        a["turn_end"] = ts
    elif not a.get("turn_open"):
        a["turn_open"] = True
        # a message sent mid-turn ends that turn and opens the next at once:
        # one stretch of work for the person, so the clock keeps running
        if not (a.get("turn_start") and a.get("turn_end") and ts - a["turn_end"] < TURN_STREAK_S):
            a["turn_start"] = ts


def _turn_init(path, a, size):
    """First sight of a session: the open turn, from the tail of the file."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - (1536 << 10)))
            blob = fh.read()
    except OSError:
        return
    a["turn_open"] = False
    a["turn_start"] = 0
    a["turn_end"] = 0
    for line in blob.split(b"\n")[1:]:
        ev = _turn_event(line)
        if ev:
            _turn_apply(a, ev)


def _scan_loot(uuid, a):
    """New tool calls in the transcript since the last pass → loot kinds."""
    path = os.path.join(G.PROJ, uuid + ".jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    off = a.get("off", -1)
    if off < 0 or off > size or a.get("turn_v") != TURN_V:
        # first sight (or an agent known from before the turn clock existed):
        # where the running stretch began comes from the full parse (the tail
        # of a busy transcript can hold only minutes); then only new lines
        if off < 0 or off > size:
            a["off"] = size
        return [("turninit", None)]
    if size == off:
        return []
    start = max(off, size - LOOT_READ_MAX)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read(size - start)
    except OSError:
        return []
    cut = blob.rfind(b"\n")
    if cut < 0:
        return []
    a["off"] = start + cut + 1
    found = []
    for line in blob[:cut].split(b"\n"):
        ev = _turn_event(line)
        if ev:
            found.append(("turn", ev))
            if ev[0] == "human":
                found.append("human")
            continue
        if b'"type":"user"' in line and b'"tool_use_id"' not in line:
            continue
        if b'"tool_use"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name") or ""
            inp = b.get("input") or {}
            if name == "Bash":
                cmd = str(inp.get("command") or "")
                for kind, rx in LOOT_RX:
                    if rx.search(cmd):
                        found.append(kind)
            elif name == "Artifact" and (inp.get("action") in (None, "", "publish")) \
                    and not inp.get("asset"):
                found.append("artifact")
    return found


def _is_human(line):
    """A prompt a person typed (here, in the classic panel, in TG), not a
    background-task notification that wakes the agent up on its own (10-15:
    the cartoon session woke every few minutes from its render monitors, and
    each of those turns scored as a «result» with a toast)."""
    try:
        rec = json.loads(line)
    except ValueError:
        return False
    # a compaction's summary is a "user" record too (10-18: it opened a turn
    # that never closed, and HQ said «working 45h» two days later)
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain") \
            or rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if not texts and not any(isinstance(b, dict) and b.get("type") == "image"
                                 for b in content):
            return False
        content = " ".join(texts)
    text = str(content or "").lstrip()
    if not text and not isinstance((rec.get("message") or {}).get("content"), list):
        return False
    for mark in ("<task-notification>", "<system-reminder>", "<command-name>",
                 "<local-command", "[Request interrupted", "Caveat:"):
        if text.startswith(mark):
            return False
    return True


# ------------------------------------------------------------------ fleet trail
# the owner 10-18: «экран со всеми видами сессий, чтобы на одном экране они
# помещались и я видел процесс по каждой сессии». HQ's FLEET screen shows every
# session at once, and for a live one its last steps (the prompt it got, the
# tools it ran, what it said) over a minute-by-minute activity strip. The
# watcher reads them as each transcript grows. Memory only: a restart rebuilds
# them from the tail of each transcript.
TRAIL_KEEP = 8            # steps kept per session
TRAIL_TAIL = 768 << 10    # bytes read on first sight (and after a burst bigger than this)
TRAIL_MIN = 30            # minutes in the activity strip
_TRAIL = {}               # uuid -> {"off", "steps": [[ts, kind, text]], "hits": {minute: n}, "n", "v"}
_TRAIL_MU = threading.Lock()
_TRAIL_V = [0]            # bumped on every change: the page asks only for what is newer

_TOOL_KIND = {"Bash": "run", "Edit": "edit", "MultiEdit": "edit", "Write": "edit",
              "NotebookEdit": "edit", "Read": "read", "Grep": "find", "Glob": "find",
              "WebSearch": "web", "WebFetch": "web", "Agent": "agent", "Task": "agent",
              "Skill": "skill", "AskUserQuestion": "ask", "Artifact": "art",
              "SendUserFile": "art", "TodoWrite": "plan"}
_MD_LEAD = re.compile(r"^(?:#+|>|[-*•]|\d+[.)])\s+")


def _first_line(text, n=150):
    """The first line worth showing of a message: no fences, table rules or
    markdown marks."""
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("```") or set(ln) <= set("|-:= "):
            continue
        ln = _MD_LEAD.sub("", ln).replace("**", "").replace("__", "").replace("`", "")
        ln = " ".join(ln.split())
        if ln:
            return ln[:n]
    return ""


def _base(p):
    p = str(p or "").rstrip("/")
    return os.path.basename(p) or p


def _trail_tool(name, inp):
    """(kind, text) of one tool call, the way the terminal labels the step."""
    inp = inp if isinstance(inp, dict) else {}
    kind = _TOOL_KIND.get(name) or ("mcp" if name.startswith("mcp__") else "tool")
    if kind == "run":
        text = inp.get("description") or str(inp.get("command") or "").strip().split("\n")[0]
    elif kind == "edit":
        text = ("Write " if name == "Write" else "Edit ") + _base(
            inp.get("file_path") or inp.get("notebook_path"))
    elif kind == "read":
        text = "Read " + _base(inp.get("file_path"))
    elif kind == "find":
        text = "Search " + str(inp.get("pattern") or "")
    elif name == "WebSearch":
        text = "Web search: " + str(inp.get("query") or "")
    elif name == "WebFetch":
        url = str(inp.get("url") or "")
        text = "Fetch " + (url.split("//", 1)[-1].split("/", 1)[0] or url)
    elif kind == "agent":
        text = "Subagent: " + str(inp.get("description") or inp.get("prompt") or "")
    elif kind == "skill":
        text = "Skill " + str(inp.get("skill") or "")
    elif kind == "ask":
        qs = inp.get("questions") or []
        q = qs[0] if qs and isinstance(qs[0], dict) else {}
        text = "Asks: " + str(q.get("question") or q.get("header") or "")
    elif kind == "art":
        fp = inp.get("file_path") or (inp.get("file_paths") or [""])[0]
        text = ("Send " if name == "SendUserFile" else
                "Page " + str(inp.get("action") or "publish") + " ") + _base(fp or inp.get("url"))
    elif kind == "plan":
        text = "Update the plan"
    elif kind == "mcp":
        parts = name.split("__")
        srv = (parts[1] if len(parts) > 2 else "").replace("claude_ai_", "").replace("_", " ")
        text = (srv + ": " if srv else "") + parts[-1].replace("_", " ")
    else:
        lab = C._tool_label(inp)
        text = name + (": " + lab if lab else "")
    return kind, " ".join(str(text).split())[:150]


_TASK_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def _task_note(text):
    """«Background command "Render the video" completed (exit code 0)»."""
    m = _TASK_SUMMARY.search(text or "")
    return " ".join((m.group(1) if m else "A background task reported back").split())[:150]


def _trail_add(tr, ts, kind, text):
    steps = tr["steps"]
    if kind == "u":
        # a prompt sent to a busy session is written twice: enqueue, then
        # (sometimes) the real record
        if any(s[1] == "u" and s[2] == text and abs(ts - s[0]) < 900 for s in steps):
            return
        tr["n"] = 0
    else:
        if kind not in ("a", "wake", "sys", "stop"):
            tr["n"] += 1            # tool calls since the prompt
        m = int(ts // 60)
        tr["hits"][m] = tr["hits"].get(m, 0) + 1
    steps.append([int(ts), kind, text])
    if len(steps) > TRAIL_KEEP:
        del steps[:-TRAIL_KEEP]


def _trail_lines(tr, blob):
    for line in blob.split(b"\n"):
        if b'"isSidechain":true' in line:
            continue
        if b'"type":"assistant"' in line:
            want = "assistant"
        elif b'"type":"user"' in line:
            if b'"tool_use_id"' in line:
                continue                    # a tool result
            want = "user"
        elif b'"queue-operation"' in line and b'"enqueue"' in line:
            want = "queue-operation"
        else:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != want:
            continue
        ts = C._epoch_ts(rec.get("timestamp")) or time.time()
        if want == "queue-operation":
            if rec.get("operation") == "enqueue":
                head = str(rec.get("content") or "").lstrip()
                if head.startswith("<task-notification>"):
                    _trail_add(tr, ts, "wake", _task_note(head))
                elif not head.startswith("<"):
                    txt = _first_line(C._PASTE_TAG.sub("", head))
                    if txt:
                        _trail_add(tr, ts, "u", txt)
            continue
        content = (rec.get("message") or {}).get("content")
        if want == "user":
            if rec.get("isCompactSummary"):
                _trail_add(tr, ts, "sys", "Memory compacted, work goes on")
                continue
            if C._is_meta(rec) or rec.get("isVisibleInTranscriptOnly"):
                continue
            raw = content if isinstance(content, str) else " ".join(
                b.get("text") or "" for b in content or []
                if isinstance(b, dict) and b.get("type") == "text")
            head = str(raw or "").lstrip()
            if head.startswith("[Request interrupted"):
                _trail_add(tr, ts, "stop", "Stopped by hand")
            elif head.startswith("<task-notification>"):
                _trail_add(tr, ts, "wake", _task_note(head))
            elif _is_human(line):
                txt = _first_line(C._PASTE_TAG.sub("", head))
                if not txt and isinstance(content, list):
                    txt = "(an image)"
                if txt:
                    _trail_add(tr, ts, "u", txt)
            continue
        if not isinstance(content, list) or rec.get("isApiErrorMessage"):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                txt = _first_line(b.get("text"))
                if txt:
                    _trail_add(tr, ts, "a", txt)
            elif b.get("type") == "tool_use":
                kind, txt = _trail_tool(b.get("name") or "", b.get("input"))
                if txt:
                    _trail_add(tr, ts, kind, txt)


def _trail_scan(uuid):
    """Read what the transcript gained since the last pass into the trail."""
    path = os.path.join(G.PROJ, uuid + ".jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    with _TRAIL_MU:
        old = _TRAIL.get(uuid)
    if old is not None and old["off"] == size:
        return
    if old is None or old["off"] > size:
        old = None
        tr = {"off": 0, "steps": [], "hits": {}, "n": 0, "v": 0}
    else:
        tr = {"off": old["off"], "steps": [list(s) for s in old["steps"]],
              "hits": dict(old["hits"]), "n": old["n"], "v": old["v"]}
    start = tr["off"]
    skip = old is None or size - start > TRAIL_TAIL
    if skip:
        start = max(0, size - TRAIL_TAIL)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read(size - start)
    except OSError:
        return
    if skip and start > 0:
        nl = blob.find(b"\n")
        start += nl + 1 if nl >= 0 else len(blob)
        blob = blob[nl + 1:] if nl >= 0 else b""
    cut = blob.rfind(b"\n")
    tr["off"] = start + cut + 1 if cut >= 0 else start
    if cut >= 0:
        _trail_lines(tr, blob[:cut])
    floor = int(time.time() // 60) - 2 * TRAIL_MIN
    for m in [m for m in tr["hits"] if m < floor]:
        del tr["hits"][m]
    changed = old is None or tr["steps"] != old["steps"] or tr["n"] != old["n"] \
        or any(tr["hits"].get(m) != c for m, c in old["hits"].items() if m >= floor) \
        or any(m not in old["hits"] for m in tr["hits"])
    with _TRAIL_MU:
        if changed:
            _TRAIL_V[0] += 1
            tr["v"] = _TRAIL_V[0]
        _TRAIL[uuid] = tr


def _trail_keep(live_ids):
    with _TRAIL_MU:
        for uuid in [u for u in _TRAIL if u not in live_ids]:
            del _TRAIL[uuid]


def trail_out(since, live):
    """The FLEET screen's part of /api/game: the trails changed after `since`
    (0 = all), and which sessions have one at all."""
    now_m = int(time.time() // 60)
    out = {}
    with _TRAIL_MU:
        v = _TRAIL_V[0]
        for r in live:
            tr = _TRAIL.get(r["uuid"])
            if not tr or tr["v"] <= since:
                continue
            strip = "".join("0123456789abcdefghijklmnopqrstuvwxyz"[min(35, tr["hits"].get(m, 0))]
                            for m in range(now_m - TRAIL_MIN + 1, now_m + 1))
            out[r["short"]] = {"st": [list(s) for s in tr["steps"]], "n": tr["n"],
                               "h": [now_m, strip]}
    return {"v": v, "since": since, "s": out}


def _pass():
    snap = snapshot_cached(1.0)
    now = time.time()
    rows = [r for r in snap["rows"] if r.get("live")]
    try:
        procs = C.ps_rows()
    except Exception:
        procs = None
    asking = {}
    for r in rows:
        if r.get("busy"):
            continue
        try:
            log = C.log_for(r["uuid"], procs) if procs is not None else None
        except Exception:
            log = None
        asking[r["uuid"]] = bool((r.get("digest") or {}).get("waiting")) or _asking(log)
    # transcripts are read outside the lock: an act must not wait on file IO.
    # Before the busy logic, so a turn's own prompt is known when it ends.
    loot = {}
    for r in rows:
        with LOCK:
            a = dict(_agent(_load(), r["uuid"], r))
        got = _scan_loot(r["uuid"], a)
        if got and got[0] == ("turninit", None):
            tse = 0
            if r.get("busy"):
                try:
                    tse = (C.transcript(r["short"], 1).get("turn") or {}).get("tse") or now
                except Exception:
                    tse = now
            got = [("turnstate", (bool(r.get("busy")), tse))]
        loot[r["uuid"]] = (a["off"], got)
    for r in rows:
        try:
            _trail_scan(r["uuid"])
        except Exception as exc:     # the trail is a view: it must not stop scoring
            import sys
            sys.stderr.write("[game] trail %s failed: %r\n" % (r["short"], exc))
    _trail_keep(set(r["uuid"] for r in rows))
    with LOCK:
        S = _load()
        d = _day(S)
        for r in rows:
            off, got = loot.get(r["uuid"], (None, []))
            a = _agent(S, r["uuid"], r)
            if off is not None and a.get("off") != off:
                a["off"] = off
                _mark()
            for kind in got:
                if isinstance(kind, tuple):
                    if kind[0] == "turn":
                        _turn_apply(a, kind[1])
                    elif kind[0] == "turnstate":
                        a["turn_open"], a["turn_start"] = kind[1]
                        a["turn_end"] = 0
                        a["turn_v"] = TURN_V
                    _mark()
                    continue
                if kind == "human":
                    a["human_ts"] = now
                    _mark()
                    continue
                last = (a.get("loot_ts") or {}).get(kind, 0)
                if kind in ("deploy", "publish", "push") and now - last < LOOT_COOLDOWN_S:
                    continue
                a.setdefault("loot_ts", {})[kind] = now
                xp = _gain(S, XP[kind])
                a["xp"] = a.get("xp", 0) + XP[kind]
                field = {"commit": "commits", "deploy": "deploys", "publish": "publishes",
                         "artifact": "artifacts"}.get(kind)
                if field:
                    d[field] += 1
                _event(S, "loot", xp, sid=r["uuid"], title=_title(r),
                       text=LOOT_WORD[kind], icon=LOOT_ICON[kind])
                if kind in ("deploy", "publish"):
                    _unlock(S, "shipper")
                    if d["deploys"] + d["publishes"] >= 5:
                        _unlock(S, "ship_storm")
                if d["commits"] >= 10:
                    _unlock(S, "committed")
        busy_n = 0
        for r in rows:
            uuid = r["uuid"]
            a = _agent(S, uuid, r)
            a["asking"] = asking.get(uuid, False)
            busy = bool(r.get("busy"))
            if busy:
                busy_n += 1
                a["idle_n"] = 0
                if a.get("quiet_since"):
                    a["quiet_since"] = 0
                    _mark()
                if not a.get("busy_since"):
                    a["busy_since"] = now
                    _mark()
            elif a.get("busy_since"):
                a["idle_n"] = a.get("idle_n", 0) + 1
                if a["idle_n"] >= DONE_CONFIRM:
                    dur = now - a["busy_since"]
                    a["busy_since"] = 0
                    a["idle_n"] = 0
                    if dur >= TURN_MIN_S:
                        _turn_done(S, r, a, dur, now)
                _mark()
            # ⛔ 10-18: a turn whose end never reached the transcript (an API
            # error, a killed process, a compaction nobody answered) kept the
            # clock open, and the next prompt ran on it. A session quiet this
            # long has no turn running.
            if not busy and a.get("turn_open"):
                if not a.get("quiet_since"):
                    a["quiet_since"] = now
                    _mark()
                elif now - a["quiet_since"] > TURN_QUIET_S:
                    a["turn_open"] = False
                    a["turn_end"] = a["quiet_since"]
                    _mark()
            dg = r.get("digest") or {}
            if (dg.get("ctx") or 0) >= 500000:
                _unlock(S, "big_brain")
        # a session that left the live set mid-turn did not finish it
        live_ids = set(r["uuid"] for r in rows)
        for uuid, a in S["agents"].items():
            if uuid not in live_ids and a.get("busy_since"):
                a["busy_since"] = 0
                a["idle_n"] = 0
                _mark()
        if busy_n > d["peak"]:
            d["peak"] = busy_n
            _mark()
        if busy_n >= 3:
            _unlock(S, "juggler")
        if busy_n >= 6:
            _unlock(S, "octopus")
        if busy_n >= 10:
            _unlock(S, "swarm")
        _check_quests(S)
    _save()


def _turn_done(S, r, a, dur, now):
    if a.get("human_ts", 0) <= a.get("last_done", 0) and a.get("human_ts", 0) <= a.get("ping", 0):
        # nobody asked for this turn: a background task woke the agent up
        a["ping"] = now
        return
    xp = 10 + min(30, int(dur / 60) * 2)
    _gain(S, xp)
    a["xp"] = a.get("xp", 0) + 15 + min(30, int(dur / 60) * 2)
    a["turns"] = a.get("turns", 0) + 1
    a["last_done"] = now
    _day(S)["turns"] += 1
    _flow_add(S, 5)
    dg = r.get("digest") or {}
    mins = int(dur // 60)
    _event(S, "turn", xp, sid=r["uuid"], title=_title(r),
           text=(dg.get("last_assistant") or "")[:180],
           icon="✅" if mins < 30 else "🏆")
    if dur >= 1800:
        _unlock(S, "marathon")


def watcher():
    def loop():
        while True:
            try:
                _pass()
            except Exception as exc:
                import sys
                sys.stderr.write("[game] pass failed: %r\n" % (exc,))
                sys.stderr.flush()
            active = time.time() - _CLIENT_TS[0] < CLIENT_ACTIVE_S
            time.sleep(POLL_ACTIVE_S if active else POLL_IDLE_S)
    t = threading.Thread(target=loop, name="rc-game", daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ API

_ROW_KEYS = ("uuid", "short", "title", "name", "summary", "live", "retired", "state",
             "busy", "busy_age", "la_ts", "project", "empty", "pid", "effort", "model")


def _compact(r):
    out = dict((k, r.get(k)) for k in _ROW_KEYS)
    dg = r.get("digest") or {}
    out["tui"] = (r.get("tui") or {}).get("kind") or ""
    out["waiting"] = dg.get("waiting") or ""
    out["last_a"] = (dg.get("last_assistant") or "")[:220]
    out["last_u"] = (dg.get("last_user") or "")[:160]
    out["ctx"] = dg.get("ctx") or 0
    out["ctx_max"] = dg.get("ctx_max") or 0
    out["model_id"] = dg.get("model_id") or ""
    out["name"] = (out.get("name") or "")[:90]
    out["summary"] = (out.get("summary") or "")[:200]
    return out


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")


def _reset_ts(text):
    """«Oct 15 at 6:40pm (Asia/Nicosia)» → epoch. The zone in brackets is the
    mini's own, so a naive local datetime is right."""
    m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)",
                 text or "")
    if not m or m.group(1) not in _MONTHS:
        return 0
    hr = int(m.group(3)) % 12 + (12 if m.group(5) == "pm" else 0)
    now = datetime.now()
    try:
        dt = datetime(now.year, _MONTHS.index(m.group(1)) + 1, int(m.group(2)), hr,
                      int(m.group(4) or 0))
    except ValueError:
        return 0
    if (now - dt).days > 200:
        dt = dt.replace(year=now.year + 1)
    return dt.timestamp()


def api(since=0, fleet=None, snap=None):
    """snap: a PEER's /api/state fetched by the hub (17.11 device switch in HQ). The board, limits and accounts are
    then that machine's; the pilot (XP, trophies, log) stays this one's. No fleet trails and no disk for a peer:
    both are read from local files."""
    remote = snap is not None
    _CLIENT_TS[0] = time.time()
    if not remote:
        snap = snapshot_cached(4.0)
    now = time.time()
    with LOCK:
        S = _load()
        lv, lo, hi = level_for(S["xp"])
        v = _flow(S, now)
        d = dict(_day(S))
        touched = list(_touched(S))
        evs = [e for e in S["events"] if e["id"] > since] if since else S["events"][-40:]
        agents = {}
        for uuid, a in S["agents"].items():
            alv, alo, ahi = level_for(a.get("xp", 0), 25)
            agents[uuid[:8]] = {"lv": alv, "xp": a.get("xp", 0), "lo": alo, "hi": ahi,
                                "turns": a.get("turns", 0),
                                "done": a.get("last_done", 0), "cmd": a.get("last_cmd", 0),
                                "hum": a.get("human_ts", 0), "ping": a.get("ping", 0),
                                "turn_start": a.get("turn_start", 0) if a.get("turn_open") else 0,
                                "busy_since": a.get("busy_since", 0),
                                "asking": bool(a.get("asking"))}
        out = {"ok": True, "now": now, "seq": S["seq"], "xp": S["xp"], "lv": lv,
               "lo": lo, "hi": hi, "rank": rank_for(lv), "next_rank": next(
                   (("%s at LV %d" % (nm, at)) for at, nm in RANKS if at > lv), ""),
               "flow": round(v, 1), "mult": _mult(v), "streak": 0,
               "today": d, "quests": [],
               "ach": [{"k": k, "i": i, "t": t, "d": dd, "ts": S["ach"].get(k, 0)}
                       for k, i, t, dd in ACHIEVEMENTS],
               "events": evs[-60:], "agents": agents,
               "totals": S["totals"], "fresh_s": FRESH_S, "quick_s": QUICK_S}
    rows = snap["rows"]
    live = [_compact(r) for r in rows if r.get("live")]
    sleeping = [r for r in rows if not r.get("live") and not r.get("retired")
                and not r.get("empty")]
    sleeping.sort(key=lambda r: -(r.get("la_ts") or 0))
    out["rows"] = live
    if fleet is not None and not remote:
        out["fleet"] = trail_out(fleet, [r for r in rows if r.get("live")])
    out["remote"] = remote
    out["sleeping"] = [_compact(r) for r in sleeping[:30]]
    out["sleeping_n"] = len(sleeping)
    out["retired_n"] = sum(1 for r in rows if r.get("retired"))
    m = snap["meta"]
    out["meta"] = dict((k, m.get(k)) for k in (
        "live_total", "cap", "busy", "load1", "load_gate", "swap_mb", "swap_total_mb",
        "mem_mb", "used_mb", "pressure", "limits", "usage_ts", "usage_stale", "ts",
        "account", "host", "accounts"))
    lim = m.get("limits") or {}
    out["resets"] = {"session": _reset_ts(lim.get("session_resets")),
                     "week": _reset_ts(lim.get("week_resets"))}
    out["app_n"] = len(snap.get("app") or [])
    # the launch bay: the same models and efforts the classic draft offers,
    # preset to what the most recent session runs
    # project names for the picker in the agent bar: the rules, then any manual ones
    names = []
    for rule in ([] if remote else (C.projects() or [])):
        nm = (rule.get("name") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    for r in list(rows) + list(sleeping):
        nm = (r.get("project") or "").strip()
        if nm and nm != C.NO_PROJECT and nm not in names:
            names.append(nm)
    out["projects"] = names
    out["models"] = [["", "CLI default"]] + [list(x) for x in C.MODEL_CHOICES]
    out["efforts"] = [""] + list(C.EFFORTS)
    recent = sorted((r for r in rows if r.get("live")), key=lambda r: -(r.get("la_ts") or 0))
    known = set(x[0] for x in C.MODEL_CHOICES)
    out["model_default"] = next(((r.get("digest") or {}).get("model_id") for r in recent
                                 if (r.get("digest") or {}).get("model_id") in known), "")
    out["effort_default"] = next((r.get("effort") or (r.get("digest") or {}).get("effort_now")
                                  for r in recent if r.get("effort")
                                  or (r.get("digest") or {}).get("effort_now")), "")
    # life support: the disk too, renders fill it (10-14 ENOSPC)
    try:
        if remote:
            raise OSError("a peer's disk is not ours to stat")
        st = os.statvfs("/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/")
        out["meta"]["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        out["meta"]["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
    except OSError:
        pass
    return out
