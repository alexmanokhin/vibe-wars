#!/usr/bin/env python3
"""rc_load_guard — sheds guardian-restored sessions before the box wedges.

Why this exists, separately from rc_guardian
--------------------------------------------
On 2026-08-05 the mini held 27 live sessions on 8 GB / 8 cores, swap filled to
4.7 of 6 GB, load hit 54, and the box stopped responding entirely from 12:41
until a hard reset at 13:51:44. Nothing paged anyone: rc_guardian was itself
frozen, so its own Telegram path was dead too. the owner noticed the box was down
eight hours later.

rc_guardian's brakes only decide whether to ADD a session. Nothing ever removed
one. This does the removing, and it lives in its own launchd agent precisely so
that a wedged guardian cannot take the alarm down with it.

⛔ Disk is NOT the trigger. Measured during the incident: 85 GiB free the whole
time, while swap grew 6 → 7 → 8 GB. A disk watcher would have stayed silent all
day. Disk is checked only as a secondary guard, because the swapfiles live on
/System/Volumes/VM and swap cannot grow on a full disk.

What it kills: only sessions rc_guardian restored (`--resume <uuid>
--remote-control`). App sessions are children of the RC daemon and are the
user's real phone/web chats; those are never touched. Killing a restored
session loses nothing — the transcript is on disk, the row stays in RC_LIVE.md
for 7 days, and the guardian brings it back when the box is calm again.
"""

import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
# Та же выводимость, что у PROJ в гардиане: литеральное «-Users-olduser»
# на машине с другим пользователем указывало бы в пустую папку, и сторож
# нагрузки не увидел бы НИ ОДНОЙ сессии — то есть молча перестал бы
# работать ровно тогда, когда он нужен. realpath — потому что рабочий
# каталог резолвится физически.
_WORKDIR = os.path.realpath(os.environ.get("RC_WORKDIR") or HOME)
PROJECTS = CFG.PROJ
STATE = CFG.state("rc-load-guard-state.json")
LOG = CFG.state("rc-load-guard.log")
METRICS = CFG.state("rc-load-guard-metrics.csv")
TG = os.path.join(CFG.HERE, "rc_notify.py")
# ⛔ 10-18: the owner turned this guard's pushes off: dozens a day, none he acts on.
# Cleanup and shedding still run, and every event is still logged right before
# its alert() call. While this file exists nothing is sent; rm it to page again.
MUTE = CFG.state("rc-load-guard.mute")
CLAUDE_BIN = CFG.CLAUDE

# Порт под Linux: те же помощники, что в rc_guardian.py. Здесь они повторены,
# а не импортированы — сторож намеренно не зависит от гардиана, чтобы
# зависший гардиан не утащил за собой и тревогу.
IS_LINUX = sys.platform.startswith("linux")
# На Linux ps смотрит только на своего пользователя: рядом чужие процессы
# (см. PS в гардиане). На macOS остаётся -A.
PS = f"ps -u {os.getuid()} -o" if IS_LINUX else "ps -Ao"
# Имя машины в тревогах; на мини так и осталось «Mac mini».
BOX = os.uname().nodename.split(".")[0] if IS_LINUX else "Mac mini"

# --- thresholds -------------------------------------------------------------
# ⛔ Absolute swap is NOT a trigger. It was, at 4500 MB, and that number is
# inside this box's ordinary working band, so the guard sat in breach almost all
# day and the only thing keeping sessions alive was the warm-transcript rule.
# Measured over 1086 samples on 08-06 (rc-load-guard-metrics.csv, kept as
# rc-load-guard-metrics.v1.csv):
#   swap p10 3707 / p50 4955 / p90 5920 / max 7640 MB — 71.7% of ALL samples
#   were above the 4500 "limit", and the box was responsive at every one of
#   them, including 7640.
# It also does not respond to the remedy: the 18:27 shed killed 3 sessions at
# 4593 MB and swap then read 4888 → 4902 → 4982. Killing sessions did not move
# it. macOS swap-used is a slowly-draining high-water mark (~5 MB/min), not a
# pressure signal, so it can neither confirm an emergency nor confirm a fix.
# Swap is still measured and logged for calibration; it just cannot kill.
#
# What actually distinguished the 08-05 wedge: load 54 on 8 cores (6-14x) while
# the box refused input. Load above 3x cores covers 5.9% of a normal day, and
# only in short spikes, which BREACH_RUNS handles.
# Killing is a LAST RESORT, not a routine control, and the measurements say so:
# the two restored sessions on the box hold 77 + 47 MB against 36 claude
# processes totalling 1688 MB. A full shed buys back ~1.5% of an 8 GB box. What
# actually prevents the 08-05 wedge is rc_guardian's MAX_LIVE cap (13, was 24
# that day, and the box died at 27 live) — admission control, not eviction.
# So a shed needs a real wedge, which means TWO agreeing signals:
LOAD_PER_CPU = 3.0          # 1-min load above 3x cores, AND pressure critical
LOAD_PER_CPU_ALONE = 5.0    # or load above 5x cores on its own. Measured over a
                            # normal day: >3x = 5.9% of samples, >5x = 1.3% and
                            # never for more than 4 consecutive minutes. The
                            # wedge held 6-14x for 70 minutes.
PRESSURE_CRITICAL = 4       # kern.memorystatus_vm_pressure_level: 1 normal,
                            # 2 warn, 4 critical. ⛔ 2 is this box's baseline
                            # (read 2 at load 2.8, everything idle), so 4 is the
                            # only reading worth anything — and never on its own,
                            # it is a transient kernel state.
DISK_MIN_GIB = 10           # swapfiles cannot grow past this
DISK_HARD_GIB = 3           # below this, shed whatever the memory says
DISK_SWAP_MB = 4000         # swap past this counts as memory being tight

# A spike is not an emergency. Require the breach to survive five consecutive
# 60s passes — longer than any load excursion measured on a normal day.
BREACH_RUNS_BEFORE_SHED = 5
MAX_KILL_PER_RUN = 3        # shed gradually, re-measure next pass
WARM_MIN = 30               # never kill a session whose transcript was written
                            # in the last 30 min: that is work in flight. The
                            # guardian uses 10 for restores; killing is the
                            # irreversible direction, so it gets the wider
                            # margin — 11-13 min idle sessions that got shed on
                            # 08-06 were all live work the owner came back to.
MIN_KEEP = 2                # floor, so a freak reading cannot empty the board
SHED_COOLDOWN_MIN = 15      # after a shed, alert instead of killing again until
                            # the box has had time to actually recover. Stops
                            # the 18:27 → 18:32 cascade where a second shed
                            # fired while the first had not helped at all.
WARM_ALERT_EVERY_MIN = 180  # ⛔ 10-09: "every session is warm" re-paged every
                            # 15 min for two days on a disk-only breach that no
                            # shed can fix. Say it, then repeat every 3 h.


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout
    except subprocess.SubprocessError:
        return ""


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def alert(text):
    if os.path.exists(MUTE):
        return
    if os.path.exists(TG):
        subprocess.run([sys.executable, TG, text], capture_output=True, timeout=30)


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        with open(STATE, "w") as fh:
            json.dump(st, fh, indent=2)
    except OSError:
        pass


# --- measurements ----------------------------------------------------------

def _meminfo():
    """/proc/meminfo как {ключ: КБ}. Только Linux; пусто, если не прочитался."""
    out = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key.strip()] = int(parts[0])
    except OSError:
        pass
    return out


def swap_used_mb():
    if IS_LINUX:
        mi = _meminfo()
        return float((mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) // 1024)
    m = re.search(r"used = ([\d.]+)M", sh("sysctl vm.swapusage"))
    return float(m.group(1)) if m else 0.0


def load1():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def pressure_level():
    """1 normal, 2 warn, 4 critical. Missing sysctl reads as normal.

    На Linux — из PSI (/proc/pressure/memory), та же шкала, что у гардиана:
    full avg10 от 20% — critical, some avg10 от 10% — warn."""
    if IS_LINUX:
        try:
            with open("/proc/pressure/memory") as fh:
                txt = fh.read()
        except OSError:
            return 1
        full = re.search(r"full avg10=([\d.]+)", txt)
        some = re.search(r"some avg10=([\d.]+)", txt)
        if full and float(full.group(1)) >= 20.0:
            return 4
        if some and float(some.group(1)) >= 10.0:
            return 2
        return 1
    out = sh("sysctl -n kern.memorystatus_vm_pressure_level").strip()
    try:
        return int(out)
    except ValueError:
        return 1


def swapouts():
    """Cumulative pages swapped out. The DELTA is the thrash signal — logged for
    calibration, not yet a trigger (no clean-day baseline exists for it)."""
    if IS_LINUX:
        try:
            with open("/proc/vmstat") as fh:
                for line in fh:
                    if line.startswith("pswpout "):
                        return int(line.split()[1])
        except OSError:
            pass
        return 0
    m = re.search(r"Swapouts:\s+(\d+)", sh("vm_stat"))
    return int(m.group(1)) if m else 0


def disk_free_gib():
    """Absolute GiB on the data volume, never a percentage — a percentage on a
    460 GiB disk hides the only number that matters."""
    if IS_LINUX:
        try:
            st = os.statvfs(HOME)
            return int(st.f_bavail * st.f_frsize) >> 30
        except OSError:
            return 999
    out = sh("df -g /System/Volumes/Data").splitlines()
    if len(out) >= 2:
        parts = out[1].split()
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3])
    return 999


def breach_reasons():
    reasons = []
    ld = load1()
    cores = os.cpu_count() or 8
    lvl = pressure_level()
    if ld > LOAD_PER_CPU * cores and lvl >= PRESSURE_CRITICAL:
        reasons.append(f"load {ld:.1f} on {cores} cores + pressure CRITICAL ({lvl})")
    elif ld > LOAD_PER_CPU_ALONE * cores:
        reasons.append(f"load {ld:.1f} on {cores} cores (>{LOAD_PER_CPU_ALONE:.0f}x)")
    gib = disk_free_gib()
    # Low disk matters to memory because the swapfiles cannot grow into it.
    # ⛔ 10-16: under 10 GiB this shed an idle session every 15 min even with the
    # kernel pressure normal and 1 GB of swap — nothing to protect, and each shed
    # archives the card in the app («сессии ушли в архив»). Shed for disk only
    # when memory is actually tight, or when the disk is close to the end.
    tight = lvl >= 2 or swap_used_mb() > DISK_SWAP_MB
    if gib < DISK_HARD_GIB or (gib < DISK_MIN_GIB and tight):
        reasons.append(f"disk {gib} GiB free (limit {DISK_MIN_GIB}"
                       + (", memory tight" if gib >= DISK_HARD_GIB else "") + ")")
    return reasons


# --- sessions --------------------------------------------------------------

def restored_sessions():
    """[(uuid, pid, idle_seconds)] for guardian-restored sessions only.

    NOT pgrep: it intermittently fails to read the argv of launchd-spawned
    processes on this box (see rc_guardian.ps_lines). ps -A always sees them.
    """
    out = []
    for line in sh(f"{PS} pid=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if not m:
            continue
        pid, args = int(m.group(1)), m.group(2)
        if "--remote-control" not in args or "--resume" not in args:
            continue
        # Anchor on the START of argv. A substring test double-counts every
        # session: the `script -q LOG /…/bin/claude --resume …` wrapper carries
        # the claude path inside its own argv, so "/bin/claude" in args matches
        # the wrapper too (caught in dry-run: 4 sessions counted as 8, one uuid
        # listed twice as a kill victim).
        if not args.startswith(CLAUDE_BIN):
            continue
        u = re.search(r"--resume\s+([0-9a-f-]{36})", args)
        if not u:
            continue
        uuid = u.group(1)
        path = os.path.join(PROJECTS, f"{uuid}.jsonl")
        try:
            idle = time.time() - os.path.getmtime(path)
        except OSError:
            idle = float("inf")           # no transcript: nothing in flight
        out.append((uuid, pid, idle))
    return out


def kill_session(uuid, pid):
    """TERM the claude process and the `script` wrapper's process group.

    The restore shape is `( printf 1; sleep loop ) | script -q LOG claude
    --resume ...`, so the wrapper's group holds the stdin-holder too. Leaving it
    behind orphans a sleep loop per session.
    """
    subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
    for line in sh(f"{PS} pid=,pgid=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if m and f"--resume {uuid}" in m.group(3) and m.group(3).startswith("script"):
            subprocess.run(["kill", "-TERM", f"-{m.group(2)}"], capture_output=True)


HEADER = "ts,swap_mb,load1,disk_gib,restored,pressure,swapout_per_min\n"


def record_metrics(n_restored, st):
    """One CSV row per pass. The thresholds above were set from a single bad day
    and a single good hour; this is what lets them be set from real data."""
    now = time.time()
    outs = swapouts()
    prev, prev_ts = st.get("swapouts"), st.get("swapouts_ts")
    rate = ""
    if prev is not None and prev_ts and now > prev_ts and outs >= prev:
        rate = f"{(outs - prev) / ((now - prev_ts) / 60):.0f}"
    st["swapouts"], st["swapouts_ts"] = outs, now
    try:
        # v1 rows have fewer columns; rotate rather than append a ragged file.
        if os.path.exists(METRICS):
            with open(METRICS) as fh:
                if fh.readline() != HEADER:
                    os.rename(METRICS, METRICS.replace(".csv", ".v1.csv"))
        new = not os.path.exists(METRICS)
        with open(METRICS, "a") as fh:
            if new:
                fh.write(HEADER)
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{swap_used_mb():.0f},"
                     f"{load1():.2f},{disk_free_gib()},{n_restored},"
                     f"{pressure_level()},{rate}\n")
    except OSError:
        pass


# ⛔ 10-02: the disk ran out at 19:10 and stayed at 0 for an hour. Every write
# failed: the panel could not create sessions (19 ENOSPC on /act new), the
# guardian and this guard could not log, 85d2fa4f could not save its transcript
# and silently dropped every message typed into it. The only page all day was
# the generic "under pressure ... every session is warm", which reads as load
# noise. The culprit was a another project cron writing 7k `claude -p` transcripts
# a day (35 GB). So disk gets its own page: the number, and what is big.
DISK_STEPS = (10, 5, 2, 1)      # page once on crossing each step down
DISK_REPAGE_MIN = 60            # below the last step, page again hourly
DISK_SUSPECTS = ("~/.claude/projects/*", "~/Library/Caches",
                 "~/Downloads", "/tmp")


def disk_hogs(limit=6):
    """Biggest dirs among the usual suspects. Only runs when paging (du is slow)."""
    import glob
    paths = []
    for pat in DISK_SUSPECTS:
        paths += glob.glob(os.path.expanduser(pat))
    paths = [p for p in paths if os.path.isdir(p)]
    if not paths:
        return []
    try:
        r = subprocess.run(["du", "-sk"] + paths, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in r.stdout.splitlines():
        kb, _, path = line.partition("\t")
        if kb.isdigit():
            rows.append((int(kb) >> 20, path.replace(HOME, "~")))
    rows.sort(reverse=True)
    return [f"{gb} GB {path}" for gb, path in rows[:limit] if gb >= 1]


# ⛔ 10-14: the disk sat at 1 GiB from 05:18 to 15:21 and the guard only paged
# and shed sessions; shedding frees no disk. Segment renders of the reels
# pipeline are rebuilt by the next render, so below DISK_STEPS[0] they go first
# (only files untouched for CLEAN_MIN_AGE_H, so a render in flight keeps its own).
CLEAN_GLOBS = ()  # add globs of disposable build output here
CLEAN_MIN_AGE_H = 2             # file age, and its dir must be idle CLEAN_DIR_IDLE_MIN
CLEAN_DIR_IDLE_MIN = 60
CLEAN_EVERY_MIN = 30
# ⛔ 10-14 evening: back to 2 GiB four hours after a 6 GiB clean (renders write
# ~1.5 GiB/h), and sessions failed again on ENOSPC. Cleaning at 10 GiB is too
# late, so the janitor starts at 25 GiB and also drops local copies of reels
# published > 7 days ago that Supabase holds byte for byte (3 days below 10 GiB).
JANITOR_BELOW_GIB = 25
OFFLOAD = ""  # optional offload script; none in this edition


def clean_regenerable(st):
    """Delete old regenerable render intermediates. Returns bytes freed."""
    if (time.time() - st.get("disk_clean_ts", 0)) / 60 < CLEAN_EVERY_MIN:
        return 0
    st["disk_clean_ts"] = time.time()
    save_state(st)
    import glob
    cutoff = time.time() - CLEAN_MIN_AGE_H * 3600
    freed = files = 0
    for pat in CLEAN_GLOBS:
        for top in glob.glob(pat):
            newest = 0
            for root, _dirs, names in os.walk(top):
                for n in names:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, n)))
                    except OSError:
                        pass
            if time.time() - newest < CLEAN_DIR_IDLE_MIN * 60:
                continue                    # a render is writing here
            for root, _dirs, names in os.walk(top):
                for n in names:
                    fp = os.path.join(root, n)
                    try:
                        if os.path.islink(fp) or os.path.getmtime(fp) > cutoff:
                            continue
                        size = os.path.getsize(fp)
                        os.unlink(fp)
                        freed += size
                        files += 1
                    except OSError:
                        pass
    if files:
        log(f"disk clean: removed {files} render intermediates, "
            f"{freed / 2**30:.1f} GiB (older than {CLEAN_MIN_AGE_H}h)")
    if os.path.exists(OFFLOAD):
        # ⛔ 10-16: 18 GiB on the evening of 10-15 fell to 3 GiB by 17:00 the
        # next day (a day of renders and translations), and 3-day offloading
        # freed nothing. Under 5 GiB, copies published a day ago go too.
        free = disk_free_gib()
        days = 1 if free < 5 else (3 if free < DISK_STEPS[0] else 7)
        before = disk_free_gib()
        try:
            r = subprocess.run(["/usr/bin/python3", OFFLOAD, "--days", str(days)],
                               capture_output=True, text=True, timeout=900)
            out = (r.stdout or r.stderr or "").strip().splitlines()
            log("disk offload: " + (out[-1] if out else f"rc {r.returncode}"))
        except Exception as exc:
            log(f"disk offload failed: {exc}")
        freed += max(0, disk_free_gib() - before) << 30
    return freed


def disk_watch(st):
    gib = disk_free_gib()
    if gib < JANITOR_BELOW_GIB:
        freed = clean_regenerable(st)
        if freed >= 1 << 30:
            gib = disk_free_gib()
            alert(f"🧹 {BOX} disk janitor: freed {freed / 2**30:.0f} GiB (reel render "
                  f"intermediates + local copies of old published reels, all in "
                  f"Supabase), now {gib} GiB free.")
    if gib >= DISK_STEPS[0] + 2:
        if st.get("disk_step"):
            st["disk_step"] = 0
            save_state(st)
        return
    step = min((x for x in DISK_STEPS if gib < x), default=None)
    if step is None:
        return
    last = st.get("disk_step") or 999
    stale = (time.time() - st.get("disk_alert_ts", 0)) / 60 >= DISK_REPAGE_MIN
    if step >= last and not (step == DISK_STEPS[-1] and stale):
        return
    st["disk_step"] = step
    st["disk_alert_ts"] = time.time()
    save_state(st)
    hogs = disk_hogs()
    log(f"disk page: {gib} GiB free; " + "; ".join(hogs))
    alert(f"💾 {BOX} disk: {gib} GiB free. At 0 nothing can write: new sessions "
          f"fail, open sessions drop messages, logs stop.\nBiggest:\n"
          + "\n".join(hogs or ["(du failed)"]))


# ⛔ 10-15: sessions start preview servers with nohup/& and never stop them.
# They outlive the session (reparented to launchd), and nothing reaped them:
# nine `python -m http.server` from the 10-10 cartoon sessions were still up
# 4 days later, next to a 13-day `python -` hung on a dead HTTPS socket and an
# 18-day `brew list`. Only preview servers in work dirs are touched, never
# services: orphaned, older than DEV_SERVER_MAX_H, no open TCP client.
DEV_SERVER_MAX_H = 48
DEV_SERVER_RE = re.compile(r"(-m http\.server\b|\bnode (\S*/)?serve\.mjs\b)")
DEV_SERVER_DIRS = (CFG.WORKDIR + "/",
                   "/private/tmp/claude-")


def _etime_s(et):
    days, _, hms = et.rpartition("-")
    parts = [int(x) for x in hms.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def reap_stale_dev_servers(st, dry=False):
    if (time.time() - st.get("dev_reap_ts", 0)) < 3600:
        return
    st["dev_reap_ts"] = time.time()
    save_state(st)
    out = sh("ps -axo pid=,ppid=,etime=,command=")
    gone = []
    for line in out.splitlines():
        f = line.split(None, 3)
        if len(f) < 4 or f[1] != "1" or not DEV_SERVER_RE.search(f[3]):
            continue
        try:
            if _etime_s(f[2]) < DEV_SERVER_MAX_H * 3600:
                continue
        except ValueError:
            continue
        pid = f[0]
        cwd = ""
        for row in sh(f"lsof -a -p {pid} -d cwd -Fn").splitlines():
            if row.startswith("n"):
                cwd = row[1:]
        if not cwd.startswith(DEV_SERVER_DIRS):
            continue
        if sh(f"lsof -a -p {pid} -iTCP -sTCP:ESTABLISHED -Fn").strip():
            continue                        # someone is looking at it right now
        if not dry:
            try:
                os.kill(int(pid), 15)
            except OSError:
                continue
        gone.append(f"{pid} {cwd.replace(HOME, '~')}")
    if gone:
        log(("would stop" if dry else "stopped") + f" {len(gone)} orphaned preview "
            f"server(s) older than {DEV_SERVER_MAX_H}h: " + "; ".join(gone))


def main():
    dry = "--dry-run" in sys.argv
    st = load_state()
    record_metrics(len(restored_sessions()), st)
    if not dry:
        disk_watch(st)
    reap_stale_dev_servers(st, dry)
    reasons = breach_reasons()

    if not reasons:
        if st.get("breaches"):
            log(f"recovered — swap {swap_used_mb():.0f} MB, load {load1():.1f}")
        st["breaches"] = 0
        save_state(st)
        return

    st["breaches"] = st.get("breaches", 0) + 1
    save_state(st)
    why = "; ".join(reasons) + f" [swap {swap_used_mb():.0f} MB]"

    if st["breaches"] < BREACH_RUNS_BEFORE_SHED:
        log(f"breach {st['breaches']}/{BREACH_RUNS_BEFORE_SHED}: {why} — watching")
        return

    since = (time.time() - st.get("last_shed", 0)) / 60
    if since < SHED_COOLDOWN_MIN:
        log(f"{why} — still in cooldown ({since:.0f}/{SHED_COOLDOWN_MIN}m since "
            f"last shed), not killing")
        alert(f"⚠️ {BOX} still under pressure ({why}) {since:.0f} min after a "
              f"shed. Shedding did not help — look at what is actually eating "
              f"the box.")
        return

    sessions = restored_sessions()
    # Longest-idle first. Idle is measured from the transcript, not from launch
    # time: a session started this morning that is mid-deploy right now is the
    # last thing to kill, and "oldest process" would have picked it first.
    sessions.sort(key=lambda s: -s[2])
    victims = [s for s in sessions if s[2] > WARM_MIN * 60]
    spare = max(0, len(sessions) - MIN_KEEP)
    victims = victims[:min(MAX_KILL_PER_RUN, spare)]

    if not victims:
        log(f"{why} — {len(sessions)} restored, none idle >{WARM_MIN}m, nothing to shed")
        # Throttled: this path re-fires every 60s for as long as the breach
        # lasts, and the old code paged on every one of them.
        if (time.time() - st.get("last_warm_alert", 0)) / 60 >= WARM_ALERT_EVERY_MIN:
            st["last_warm_alert"] = time.time()
            save_state(st)
            alert(f"⚠️ {BOX} under pressure ({why}) but every restored session "
                  f"is warm — cannot shed automatically. {len(sessions)} restored live.")
        return

    names = []
    for uuid, pid, idle in victims:
        names.append(f"{uuid[:8]} (idle {idle/60:.0f}m)")
        if not dry:
            kill_session(uuid, pid)
            # read by rc_guardian.apply_archive: a killed session's card gets
            # archived in the app by itself, and that must not retire it
            shed = st.setdefault("shed_uuids", {})
            shed[uuid] = time.time()
            for k in [k for k, v in shed.items() if time.time() - v > 86400]:
                shed.pop(k, None)
    verb = "would kill" if dry else "killed"
    log(f"{why} — {verb} {len(victims)} of {len(sessions)} restored: {', '.join(names)}")
    if not dry:
        alert(f"🧹 {BOX} shed {len(victims)} idle session(s): {why}\n"
              + "\n".join(names)
              + "\nAll stay in RC_LIVE.md and come back when the box is calm.")
        st["breaches"] = 0
        st["last_shed"] = time.time()
        save_state(st)


if __name__ == "__main__":
    main()
