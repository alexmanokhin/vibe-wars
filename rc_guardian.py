#!/usr/bin/env python3
"""
rc-guardian — keeps Remote Control sessions alive, and resurrects them in the
Claude app when they die.

Why this exists (2026-08-03): the RC daemon logs
    "Error: Server unreachable for 13 minutes, giving up."
    "Shutting down 20 active session(s)..."
and every live session dies. The app chats are bound to the daemon's env_ id,
which is re-minted on every restart, so those chats can never be revived in
place. But every RC session writes an ordinary local transcript, and
`claude --resume <uuid> --remote-control "<name>"` brings the whole
conversation back as a live session in the app.

One invocation = one pass. Run it from launchd every 60s.

  census        find RC transcripts (entrypoint=sdk-cli), name them, register them
  detect        read new bytes of claude-rc.log for a wipe event
  restore       bring victims back, capped so an 8 GB box does not thrash
  snapshot      always leave a readable list of the working set on disk
  migrate       on account switch, re-materialise the working set under the new one

Nothing here restores a session the user closed on purpose: restores fire only
on a daemon-level wipe (mass session failure, "Shutting down", daemon restart)
or an explicit --migrate / --restore-all.

Hand edits (each takes the pass lock, so they cannot be clobbered mid-cycle):

  --rename <uuid|prefix> <new name>   set the card name used on the next restore
  --retire <uuid|prefix>              drop it from the queue and from RC_LIVE.md
  --unretire <uuid|prefix>            put it back

Archiving a chat in the app retires it (retired_by "app-archive"), and
unarchiving it un-retires it again — see sync_archive(). Archiving leaves no
local trace, so this is read from claude.ai every 5 min while the account is
signed in and cached in the registry: after a switch the old account's state
can no longer be read. A manual --retire is never undone by the app.
"""

import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
# Рабочий каталог сессий и папка их транскриптов. Claude Code берёт имя папки
# из cwd, заменяя слэши дефисами, — раньше тут стояло литеральное
# «-Users-olduser», и на машине с другим пользователем census смотрел бы в
# пустоту: ни одной сессии ни в панели, ни в очереди. Выглядит как поломка, а
# не как «смотрит не туда», поэтому имя выводится из HOME.
#
# realpath, а НЕ сам HOME: рабочий каталог резолвится физически (проверено —
# os.getcwd() через симлинк отдаёт настоящий путь). Если home окажется
# симлинком, cd по нему и слаг папки разъехались бы, и census снова смотрел бы
# мимо.
#
# ⛔ И это НЕ обязательно домашний каталог. Claude Code 2.1.258 отказывается
# работать в home: «home-directory trust is never saved, so running `claude`
# here first won't help». Демон на основной машине падал с этим четыре раза
# подряд, пока рабочим каталогом не стал ~/rc-work. На мини (2.1.257) старая
# запись доверия для /Users/olduser ещё действует — то есть её связка
# сломается ровно в тот день, когда CLI там обновится.
#
# Каталог задаётся переменной RC_WORKDIR; из него же выводится и имя папки с
# транскриптами — иначе census смотрел бы не туда, куда пишет claude.
WORKDIR = CFG.WORKDIR
PROJ = CFG.PROJ
REG_PATH = CFG.state("rc-registry.json")
STATE_PATH = CFG.state("rc-guardian-state.json")
NEW_UUIDS_PATH = CFG.state("rc-new-uuids.json")
LOG_PATH = CFG.state("rc-guardian.log")
# --- платформа -------------------------------------------------------------
# Порт под Linux (a Linux VPS). Ветка Linux включается только по sys.platform, на
# macOS всё ниже ведёт себя ровно как раньше. Проверки собраны здесь и в
# нескольких помощниках (_meminfo, swap_used_mb, pressure_level, free_mb,
# connected_pids, kickstart_daemon, script_cmd, spawn_detached), а не
# размазаны по коду, чтобы их можно было пересчитать.
IS_LINUX = sys.platform.startswith("linux")
# Логи демона: на macOS в ~/Library/Logs пишет плист, на Linux в ~/.claude/logs
# пишет systemd-юнит (см. make_units.py). Путь должны знать оба: гардиан читает
# его ради обнаружения массовой смерти сессий, health-скрипт обрезает.
LOGDIR = os.path.join(HOME, ".claude/logs" if IS_LINUX else "Library/Logs")
RCLOG = os.path.join(LOGDIR, "claude-rc.log")
# Служба демона: launchd-ярлык на macOS, пользовательский юнит systemd на Linux.
DAEMON_LABEL = CFG.LABEL_PREFIX + ".remote-control"
DAEMON_UNIT = CFG.UNIT_PREFIX + "-remote-control.service"
# Only this user's processes: on a shared machine another user's claude is not
# ours (its daemon and sessions would be counted as ours). On macOS `-x -U uid`
# lists launchd-spawned processes with their argv, like `-A` (pgrep may not).
PS = f"ps -u {os.getuid()} -o" if IS_LINUX else f"ps -x -U {os.getuid()} -o"
SNAP = CFG.state("RC_LIVE.md")
CLAUDE = CFG.CLAUDE
# ⛔ 09-29: macOS privacy (TCC) knows claude by its file PATH, and every update
# lands in a new ~/.local/share/claude/versions/2.1.xxx, so a Full Disk Access
# grant on it lapses at the next update (almost daily). Sessions therefore run
# a fixed copy: claude_exec() keeps an APFS clone of the newest version here.
# Every release carries the same Developer ID requirement (identifier
# com.anthropic.claude-code, team Q6L2SF6YDW), so the grant made once on this
# path keeps matching. It starts with CLAUDE, so every `startswith(CLAUDE)`
# process check still recognises it.
CLAUDE_FIXED = os.path.join(os.path.dirname(CFG.CLAUDE), CFG.NAME + "-rc")
TG = os.path.join(CFG.HERE, "rc_notify.py")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")

# --- caps: this box is 8 GB / 8 cores, each live session is 100-260 MB ---
# Потолок живых сессий — единственное, что у машин по-настоящему разное, поэтому
# он задаётся переменной окружения из plist. Держать ради одного числа вторую
# копию гардиана — верный способ развести машины по всему остальному.
#
# 13 — измерено на мини: 24 положили её 08-05 (27 живых, своп 4.7/6 ГБ, load 54,
# 71 минута без цикла гардиана, потом жёсткий ресет). 8 ГБ не держат больше ~13
# живых сессий вместе с Chrome и VPN. Второй машине число подбирать так же —
# по свободной памяти, а не по объёму установленной.
#
# Мусор в переменной не должен ронять импорт: гардиана импортируют и панель, и
# бот, и сторож нагрузки.
try:
    MAX_LIVE = int(os.environ.get("RC_MAX_LIVE") or 13)
except ValueError:
    MAX_LIVE = 13
if MAX_LIVE < 1:
    MAX_LIVE = 13
MAX_PER_RUN = 3         # stagger restores across cycles
MIN_FREE_MB = 48        # ⛔ Was 400, and that number blocked EVERY restore for
                        # 30+ hours: 195 consecutive "restore paused: only NNN MB
                        # free" between 17:12 and 20:36 on 08-06, 12 sessions
                        # pending, last successful restore 08-05 14:09. free +
                        # speculative is what macOS keeps deliberately small, so
                        # it reads 104-369 MB on a perfectly healthy box and read
                        # 70 MB during the wedge — the ranges overlap almost
                        # completely and it cannot tell the two apart. Kept only
                        # as a near-OOM floor; the real RAM signal is pressure
                        # level 4 in pressure_reason(), and the real admission
                        # control is MAX_LIVE.
MAX_SWAP_MB = 6000      # ⛔ Was 2048, which is BELOW this box's floor: the
                        # minimum swap reading across 1086 samples on a normal
                        # day was 2287 MB, i.e. 100% of the day would have
                        # blocked restores once the free_mb gate stopped
                        # shadowing it. Absolute, not a fraction of total: macOS
                        # resizes the swapfiles on demand, so used/total is
                        # pinned near 80% in every state and gates nothing.
                        # 6000 sits above the p90 (5920) of a normal day.
                        # Blocking a restore is cheap and retried every 60s, so
                        # this side can afford to be the conservative one.
MAX_LOAD_PER_CPU = 2.0  # 1-min load above 2x cores: nothing new until it drains
WORKING_WINDOW_H = 168  # snapshot/registry window: 7 days, so RC_LIVE.md keeps a
                        # week of sessions recoverable by hand. Was 48h.
RESTORE_FRESH_H = 48    # auto-restore window: what the guardian brings back by
                        # itself, on a wipe or an account switch. Deliberately
                        # narrower than the snapshot — a 7-day pool would let one
                        # wipe resurrect chats finished days ago.
NAME_MAX = 64          # only caps auto-derived names; --rename accepts any length
RESTORE_TIMEOUT = 45    # shared deadline for the whole batch to surface app links

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def boot_time():
    """When the machine last booted (epoch seconds, rounded), 0 if unknown."""
    try:
        if IS_LINUX:
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return int(line.split()[1])
            return 0
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


# A reboot (or shutdown) closes every session's card in the app, and the app
# then reports it archived: that is not the user's archive. Sessions that were
# live before a reboot are restored, and their archive edge within this window
# after the boot is ignored.
REBOOT_KEEP_S = 30 * 60


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def sh(cmd, timeout=20):
    # errors="replace": `ps` обрезает argv по ширине и рвёт многобайтный
    # символ — имена сессий русские, так что это не редкость. Строгое
    # декодирование роняло весь проход гардиана, и не ловилось except'ом ниже:
    # UnicodeDecodeError — это ValueError, а не SubprocessError (падение 08-18).
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              errors="replace", timeout=timeout).stdout
    except subprocess.SubprocessError:
        return ""


# --------------------------------------------------------------------------
# environment probes
# --------------------------------------------------------------------------

def account_id():
    d = load(CLAUDE_JSON, {})
    acc = d.get("oauthAccount") or {}
    return acc.get("emailAddress") or acc.get("accountUuid") or "unknown"


def net_ok():
    code = sh(f"curl -s --max-time 8 -o /dev/null -w '%{{http_code}}' "
              f"https://api.anthropic.com/v1/messages").strip()
    return code in {"200", "400", "401", "403", "404", "405"}


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
    """Занятый своп в МБ; -1, если не прочитался."""
    if IS_LINUX:
        mi = _meminfo()
        if "SwapTotal" in mi and "SwapFree" in mi:
            return (mi["SwapTotal"] - mi["SwapFree"]) // 1024
        return -1
    m = re.search(r"used = ([\d.]+)M", sh("sysctl vm.swapusage"))
    return int(float(m.group(1))) if m else -1


def swap_total_mb():
    """Потолок свопа в МБ; 0, если не прочитался. На macOS он плавает —
    система наращивает свопфайлы по мере надобности, на Linux это раздел."""
    if IS_LINUX:
        return _meminfo().get("SwapTotal", 0) // 1024
    m = re.search(r"total = ([\d.]+)M", sh("sysctl vm.swapusage"))
    return int(float(m.group(1))) if m else 0


def pressure_level():
    """1 normal, 2 warn, 4 critical — шкала kern.memorystatus_vm_pressure_level.

    На Linux то же число выводится из PSI (/proc/pressure/memory): `full avg10`
    — доля последних 10 секунд, когда ВСЕ задачи стояли за памятью, от 20% это
    critical; `some avg10` от 10% — warn. Нет PSI (старое ядро, контейнер) —
    normal, как и отсутствующий sysctl на macOS.
    """
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
    level = sh("sysctl -n kern.memorystatus_vm_pressure_level").strip()
    return int(level) if level.isdigit() else 1


def free_mb():
    if IS_LINUX:
        # MemAvailable — оценка ядра, сколько можно занять без свопа. Это тот
        # же смысл, что free + speculative ниже, а не MemFree, который на Linux
        # всегда мал из-за page cache.
        return _meminfo().get("MemAvailable", 0) // 1024
    out = sh("vm_stat")
    pages = {}
    for line in out.splitlines():
        m = re.match(r"Pages (\w+)[^:]*:\s+(\d+)", line)
        if m:
            pages[m.group(1)] = int(m.group(2))
    psize = 16384
    m = re.search(r"page size of (\d+)", out)
    if m:
        psize = int(m.group(1))
    # NOT inactive: on a box that is already swapping, inactive pages are dirty
    # and backed by swap, so counting them reported 1265 MB free while the real
    # figure was 70 MB. That made MIN_FREE_MB unreachable and the brake never
    # fired once (verified 08-05, the day the box wedged). Only free +
    # speculative are genuinely reclaimable without paging.
    usable = pages.get("free", 0) + pages.get("speculative", 0)
    return usable * psize // (1024 * 1024)


def pressure_reason():
    """Why the box is too loaded to take another session, or '' if it is fine.

    free_mb alone is a weak gate on macOS, which keeps the free list small by
    design. These are the signals that actually moved before the 08-05 wedge:
    swap filled, the kernel raised its pressure level, and load ran 6x cores
    while every guardian pass timed out.

    ⛔ Do NOT block on pressure level 2 alone. Measured 08-06: this box reports
    level 2 (warn) as its steady state even at load 8.9 with the working set
    trimmed, so a bare `level > 1` test never clears and would silently disable
    restores forever — which breaks the one thing the guardian exists for. Only
    level 4 (critical) is a standalone block.

    ⛔ Do NOT gate swap as used/total either. macOS grows and shrinks the
    swapfiles on demand (measured: total went 6 → 7 → 8 GB during the spiral,
    then back to 4 GB after the shed), so the ratio stays pinned near 80%
    whatever the real state is. The absolute figure against 8 GB of RAM is the
    signal that actually moves.
    """
    level = pressure_level()
    if level >= 4:                                # 1 normal, 2 warn, 4 critical
        return f"kernel memory pressure CRITICAL (level {level})"

    # ⛔ swap is NOT a gate here. Measured over 1088 samples: p50 4955 / p90 5921
    # / max 7640 MB, and the box stayed responsive at every reading. MAX_SWAP_MB
    # 6000 therefore sits inside the normal band — it blocked 283 restores on
    # 08-18 alone, including 10 straight minutes with 65 sessions queued after an
    # account switch. rc_load_guard.py dropped swap as a trigger on 08-06 for the
    # same reason; this copy was missed. Swap-used on macOS is a slowly-draining
    # high-water mark, so it can neither confirm an emergency nor confirm a fix.
    # It is logged in the cycle summary instead. Real gates: pressure level 4,
    # load, and MAX_LIVE.

    try:
        load1 = os.getloadavg()[0]
        if load1 > MAX_LOAD_PER_CPU * os.cpu_count():
            return f"load {load1:.1f} on {os.cpu_count()} cores"
    except OSError:
        pass
    return ""


def ps_lines():
    """(pid, argv) for every process of this user.

    NOT pgrep: `pgrep -f` intermittently fails to read the argv of the
    launchd-spawned RC daemon (verified 08-04 — ps sees pid 26354, pgrep does
    not). That reads as "daemon DOWN" here and as "server missing" in
    claude-rc-health.sh, whose bad-counter would eventually kickstart the daemon
    and wipe every app session. ps -A always sees it.
    """
    out = []
    for line in sh(f"{PS} pid=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def ps_lines_etime():
    """(pid, elapsed_seconds, argv) for every process of this user."""
    out = []
    for line in sh(f"{PS} pid=,etime=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+([\d:-]+)\s+(.*)", line)
        if m:
            try:
                out.append((m.group(1), _etime_seconds(m.group(2)), m.group(3)))
            except ValueError:
                continue
    return out


def daemon_pid():
    for pid, args in ps_lines():
        if "remote-control --remote-control-session-name-prefix" in args:
            return pid
    return ""


def connected_pids():
    """pids holding a non-loopback ESTABLISHED TCP socket.

    lsof prints the CLI's COMMAND as its version dir (`2.1.226`), not `claude`,
    so never filter this by command name — collect every pid and let the caller
    intersect. Empty result means lsof failed; callers must treat that as
    "unknown", never as "nothing is connected".
    """
    pids = set()
    if IS_LINUX:
        # lsof на свежем Debian может не стоять, ss из iproute2 есть всегда.
        # Строка без заголовка (-H): recv-q send-q local peer users:((…,pid=1,fd=5))
        # Без root ss показывает pid только своих процессов — нам того и надо.
        for line in sh("ss -Htnp state established").splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2].startswith(("127.", "[::1]")):
                continue
            pids.update(re.findall(r"pid=(\d+)", line))
        return pids
    for line in sh("lsof -nP -iTCP -sTCP:ESTABLISHED").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 8 and parts[1].isdigit() and "127.0.0.1" not in parts[8]:
            pids.add(parts[1])
    return pids


HUSK_GRACE_S = 300


def _etime_seconds(etime):
    """macOS ps etime: [[dd-]hh:]mm:ss — there is no etimes on this platform."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def husk_pids():
    """Restored sessions whose relay is gone but whose process is still up.

    After a long relay outage a restored session logs `Remote Control
    disconnected - Transport recovery exhausted (code 4093)` and does not come
    back. The process keeps running with zero sockets, so a process-only
    liveness test counts it live forever, MAX_LIVE stays saturated and the real
    queue never restores (the 08-06 blind spot).

    Two false positives this MUST NOT hit, both verified 08-10:
    - the `script -q LOG claude --resume …` wrapper carries the same argv but
      never holds a socket of its own, so matching on argv alone flags every
      healthy session and reaping it kills the lot;
    - a session restored seconds ago has not dialled the relay yet.
    """
    conn = connected_pids()
    if not conn:                     # lsof unavailable — assume nothing is a husk
        return []
    # a session on another model (rc_providers) has no relay at all and no
    # socket while idle: by this test it would always look like a husk
    other = provider_uuids()
    out = []
    for pid, etime, args in ps_lines_etime():
        if not is_rc_args(args):
            continue
        if not RESUME_RE.search(args):
            continue
        if not args.lstrip().startswith(CLAUDE):   # skip the `script` wrapper
            continue
        if pid in conn or etime < HUSK_GRACE_S:
            continue
        m = RESUME_RE.search(args)
        if m and m.group(1) in other:
            continue
        out.append(pid)
    return out


def live_uuids():
    """uuids of sessions this guardian restored that are still running AND connected.

    A session with no relay is not live no matter how healthy its process looks
    — see husk_pids().
    """
    husks = set(husk_pids())
    found = set()
    for pid, args in ps_lines():
        if not is_rc_args(args) or pid in husks or "--fork-session" in args:
            continue
        m = RESUME_RE.search(args)
        if m:
            found.add(m.group(1))
            continue
        born_here = new_session_uuid(args)
        if born_here:
            found.add(born_here)
    return found


NEWLOG_RE = re.compile(r"/tmp/rc-new-([\w.-]+)\.log")

# Codex sessions (rc_codex) carry no --remote-control and resume as
# `codex resume <id>`; these two flags are in every one rc_codex launches.
CODEX_MARK = ("--no-daemon", "--dangerously-bypass-approvals-and-sandbox")
RESUME_RE = re.compile(r"(?:^|\s)(?:--)?resume\s+([0-9a-f-]{36})")


def is_codex_args(args):
    return all(m in args for m in CODEX_MARK)


def is_vibe_args(args):
    """A Mistral Vibe session wrapper (rc_vibe): `vibe … --trust --auto-approve`."""
    return "--trust" in args and "--auto-approve" in args and "vibe" in args


def is_rc_args(args):
    """A session process (or its wrapper) this panel runs: Claude, Codex or Vibe."""
    return "--remote-control" in args or is_codex_args(args) or is_vibe_args(args)


def new_session_uuid(args):
    """uuid сессии, заведённой здесь через launch_new, либо None.

    Такая сессия стартует БЕЗ --resume: её uuid ещё не существует в момент
    запуска, поэтому argv его не содержит и обычный поиск по --resume её не
    находит. act_new кладёт uuid рядом с логом, как только Claude создаст
    транскрипт, — это единственная связь процесса с сессией. Без неё гардиан
    читает живой процесс как мёртвый и поднимает ВТОРОЙ claude на том же чате.
    """
    m = NEWLOG_RE.search(args)
    if not m:
        return None
    try:
        with open("/tmp/rc-new-%s.uuid" % m.group(1)) as fh:
            uuid = fh.read().strip()
    except OSError:
        # ⛔ 10-01: macOS tmp_cleaner had deleted the .uuid of 28 panel sessions
        # (8-29 days old); invisible to the panel, they held 2.3 GB. The copy in
        # ~/.claude survives it.
        uuid = load(NEW_UUIDS_PATH, {}).get(m.group(1), "")
    return uuid if re.match(r"^[0-9a-f-]{36}$", uuid) else None


def record_new_uuid(key, uuid):
    """Bind a panel-started session (launch key) to its uuid: beside the log in
    /tmp, and in ~/.claude, which the tmp cleaner never touches."""
    try:
        with open("/tmp/rc-new-%s.uuid" % key, "w") as fh:
            fh.write(uuid)
    except OSError:
        pass
    m = load(NEW_UUIDS_PATH, {})
    m[key] = uuid
    save(NEW_UUIDS_PATH, m)


RUNTIME_RE = re.compile(r"/tmp/rc-[\w.-]+\.(?:log|sh)")


def keep_runtime_files():
    """Refresh the atime of every running session's /tmp files.

    ⛔ 10-01: /usr/libexec/tmp_cleaner (launchd, daily 00:00) deletes /tmp files
    whose atime, mtime AND ctime are all 3+ days old, and reads here do not move
    atime. A session idle for 3 days lost its pty log, launch script and .uuid:
    the guardian read it as dead, the panel stopped showing it, and it ran on as
    an orphan for weeks. Only atime is set: mtime is the busy signal (rule 6).
    FIFOs are not regular files, the cleaner skips them.
    """
    now = time.time()
    paths = set()
    for _pid, args in ps_lines():
        if "/tmp/rc-" not in args:
            continue
        paths.update(RUNTIME_RE.findall(args))
        m = NEWLOG_RE.search(args)
        if m:
            paths.add("/tmp/rc-new-%s.uuid" % m.group(1))
    for p in paths:
        try:
            os.utime(p, (now, os.stat(p).st_mtime))
        except OSError:
            pass
    # drop keys whose wrapper is gone, so the map does not grow forever
    # and copy in bindings made before the map existed (or lost a race)
    keys = {k for k in (NEWLOG_RE.search(a) for _p, a in ps_lines()) if k}
    keys = {k.group(1) for k in keys}
    m = load(NEW_UUIDS_PATH, {})
    fresh = {k: v for k, v in m.items() if k in keys}
    for k in keys - set(fresh):
        try:
            with open("/tmp/rc-new-%s.uuid" % k) as fh:
                fresh[k] = fh.read().strip()
        except OSError:
            pass
    if fresh != m:
        save(NEW_UUIDS_PATH, fresh)


UNBOUND_GRACE_S = 30 * 60


def born_after_launch(key, window=600):
    """uuid of the one transcript created within `window` s of launch `key`
    (new-<epoch>) that no other process or binding owns, else None. act_new
    gives up after 30 s; a slow start creates the transcript later."""
    mk = re.match(r"new-(\d+)$", key)
    if not mk:
        return None
    t0 = int(mk.group(1))
    taken = set(load(NEW_UUIDS_PATH, {}).values())
    taken |= set(re.findall(r"--resume\s+([0-9a-f-]{36})",
                            "\n".join(a for _p, a in ps_lines())))
    hits = []
    try:
        names = os.listdir(PROJ)
    except OSError:
        return None
    for f in names:
        if not f.endswith(".jsonl") or f[:-6] in taken:
            continue
        try:
            st = os.stat(os.path.join(PROJ, f))
        except OSError:
            continue
        born = getattr(st, "st_birthtime", st.st_mtime)
        if t0 <= born <= t0 + window:
            hits.append(f[:-6])
    return hits[0] if len(hits) == 1 else None


def reap_unbound_new():
    """Kill panel-started sessions that no uuid can be found for.

    Such a process is invisible to everything here: not live, not in the panel,
    never retired, never killed, so it runs until reboot (10-01: 28 of them,
    up to 29 days, 2-13 h of CPU each). act_new binds the uuid within 30 s, so
    one still unbound after UNBOUND_GRACE_S never will be. Its transcript stays
    on disk and the guardian can restore it with --resume.
    """
    n = 0
    for pid, etime, args in ps_lines_etime():
        if not re.match(r"\s*(\S*/)?script\s", args) or not is_rc_args(args):
            continue
        m = NEWLOG_RE.search(args)
        if not m or new_session_uuid(args):
            continue
        if etime < UNBOUND_GRACE_S:
            continue
        found = born_after_launch(m.group(1))
        if found:
            record_new_uuid(m.group(1), found)
            log(f"bound panel session {m.group(1)} to {found[:8]} by transcript birth")
            continue
        kids = [p for p, pp, _st, a in ps_tree()
                if pp == pid and a.lstrip().startswith(CLAUDE)]
        for k in kids or [pid]:
            kill_session_tree(k)
        n += 1
    if n:
        log(f"killed {n} panel session(s) with no uuid binding: invisible orphans")
    return n


def live_daemon_children():
    """count of sessions the daemon itself spawned (app-created chats)."""
    return len([1 for _pid, args in ps_lines()
                if "--sdk-url" in args and "code/sessions/cse_" in args])


# --------------------------------------------------------------------------
# census
# --------------------------------------------------------------------------

HEAD_BYTES = 200_000


def is_rc_transcript(path):
    """RC/remote sessions write entrypoint=sdk-cli; desktop app writes claude-desktop.

    Третий случай — `cli`: сессия, запущенная на этой машине через launch_new,
    то есть кнопка «новая сессия» в панели. Это полноценный чат с каналом
    ввода, и его надо регистрировать. Пустышки от `claude -p "/usage"`, которые
    когда-то забили реестр, пишут sdk-cli (проверено 08-24 на 218 штуках), так
    что `cli` их не возвращает.
    """
    try:
        with open(path, errors="ignore") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return False
    if '"entrypoint":"sdk-cli"' in head:
        return True
    if '"entrypoint":"claude-desktop"' in head:
        return False
    if '"entrypoint":"cli"' in head:
        return True
    return False


def first_prompt(path):
    try:
        with open(path, errors="ignore") as fh:
            for _ in range(400):
                line = fh.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "user":
                    continue
                c = (o.get("message") or {}).get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                if not isinstance(c, str):
                    continue
                c = " ".join(c.split())
                if not c or c.startswith("Caveat") or c.startswith("<"):
                    continue
                return c[:400]
    except OSError:
        pass
    return ""


def make_name(prompt, uuid):
    if not prompt:
        return f"session {uuid[:8]}"
    name = re.sub(r"^(йоу|yo|слушай|привет|hi|hey)[ ,!-]*", "", prompt, flags=re.I)
    name = re.sub(r'["`\\\n\r]', " ", name)
    name = " ".join(name.split())[:NAME_MAX].strip()
    return name or f"session {uuid[:8]}"


def has_content(rec):
    """Did a human ever say anything in this session?

    ⛔ The restore queue is ranked by transcript mtime, and `claude -p "/usage"`
    writes an sdk-cli transcript into PROJ exactly like a real RC session does.
    The panel and the bot each poll it every 15 min, so on 08-23 those pings were
    66 of the 106 sessions in the window AND the freshest files on disk: they won
    every slot ahead of the chats the owner was actually working in, and a migrate
    re-materialised them as empty cards. Restoring a session with no user message
    resurrects nothing, so it never belongs in the queue.

    Source-side fix lives in rc_control.usage(), which now runs from its own cwd
    so those transcripts land outside PROJ. This is the backstop for every other
    `claude -p` that ever runs from the home directory.
    """
    return bool((rec.get("prompt") or "").strip())


def census(reg):
    """Register every RC transcript touched inside the working window."""
    cutoff = time.time() - WORKING_WINDOW_H * 3600
    sessions = reg.setdefault("sessions", {})
    seen = 0
    try:
        entries = os.scandir(PROJ)
    except OSError:
        return 0
    for e in entries:
        if not e.name.endswith(".jsonl"):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        uuid = e.name[:-6]
        rec = sessions.get(uuid)
        # Вердикт «other» раньше запоминался навсегда. Транскрипт, осмотренный
        # до того, как Claude написал entrypoint, оставался невидимым до конца
        # жизни — именно так пропадали сессии из панели. Пересматриваем, пока
        # файл не перерос окно, которое is_rc_transcript вообще читает.
        if (rec is not None and rec.get("kind") == "other"
                and rec.get("judged_at", 0) < HEAD_BYTES
                and st.st_size > rec.get("judged_at", 0)):
            rec = None
        if rec is None:
            if not is_rc_transcript(e.path):
                sessions[uuid] = {"kind": "other", "judged_at": st.st_size}
                continue
            prompt = first_prompt(e.path)
            rec = sessions[uuid] = {
                "kind": "rc",
                "name": make_name(prompt, uuid),
                "prompt": prompt[:200],
                "first_seen": now(),
                "restores": 0,
            }
        if rec.get("kind") != "rc":
            continue
        # name/prompt are derived once, at registration. A chat first seen
        # between its create and its first user message would keep the empty
        # name forever — and, with has_content() gating the queue, would never be
        # restored again. Re-derive while it is still empty: a real chat
        # graduates on the next 60 s pass, a /usage ping never does.
        if not rec.get("prompt"):
            p = first_prompt(e.path)
            if p:
                rec["prompt"] = p[:200]
                rec["name"] = make_name(p, uuid)
        rec["last_active"] = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        rec["last_active_ts"] = st.st_mtime
        rec["size"] = st.st_size
        seen += 1
    # Codex (rc_codex) and Mistral Vibe (rc_vibe) sessions keep their thread
    # outside PROJ: a rollout file, or a session folder (activity = newest file)
    for uuid, rec in sessions.items():
        if not isinstance(rec, dict) or rec.get("provider") not in ("codex", "vibe"):
            continue
        path = rec.get("rollout") or ""
        if rec["provider"] == "codex" and (not path or not os.path.exists(path)):
            try:
                import rc_codex
                path = rc_codex.path_for_id(uuid)
            except Exception:
                path = ""
            if path:
                rec["rollout"] = path
        if not path:
            continue
        if rec["provider"] == "vibe":
            mt, size = vibe_version(path)
        else:
            try:
                st = os.stat(path)
            except OSError:
                continue
            mt, size = st.st_mtime, st.st_size
        if not mt:
            continue
        rec["last_active"] = datetime.fromtimestamp(mt).isoformat(timespec="seconds")
        rec["last_active_ts"] = mt
        rec["size"] = size
        seen += 1
    return seen


def vibe_version(sdir):
    """(newest mtime, total size) of a Vibe session folder's journal and meta:
    the folder's own mtime does not move when a journal file grows."""
    import glob
    mt, size = 0.0, 0
    for f in glob.glob(os.path.join(sdir, "journal", "*.jsonl")) + [os.path.join(sdir, "meta.json"),
                                                                    os.path.join(sdir, "CURRENT")]:
        try:
            st = os.stat(f)
        except OSError:
            continue
        mt = max(mt, st.st_mtime)
        size += st.st_size
    return mt, size


def working_set(reg, live):
    """RC sessions inside the window, newest first, not currently running."""
    cutoff = time.time() - WORKING_WINDOW_H * 3600
    out = []
    for uuid, rec in reg.get("sessions", {}).items():
        if rec.get("kind") != "rc":
            continue
        # retired: by hand (--retire, panel Kill/Retire) or by archiving the
        # chat in the app (retired_by "app-archive", see sync_archive). This one
        # test keeps both out of the migrate queue, the wipe pool, the top-up
        # and the husk re-queue — all of them are built from this list.
        if rec.get("retired"):
            continue
        if not has_content(rec):
            continue
        if rec.get("last_active_ts", 0) < cutoff:
            continue
        out.append((uuid, rec))
    out.sort(key=lambda kv: kv[1].get("last_active_ts", 0), reverse=True)
    return out


# --------------------------------------------------------------------------
# archive state in the app
# --------------------------------------------------------------------------
#
# ⛔ 09-24: the 19:00 account switch carried 25 sessions, 8 of them archived in
# the app and idle since 09-06..09-20. Archiving leaves no local trace: the
# restored process keeps running, the transcript, pty log and claude-rc.log say
# nothing. Nor does time separate them: a kept chat (5ca96a49, last message
# 09-18) is older than an archived one (cbac47d5, 09-20), and a 48 h
# last-message rule would have parked 10 chats the owner kept. claude.ai is the only
# place that knows, and only while that account is signed in.

SESSIONS_API = "https://api.anthropic.com/v1/code/sessions"
ARCHIVE_POLL_S = 300      # every 5 min; forced before a migrate and during sign-in
ARCHIVE_BUDGET_S = 20     # the whole listing: past this keep the last known flags
ARCHIVE_PAGE = 100        # 300 cards = 3 pages (the default page is 20)
ARCHIVE_MAX_PAGES = 30
ARCHIVE_BY = "app-archive"
LOAD_GUARD_STATE = CFG.state("rc-load-guard-state.json")
SHED_ARCHIVE_S = 30 * 60


def oauth_token():
    """The CLI's claude.ai access token, '' when unreadable. Never log it.

    Keychain works from launchd, not over ssh (see the skill's rule 13): from
    ssh this is '' and the sync fails open.
    """
    if IS_LINUX:
        d = load(os.path.join(HOME, ".claude/.credentials.json"), {})
    else:
        try:
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10).stdout
            d = json.loads(raw)
        except (subprocess.SubprocessError, OSError, ValueError):
            d = {}
    oauth = d.get("claudeAiOauth") if isinstance(d, dict) else None
    if not isinstance(oauth, dict):
        return ""
    return oauth.get("accessToken") or ""


def app_statuses():
    """({"session_<X>": "active"|"archived"}, error) for every card of the
    signed-in account. Card session_<X> is item id cse_<X>.

    On an error the pages read so far still come back: each item is the truth
    about its own card, and a card that is missing only means "unknown".
    """
    token = oauth_token()
    if not token:
        return {}, "no token"
    out = {}
    cursor = ""
    deadline = time.time() + ARCHIVE_BUDGET_S
    for _ in range(ARCHIVE_MAX_PAGES):
        left = deadline - time.time()
        if left <= 0:
            return out, "timed out"
        url = "%s?limit=%d" % (SESSIONS_API, ARCHIVE_PAGE)
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor, safe="")
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=min(10, left)) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return out, "HTTP %d" % exc.code
        except Exception as exc:        # never let the listing take the pass down
            return out, type(exc).__name__
        for item in body.get("data") or []:
            sid, status = item.get("id") or "", item.get("status")
            if sid.startswith("cse_") and status in ("active", "archived"):
                out["session_" + sid[4:]] = status
        cursor = body.get("next_cursor") or ""
        if not cursor:
            return out, ""
    return out, "more than %d pages" % ARCHIVE_MAX_PAGES


def wrapper_logs():
    """{uuid: pty log} for every running session wrapper, restored or new."""
    out = {}
    for _pid, args in ps_lines():
        if not is_rc_args(args) or not re.match(r"\s*(\S*/)?script\s", args):
            continue
        m = re.search(r"(/tmp/rc-(?:restore|new)-[\w.-]+\.log)", args)
        if not m:
            continue
        r = RESUME_RE.search(args)
        uuid = r.group(1) if r else new_session_uuid(args)
        if uuid:
            out[uuid] = m.group(1)
    return out


def head_link(logf, limit=65536):
    """The card this process is on: the FIRST card token in its pty log.

    Not read_link(), which takes the last one: a session that has run for days
    has other sessions' ids on its screen (this one printed dozens while the
    archive sync was being built). The card is printed at start, 0.5-3.5 KB in
    (all 16 live logs on 09-24), before any conversation is drawn.
    """
    try:
        with open(logf, "rb") as fh:
            txt = ANSI.sub("", fh.read(limit).decode("utf-8", "ignore"))
    except OSError:
        return ""
    m = re.search(r"(?:^|[^A-Za-z0-9])(01[A-Za-z0-9]{22})(?![A-Za-z0-9])", txt)
    return "session_" + m.group(1) if m else ""


def refresh_links(reg, statuses):
    """Keep app_session pointing at the card the session is on NOW.

    The archive flag is read by card, so a missing or stale link is a session
    the sync cannot see. Two holes: backfill_links() fills a link only once and
    only from restore logs, so sessions started from the panel's «new session»
    never got one (19 of 44 in the working set on 09-24); and a restore whose
    link came after the 45 s window kept the card of the previous account.

    An existing link is replaced only by a card the listing confirms exists:
    a mangled pty token must never re-point a session at somebody else's card.
    """
    sessions = reg.get("sessions", {})
    for uuid, logf in wrapper_logs().items():
        rec = sessions.get(uuid)
        if rec is None or rec.get("kind") != "rc":
            continue
        link = head_link(logf)
        if not link or link == rec.get("app_session"):
            continue
        if not rec.get("app_session") or link in statuses:
            rec["app_session"] = link
    # not running, never linked, started from the panel: its card is in the
    # launch log, unless a restore has since put it on another card
    import glob
    for f in glob.glob("/tmp/rc-new-*.uuid"):
        try:
            if os.stat(f).st_uid != os.getuid():   # /tmp is shared
                continue
            with open(f) as fh:
                uuid = fh.read().strip()
        except OSError:
            continue
        rec = sessions.get(uuid)
        if (rec is None or rec.get("kind") != "rc" or rec.get("app_session")
                or glob.glob("/tmp/rc-restore-%s-*.log" % uuid[:8])):
            continue
        link = head_link(f[:-len(".uuid")] + ".log")
        if link:
            rec["app_session"] = link


def apply_archive(reg, statuses):
    """Write the app's archive state into the registry. Returns (retired,
    un-retired) as lists of (uuid, rec).

    Edge-triggered, per card (`app_archived_card`): only a CHANGE seen in the
    app moves `retired`. So a hand --unretire of an archived chat sticks, and
    unarchiving brings back only what archiving took away — a hand retire
    (no `retired_by`) stays retired. A running archived session is left
    running; being retired it is never carried, re-queued or restored.
    """
    gone, back = [], []
    # ⛔ 10-14: the load guard TERMs a session, the session closes its card, the
    # app reports it archived 20 s to 4 min later, and this edge used to retire
    # it, so "come back when the box is calm" never happened (dda265a9,
    # 29828123). Archives within SHED_ARCHIVE_S of a shed are not the user's.
    shed = {}
    try:
        with open(LOAD_GUARD_STATE) as fh:
            shed = json.load(fh).get("shed_uuids") or {}
    except (OSError, ValueError):
        pass
    for uuid, rec in reg.get("sessions", {}).items():
        if rec.get("kind") != "rc":
            continue
        link = rec.get("app_session") or ""
        status = statuses.get(link) if link else None
        if status is not None:
            archived = status == "archived"
            was = rec.get("app_archived") if rec.get("app_archived_card") == link else None
            rec["app_archived"] = archived
            rec["app_archived_card"] = link
            if (archived and was is not True and not rec.get("retired")
                    and time.time() - float(shed.get(uuid) or 0) < SHED_ARCHIVE_S):
                log(f"archived in the app right after a load-guard shed -> "
                    f"kept {uuid[:8]}, not retired")
            elif (archived and was is not True and not rec.get("retired")
                    and time.time() - float(rec.get("reboot_keep") or 0) < REBOOT_KEEP_S):
                log(f"card closed by the reboot -> kept {uuid[:8]}, not retired")
            elif archived and was is not True and not rec.get("retired"):
                rec["retired"] = True
                rec["retired_by"] = ARCHIVE_BY
                gone.append((uuid, rec))
            elif (not archived and was is True and rec.get("retired")
                    and rec.get("retired_by") == ARCHIVE_BY):
                rec["retired"] = False
                back.append((uuid, rec))
        # the tag means "retired because archived" and nothing else: a panel
        # un-retire (rc_control does not know the tag) must not leave it behind
        if not rec.get("retired"):
            rec.pop("retired_by", None)
    return gone, back


def signin_running():
    """A `claude auth login` is under way (panel sign-in or a terminal): the
    old account is still the one in the Keychain, for the last time."""
    # anchored on the binary: a session NAMED "... auth login ..." is in argv too
    return any(re.match(r"\s*(?:\S*/)?(?:claude|[\d.]+)\s+auth\s+login\b", args)
               for _pid, args in ps_lines())


def sync_archive(reg, state, force=False):
    """Poll archive state and write it into reg (the caller holds the lock and
    saves). Every ARCHIVE_POLL_S, or now when forced.

    Fails open: no token, 401, a network error = the last known flags stay,
    the pass goes on, and the error is logged once, not every 5 min.
    """
    if CFG.DEFAULT_PROVIDER:
        return      # no Claude subscription here: no app cards to sync
    if not force and time.time() - state.get("archive_try", 0) < ARCHIVE_POLL_S:
        return
    # a failed attempt waits the full interval too: a dead endpoint must not
    # cost every 60 s pass its timeout
    state["archive_try"] = time.time()
    statuses, err = app_statuses()
    refresh_links(reg, statuses)
    gone, back = apply_archive(reg, statuses)
    for uuid, rec in gone:
        log(f"archived in the app -> retired {uuid[:8]}  {rec.get('name', '')}")
    for uuid, rec in back:
        log(f"unarchived in the app -> un-retired {uuid[:8]}  {rec.get('name', '')}")
    if statuses:
        state["archive_ok"] = time.time()
        state["archive_cards"] = len(statuses)
    prev = state.get("archive_err", "")
    if err and err != prev:
        log(f"archive sync failed ({err}) — keeping the last known flags"
            + (f", {len(statuses)} cards read" if statuses else ""))
    elif not err and prev:
        log(f"archive sync back ({len(statuses)} cards)")
    state["archive_err"] = err


def archive_line(reg, state):
    """One line for --status / --preflight."""
    n = sum(1 for r in reg.get("sessions", {}).values()
            if r.get("retired") and r.get("retired_by") == ARCHIVE_BY)
    ok = state.get("archive_ok")
    when = datetime.fromtimestamp(ok).strftime("%m-%d %H:%M") if ok else "never"
    err = state.get("archive_err")
    return (f"{n} retired by archiving in the app, last read {when}"
            + (f" (now failing: {err})" if err else ""))


# --------------------------------------------------------------------------
# wipe detection
# --------------------------------------------------------------------------

def read_new_rclog(state):
    try:
        size = os.path.getsize(RCLOG)
    except OSError:
        return ""
    if "rclog_offset" not in state:
        # first run: start at EOF, never replay a historic wipe
        state["rclog_offset"] = size
        state["rclog_size"] = size
        return ""
    off = state["rclog_offset"]
    prev_size = state.get("rclog_size")
    # claude-rc-health.sh caps this log in place: tail -c 8MB once it passes 12MB.
    # After that the byte offsets mean nothing, and reading from 0 replays every
    # old wipe in the retained tail as if it just happened (false alarm 08-04
    # 01:35: the 08-03 00:29 wipe came back as "21 sessions failed at once").
    # Rotation loses at most one cycle of log — resync to EOF instead.
    if off > size or (prev_size is not None and size < prev_size):
        log(f"rc log rotated ({prev_size} -> {size} bytes) — resyncing to EOF")
        state["rclog_offset"] = size
        state["rclog_size"] = size
        return ""
    if off == size:
        state["rclog_size"] = size
        return ""
    try:
        with open(RCLOG, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(4_000_000)
    except OSError:
        return ""
    state["rclog_offset"] = off + len(chunk)
    state["rclog_size"] = size
    return ANSI.sub("", chunk.decode("utf-8", "ignore"))


FRESH_WINDOW_S = 30 * 60


def fresh_lines(text):
    """Keep only log lines stamped within the last 30 min.

    Second belt against replaying history: whatever rewinds the offset (rotation,
    a restored state file, a manual edit), a stale tail can never look like a
    wipe that is happening right now. The daemon stamps [HH:MM:SS] with no date,
    so a line that reads as "in the future" is yesterday's.
    """
    ref = datetime.now()
    out = []
    for line in text.splitlines():
        m = re.search(r"\[(\d\d):(\d\d):(\d\d)\]", line)
        if not m:
            out.append(line)
            continue
        h, mi, s = (int(x) for x in m.groups())
        age = (ref - ref.replace(hour=h, minute=mi, second=s, microsecond=0)).total_seconds()
        if age < -300:      # clock skew aside, a "future" stamp is from yesterday
            age += 86400
        if age <= FRESH_WINDOW_S:
            out.append(line)
    return "\n".join(out)


def detect_wipe(state, chunk, dpid, prev_app):
    """Return (victims_count, reason) or (0, '')."""
    reasons = []
    victims = 0
    chunk = fresh_lines(chunk)

    m = re.findall(r"Shutting down (\d+) active session\(s\)", chunk)
    if m:
        victims = max(victims, max(int(x) for x in m))
        reasons.append("daemon gave up on the relay and shut sessions down")

    failed = re.findall(r"Session failed: Process exited with error (cse_\w+)", chunk)
    if len(failed) >= 2:
        victims = max(victims, len(set(failed)))
        reasons.append(f"{len(set(failed))} sessions failed at once")

    if re.search(r"Server unreachable for \d+ minutes, giving up", chunk):
        if "relay" not in " ".join(reasons):
            reasons.append("relay unreachable, daemon gave up")

    prev_pid = state.get("daemon_pid")
    if prev_pid and dpid and prev_pid != dpid:
        reasons.append(f"daemon restarted ({prev_pid} -> {dpid})")
        # Only the daemon's own children die with it; guardian-restored sessions
        # are separate processes. Counting them here would resurrect sessions the
        # user closed days ago.
        victims = max(victims, prev_app)

    return victims, "; ".join(reasons)


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def fifo_path(uuid):
    """Канал ввода живой сессии. Держится открытым самим её скриптом — см. launch()."""
    return f"/tmp/rc-in-{uuid[:8]}.fifo"


def script_cmd(logf, argv):
    """Обёртка `script`, пишущая pty сессии в logf.

    BSD script (macOS): `script -q LOG cmd args…`; util-linux (Debian):
    `script -q -f -c "cmd args…" LOG`. ⛔ Обе формы обязаны оставлять в argv
    обёртки и `script -q `, и путь лога: по ним rc_control.log_for находит
    pty-лог живой сессии, а pid_for и сторож нагрузки отличают обёртку от
    самого claude. На Linux команда идёт через `exec`, иначе между script и
    claude остался бы `sh -c`, и процесс с argv от пути claude не нашёлся бы.
    `-f` — сбрасывать лог на каждую запись: статус «работает» в панели
    считается по свежести этого файла.
    """
    if IS_LINUX:
        return (f"script -q -f -c {shlex.quote('exec ' + argv)} "
                f"{shlex.quote(logf)}")
    # -F: flush every write. Without it BSD script buffers the screen, the log
    # lags behind the TUI, and a finished turn kept reading as "working" from
    # its last flushed spinner frame (09-25: Stop shown on an idle session).
    return f"script -q -F {shlex.quote(logf)} {argv}"


def spawn_detached(sh_path):
    """Запустить обёртку сессии так, чтобы она пережила того, кто её запустил.

    start_new_session — setsid(): без него сессия умирает вместе с проходом
    гардиана (SIGHUP / EOF на stdin). На macOS этого достаточно. ⛔ На Linux —
    нет: systemd держит процессы в cgroup службы, и когда oneshot-юнит
    гардиана завершается (или панель перезапускается), всё, что они породили,
    получает SIGTERM, setsid тут не спасает. Поэтому сессия уезжает в
    собственный scope через systemd-run: свой юнит, своя cgroup, живёт под
    менеджером пользователя, а не под тем, кто её поднял.
    """
    argv = ["/bin/bash", sh_path]
    if IS_LINUX:
        unit = "rc-sess-%s-%d" % (
            re.sub(r"[^A-Za-z0-9]+", "-", os.path.basename(sh_path))[:40],
            int(time.time()))
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                "--unit", unit] + argv
    return subprocess.Popen(argv,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)


def claude_exec():
    """Path sessions are launched with: CLAUDE_FIXED, re-cloned from the newest
    installed version when that changed; CLAUDE if anything goes wrong."""
    if IS_LINUX:
        return CLAUDE
    try:
        src = os.path.realpath(CLAUDE)
    except OSError:
        return CLAUDE
    stamp = CLAUDE_FIXED + ".from"
    try:
        with open(stamp) as fh:
            same = fh.read().strip() == src
    except OSError:
        same = False
    if same and os.access(CLAUDE_FIXED, os.X_OK):
        return CLAUDE_FIXED
    tmp = "%s.tmp%d" % (CLAUDE_FIXED, os.getpid())
    try:
        if subprocess.run(["/bin/cp", "-c", src, tmp], capture_output=True).returncode:
            subprocess.run(["/bin/cp", src, tmp], capture_output=True, check=True)
        if subprocess.run(["/usr/bin/codesign", "--verify", tmp],
                          capture_output=True, timeout=60).returncode:
            raise OSError("signature does not verify")
        os.chmod(tmp, 0o755)
        os.replace(tmp, CLAUDE_FIXED)
        with open(stamp, "w") as fh:
            fh.write(src + "\n")
        log(f"claude-rc now runs {os.path.basename(src)}")
        return CLAUDE_FIXED
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log(f"could not refresh claude-rc ({exc}); launching {CLAUDE}")
        return CLAUDE


def wrapper_text(fifo, logf, argv, answer, env_file=None):
    """Текст обёртки одной сессии: держит канал ввода, пишет pty в лог,
    умирает вместе с claude.

    macOS (BSD script):
      exec 3<>FIFO         stdin — FIFO, который скрипт сам держит открытым
                           на чтение-запись: сессия умирает в тот же миг, когда
                           stdin ловит EOF, а `exec 3<>` гарантирует писателя на
                           весь её срок. Всё написанное в FIFO снаружи уезжает
                           в stdin сессии — так панель набирает в живую сессию.
      printf '1\n' >&3     ответ на диалог возобновления: "Resume from summary
                           (recommended)" — только при --resume.
      cat <&3 | script … & ⛔ FIFO нельзя отдать script напрямую: на macOS он
                           делает tcgetattr на своём stdin и падает с
                           "Operation not supported on socket" (08-23). Обычный
                           пайп принимает — поэтому между ними cat.
      wait $!; kill 0      ⛔ конвейер в фон, ждём именно script. Иначе cat
                           висит на FIFO вечно, обёртка переживает сессию и
                           держит канал открытым — проверка живости врёт
                           (08-24, Broken pipe).

    Linux (util-linux script):
      script … <&3         FIFO в stdin он принимает, cat не нужен; выходит,
                           как только умер ребёнок (проверено 04-09).
      kill 0               ⛔ Форма с `cat | … &; wait $!` здесь НЕ работает:
                           bash 5 в `wait $!` ждёт весь конвейер, а cat на FIFO
                           без EOF живёт вечно — обёртка не умирала (04-09).
    """
    q = shlex.quote(fifo)
    ino = "stat -c %i" if IS_LINUX else "stat -f %i"
    lines = ["#!/bin/bash",
             f"cd {shlex.quote(WORKDIR)}"]
    if env_file:
        # another model behind the same claude (rc_providers): its env, never argv
        lines.append(f"set -a; . {shlex.quote(env_file)}; set +a")
    lines += [f"exec 3<>{q}",
             f"ino=$({ino} {q} 2>/dev/null)"]
    if answer:
        lines.append("printf '1\\n' >&3")
    if IS_LINUX:
        lines.append(f"{script_cmd(logf, argv)} <&3")
    else:
        # ⛔ Was `cat <&3 | script … &` + `wait $!`. /bin/bash 3.2 on macOS
        # waits there for the WHOLE pipeline, and cat, reading a FIFO the
        # wrapper itself holds open, never ends: `kill 0` never ran. 09-25: 101
        # dead wrappers (bash+cat) up to 21 days old, each still holding its
        # session's channel — so a message to a stopped session found a
        # "live" channel, cat died of SIGPIPE mid-write, and the send failed
        # with Broken pipe. script now runs in the foreground (bash waits for
        # it alone), fed through a process substitution — still a plain pipe,
        # which script's tcgetattr accepts.
        lines.append(f"{script_cmd(logf, argv)} < <(cat <&3)")
    # ⛔ 09-28: a FIFO nobody holds hangs whoever opens it, and Claude Code
    # 2.1.28x opens stray pipes in the temp dirs from its main thread: b28ee05e
    # froze three times on /tmp/ghauth.fifo (left by a gh sign-in on 09-03),
    # d3e26fda four hours on 09-24. So the wrapper takes its channel's name
    # with it — only if the name is still the pipe it opened (a relaunch may
    # have replaced it; `-ef /dev/fd/3` does not work on macOS, the inode does).
    lines.append(f'[ -n "$ino" ] && [ "$({ino} {q} 2>/dev/null)" = "$ino" ] && rm -f {q}')
    lines.append("kill 0 2>/dev/null")
    return "\n".join(lines) + "\n"


DIGEST_PATH = CFG.state("rc-digest.json")
EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")


def launch_opts(uuid, rec, digests=None):
    """(model, effort) to relaunch a session with: the registry's choice, else
    what its transcript says it was running (the panel's digest cache).

    ⛔ 09-24: the 19:00 migrate brought 8b19567c back as `claude --resume …`
    with no --model, although it had been running claude-fable-5-1: the model
    was only ever a launch flag of the panel's «new session», never written to
    the registry, so every restore quietly switched it to the CLI default.
    Every assistant record carries message.model and effort; the digest keeps
    the latest of both per session.
    """
    if rec.get("provider") in ("codex", "vibe"):
        # their own CLI: the model and effort chosen in the panel, else its default
        return rec.get("model"), rec.get("effort"), rec["provider"]
    if rec.get("provider"):
        # the provider's env names the model; Claude ids and efforts do not apply
        return None, None, rec["provider"]
    model, effort = rec.get("model"), rec.get("effort")
    if model and effort:
        return model, effort, None
    if digests is None:
        digests = load(DIGEST_PATH, {})
    d = digests.get(uuid) if isinstance(digests, dict) else None
    d = d if isinstance(d, dict) else {}
    seen_model = d.get("model_id") or ""
    seen_effort = d.get("effort_now") or ""
    # "<synthetic>" and the like are not something `claude --model` accepts
    if not model and re.match(r"^claude-[a-z0-9.-]+$", seen_model):
        model = seen_model
    if not effort and seen_effort in EFFORT_VALUES:
        effort = seen_effort
    return model or None, effort or None, None


def provider_env(provider):
    """Env file for a session on another model (rc_providers), or None for Claude."""
    if not provider or provider in ("codex", "vibe"):
        return None
    import rc_providers
    return rc_providers.env_file(provider)


def codex_cmd(resume_id=None, model=None, effort=None):
    import rc_codex
    return rc_codex.launch_cmd(model=model, effort=effort, resume_id=resume_id)


def vibe_cmd(resume_id=None, model=None):
    import rc_vibe
    import rc_providers
    return rc_vibe.launch_cmd(resume_id=resume_id, home=rc_providers.vibe_home(model))


def other_cmd(provider, resume_id=None, model=None, effort=None):
    """The command of a session that runs its own CLI (Codex, Mistral Vibe)."""
    if provider == "vibe":
        return vibe_cmd(resume_id, model)
    return codex_cmd(resume_id, model, effort)


def provider_uuids():
    """uuids of registry sessions that run on another model (rc_providers)."""
    try:
        reg = load(REG_PATH, {})
    except Exception:
        return set()
    return {u for u, r in (reg.get("sessions") or {}).items()
            if isinstance(r, dict) and r.get("provider")}


def launch(uuid, name, model=None, effort=None, provider=None):
    """Start one restored session; returns the pty log path to read the link from.

    model/effort приходят из записи в реестре, а не из вызова, поэтому выбор
    переживает и автоматический подъём гардианом — иначе он действовал бы ровно
    один раз, на ручном подъёме из панели, и молча терялся при следующем реапе.

    Флаги встают ДО --remote-control. Чем именно тот считает следующий за ним
    токен — своим значением или позиционным аргументом — отсюда не проверить,
    и лезть между ним и его аргументом незачем.
    """
    opts = ""
    if model:
        opts += " --model " + shlex.quote(str(model))
    if effort:
        opts += " --effort " + shlex.quote(str(effort))
    env_file = provider_env(provider)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name)[:24].strip("-") or uuid[:8]
    logf = f"/tmp/rc-restore-{uuid[:8]}-{slug}.log"
    # Written to a file rather than inlined: session names are raw user prompts
    # (apostrophes, quotes, Cyrillic) and nested shell quoting is where this breaks.
    sh_path = f"/tmp/rc-launch-{uuid[:8]}.sh"
    fifo = fifo_path(uuid)
    # старый канал мог остаться от прошлого подъёма: у пережившего его процесса
    # свой открытый fd, так что снять имя безопасно
    try:
        os.unlink(fifo)
    except OSError:
        pass
    os.mkfifo(fifo, 0o600)
    with open(sh_path, "w") as fh:
        fh.write(wrapper_text(
            fifo, logf,
            other_cmd(provider, uuid, model, effort) if provider in ("codex", "vibe") else
            f"{shlex.quote(claude_exec())} --resume {shlex.quote(uuid)}{opts} "
            f"--remote-control {shlex.quote(name)}",
            # codex and vibe resume open the thread with no dialog to answer
            answer=provider not in ("codex", "vibe"), env_file=env_file))
    os.chmod(sh_path, 0o755)
    # setsid + (на Linux) свой scope — см. spawn_detached.
    try:
        spawn_detached(sh_path)
    except OSError as exc:
        log(f"restore launch failed for {uuid[:8]}: {exc}")
    return logf


def launch_new(name, key, model=None, effort=None, provider=None):
    """Поднять НОВУЮ сессию — без --resume, с нуля.

    uuid появится только когда Claude создаст транскрипт, поэтому канал ввода
    заводится под временным именем `key`, а вызывающий переименует его в
    fifo_path(uuid), как только узнает uuid. Открытый fd переезд имени
    переживает — процесс продолжает читать тот же канал.
    """
    opts = ""
    if model:
        opts += " --model " + shlex.quote(str(model))
    if effort:
        opts += " --effort " + shlex.quote(str(effort))
    env_file = provider_env(provider)
    logf = f"/tmp/rc-new-{key}.log"
    sh_path = f"/tmp/rc-launch-{key}.sh"
    fifo = f"/tmp/rc-in-{key}.fifo"
    try:
        os.unlink(fifo)
    except OSError:
        pass
    os.mkfifo(fifo, 0o600)
    with open(sh_path, "w") as fh:
        # без --resume диалога возобновления нет — и ответа «1» тоже
        fh.write(wrapper_text(
            fifo, logf,
            other_cmd(provider, None, model, effort) if provider in ("codex", "vibe") else
            f"{shlex.quote(claude_exec())}{opts} --remote-control {shlex.quote(name)}",
            answer=False, env_file=env_file))
    os.chmod(sh_path, 0o755)
    try:
        spawn_detached(sh_path)
    except OSError as exc:
        log(f"launch_new failed: {exc}")
        raise
    return logf, fifo


def read_link(logf):
    """The pty capture drops characters as the TUI redraws ('sssion_', 'esson_'),
    so anchor on code/ and on the fixed 24-char id shape, not on 'session_'."""
    try:
        with open(logf, "rb") as fh:
            txt = ANSI.sub("", fh.read().decode("utf-8", "ignore"))
    except OSError:
        return ""
    # Anchoring on "code/" fails too: the redraw mangles the URL itself
    # ("https://caude.ai/cod/sessio_01Be6…"). The 24-char 01+22 token is the only
    # thing that survives intact, and nothing else in a pty log looks like it.
    m = re.findall(r"(?:^|[^A-Za-z0-9])(01[A-Za-z0-9]{22})(?![A-Za-z0-9])", txt)
    return "session_" + m[-1] if m else ""


def reap_husks():
    """Kill restored sessions that lost their relay so they can be restored again.

    Each restore runs under its own setsid, so killing the process group takes
    the bash wrapper, the stdin sleep-loop and `script` with it — killing the
    claude pid alone leaves the wrapper behind and it never exits.
    """
    pids = husk_pids()
    if not pids:
        return []
    # which sessions these were, captured before the kill takes the argv away
    want = set(pids)
    uuids = []
    for pid, args in ps_lines():
        if pid not in want:
            continue
        m = RESUME_RE.search(args)
        if m:
            uuids.append(m.group(1))
    groups = set()
    for pid in pids:
        pgid = sh(f"ps -o pgid= -p {pid}").strip()
        if pgid.isdigit():
            groups.add(pgid)
    for pgid in groups:
        subprocess.run(["/bin/bash", "-lc", f"kill -TERM -{pgid} || true"],
                       capture_output=True)
    time.sleep(2)
    for pgid in groups:
        subprocess.run(["/bin/bash", "-lc", f"kill -KILL -{pgid} 2>/dev/null || true"],
                       capture_output=True)
    log(f"reaped {len(pids)} husk session(s) — relay gone, process still up")
    return uuids


# --------------------------------------------------------------------------
# wedged sessions
# --------------------------------------------------------------------------
#
# ⛔ 09-13 (546d9eb4) and 09-25 (d3e26fda, four hours): the claude process stays
# up, holds its FIFO and its relay, and does nothing. Its main thread sat in a
# blocking open(); nothing reaped its finished children, its screen (pty log)
# stopped mid-spinner, and every message typed into it went into the FIFO and
# nowhere else. The panel only said "restart it" after a 25 s send attempt.
#
# The signature, both times: a <defunct> child of claude that outlives several
# passes. A running event loop reaps a child within milliseconds (38 healthy
# sessions on 09-25: zero zombies between them). Paired with a screen that has
# not changed for as long, it cannot be a busy session: a busy TUI redraws its
# spinner and timer every second, and an idle one has no zombies.

WEDGE_S = 300


def ps_tree():
    """[(pid, ppid, stat, args)] for this user."""
    out = []
    for line in sh(f"{PS} pid=,ppid=,stat=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)", line)
        if m:
            out.append(m.groups())
    return out


def descendants(pid, rows=None):
    """Every process below pid, in any process group or session.

    ⛔ Killing the session's process group is not enough: a background shell the
    session started (Bash run_in_background) runs in its own session and
    survived the kill on 09-25 as an orphan `until grep …; do sleep 5; done`,
    looping forever. Collect the tree BEFORE the kill: after it they belong to
    launchd and can no longer be told apart from anything else.
    """
    rows = rows if rows is not None else ps_tree()
    kids = {}
    for p, pp, _st, _a in rows:
        kids.setdefault(pp, []).append(p)
    out, todo = [], [str(pid)]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in out:
                out.append(c)
                todo.append(c)
    return out


def wrapper_pgid(pid, rows=None):
    """Process group of the launch wrapper above a claude pid (bash → cat,
    script), or None. claude itself sits in script's own session, so killing
    its group leaves the wrapper — and the session's input channel — behind."""
    rows = rows if rows is not None else ps_tree()
    parent = next((pp for p, pp, _st, _a in rows if p == str(pid)), None)
    if not parent:
        return None
    pargs = next((a for p, _pp, _st, a in rows if p == parent), "")
    if not re.match(r"\s*(\S*/)?script\s", pargs):
        return None
    try:
        return os.getpgid(int(parent))
    except OSError:
        return None


def reap_orphan_fifos(live, state):
    """Remove named pipes nobody holds, in /tmp, the user's temp dir and
    ~/.claude/rc-auth: Claude Code opens stray pipes there and freezes on them
    (see wrapper_text). Session channels of running sessions are never touched;
    an rc-in pipe must be a minute old, anyone else's half an hour.

    lsof decides who holds what, and it is checked against the live sessions'
    own channels first: if it cannot see those as held, nothing is removed.
    ⛔ `lsof -- <fifo path>` finds nothing on macOS even for a held pipe; only
    the full listing names them. /tmp is walked every pass (instant); the
    user's temp dir, ~1000 subdirectories and 6 s, every 10 minutes.
    """
    import tempfile
    now_ts = time.time()
    roots = [(os.path.realpath("/tmp"), 2), (CFG.state("rc-auth"), 1)]
    if now_ts - state.get("fifo_deep", 0) > 600:
        roots.append((os.path.realpath(tempfile.gettempdir()), 2))
        state["fifo_deep"] = now_ts
    cands = []

    def walk(d, depth):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if (st.st_mode & 0o170000) == 0o010000:            # S_IFIFO
                cands.append((e.path, st.st_mtime))
            elif depth > 1 and e.is_dir(follow_symlinks=False):
                walk(e.path, depth - 1)
    for root, depth in roots:
        walk(root, depth)
    tmp = os.path.realpath("/tmp")
    mine = {os.path.join(tmp, "rc-in-%s.fifo" % u[:8]) for u in live}
    controls = [p for p, _m in cands if p in mine]
    doomed = [p for p, m in cands if p not in mine
              and now_ts - m > (60 if os.path.basename(p).startswith("rc-in-") else 1800)]
    if not doomed:
        return 0
    try:
        out = subprocess.run(["lsof", "-nP", "-Fn", "-u", str(os.getuid())],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    held = {line[1:] for line in out.splitlines() if line.startswith("n")}
    if controls and not any(c in held for c in controls):
        log("orphan pipe sweep skipped: lsof does not see the live sessions' channels")
        return 0
    gone, woke = [], []
    for p in doomed:
        if p in held:
            continue
        # ⛔ A process blocked in open() on a pipe is not a holder in lsof, so
        # the pipe it waits on looks orphaned. Unlinking it makes the wait
        # permanent: nothing can open the other side by name any more (09-28,
        # the ghauth.fifo lesson). Wake whoever waits first, then unlink.
        if _release_one(p):
            woke.append(p)
        try:
            os.unlink(p)
            gone.append(p)
        except OSError:
            pass
    if woke:
        log(f"released a process blocked on orphan pipe(s): "
            + ", ".join(os.path.basename(w) for w in woke))
    if gone:
        log(f"removed {len(gone)} orphan pipe(s) nobody held: "
            + ", ".join(os.path.basename(g) for g in gone[:6]))
    return len(gone)


def _release_one(p):
    """Open a pipe once from each side and close it: a reader blocked in
    open() gets EOF, a writer blocked in open() is let through. True if
    someone was waiting."""
    waited = False
    try:
        fd = os.open(p, os.O_WRONLY | os.O_NONBLOCK)      # succeeds only if a reader waits
        os.close(fd)
        waited = True
    except OSError:
        pass
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
        time.sleep(0.1)
        try:
            if os.read(fd, 65536):
                waited = True
        except OSError:
            pass
        os.close(fd)
    except OSError:
        pass
    return waited


def _unheld_fifos(deep=False):
    """Named pipes in /tmp (and, deep, the user's temp dir) that no process of
    this user holds, per the full lsof listing."""
    import tempfile
    roots = [(os.path.realpath("/tmp"), 2)]
    if deep:
        roots.append((os.path.realpath(tempfile.gettempdir()), 2))
    found = []

    def walk(d, depth):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if (st.st_mode & 0o170000) == 0o010000:
                found.append(e.path)
            elif depth > 1 and e.is_dir(follow_symlinks=False):
                walk(e.path, depth - 1)
    for root, depth in roots:
        walk(root, depth)
    try:
        out = subprocess.run(["lsof", "-nP", "-Fn", "-u", str(os.getuid())],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    held = {line[1:] for line in out.splitlines() if line.startswith("n")}
    return [p for p in found if p not in held]


def release_fifo_waiters(deep=True):
    """Wake anything blocked opening a named pipe nobody holds: open it once
    from the other side and close it. Returns the pipes that had a waiter.

    ⛔ 09-28: this is what unfroze b28ee05e on the spot (/tmp/ghauth.fifo)
    where a restart only froze it again. Only unheld pipes are touched: a live
    session's channel is never opened for reading, so no message can be stolen.
    """
    return [p for p in _unheld_fifos(deep) if _release_one(p)]


def cpu_seconds(pid):
    """CPU time the process has used, in seconds; None if it is gone."""
    out = sh(f"ps -o time= -p {int(pid)}").strip()
    if not out:
        return None
    days = 0
    if "-" in out:
        d, out = out.split("-", 1)
        days = int(d) if d.isdigit() else 0
    parts = out.split(":")
    try:
        return (days * 86400 + (int(parts[-3]) * 3600 if len(parts) > 2 else 0)
                + (int(parts[-2]) * 60 if len(parts) > 1 else 0) + float(parts[-1]))
    except ValueError:
        return None


def try_unfreeze(pid):
    """Release pipe waiters, then see whether the frozen claude runs again.
    Returns (unfrozen, pipes_released)."""
    before = cpu_seconds(pid)
    woke = release_fifo_waiters()
    time.sleep(3)
    after = cpu_seconds(pid)
    moved = before is not None and after is not None and after - before > 0.05
    return moved, woke


def reap_stale_wrappers():
    """Clear launch wrappers whose session is gone: bash (+cat) with no
    `script` under it for over a minute. They hold the session's FIFO, so its
    channel looks alive and the next message breaks on it (see wrapper_text)."""
    rows = []
    for line in sh(f"{PS} pid=,ppid=,pgid=,etime=,args=").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d:-]+)\s+(.*)", line)
        if m:
            rows.append(m.groups())
    kids = {}
    for p, pp, _g, _e, a in rows:
        kids.setdefault(pp, []).append(a)
    n = 0
    for p, _pp, g, et, a in rows:
        if not re.search(r"/bin/bash /tmp/rc-launch-[\w.-]+\.sh$", a) or g != p:
            continue
        try:
            if _etime_seconds(et) < 60:
                continue
        except ValueError:
            continue
        if any(re.match(r"\s*(\S*/)?script\s", c) for c in kids.get(p, [])):
            continue
        try:
            os.killpg(int(g), signal.SIGTERM)
            n += 1
        except OSError:
            pass
    if n:
        log(f"cleared {n} dead session wrapper(s): no session under them, "
            f"still holding its input channel")
    return n


def kill_session_tree(pid):
    """TERM, then KILL: the session's process group, everything below claude,
    and the launch wrapper above it."""
    rows = ps_tree()
    below = [int(p) for p in descendants(pid, rows)]
    wrap = wrapper_pgid(pid, rows)
    try:
        pgid = os.getpgid(int(pid))
    except OSError:
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for g in (pgid, wrap):
            if g:
                try:
                    os.killpg(g, sig)
                except OSError:
                    pass
        for p in [int(pid)] + below:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(2)


def session_procs(rows=None):
    """{uuid: (claude_pid, pty_log)} for every running session wrapper.

    The wrapper is `script -q LOG claude …`; claude is its child. The uuid is in
    `--resume <uuid>`, or, for a session started from the panel, beside its
    launch log (new_session_uuid).
    """
    rows = rows if rows is not None else ps_tree()
    out = {}
    for pid, _pp, _st, args in rows:
        if not is_rc_args(args) or not re.match(r"\s*(\S*/)?script\s", args):
            continue
        m = re.search(r"(/tmp/rc-(?:restore|new)-[\w.-]+\.log)", args)
        if not m:
            continue
        r = RESUME_RE.search(args)
        uuid = r.group(1) if r else new_session_uuid(args)
        if not uuid:
            continue
        # claude is the child of `script` that carries --remote-control.
        # ⛔ Not simply "the first child": since the 09-25 wrapper
        # (`script … < <(cat <&3)`) script has TWO, the process-substitution
        # bash (lower pid, listed first) and claude. Taking the first made
        # find_wedged() watch that bash for zombies, so it never fired: 551f1f1d
        # sat frozen 3 h on 09-28 with two <defunct> children under claude.
        for cpid, cpp, cst, cargs in rows:
            if (cpp == pid and not cst.startswith("Z") and is_rc_args(cargs)
                    and not re.match(r"\s*(\S*/)?script\s", cargs)):
                out[uuid] = (cpid, m.group(1))
                break
    return out


def find_wedged(state, rows=None):
    """[(uuid, claude_pid, screen_silent_s, zombies)] frozen for WEDGE_S.

    A zombie seen now is only a candidate: it has to be the SAME zombie, with
    the screen not having grown by a byte, across passes spanning WEDGE_S.
    """
    rows = rows if rows is not None else ps_tree()
    now_ts = time.time()
    seen = state.get("wedge") or {}
    nxt, out = {}, []
    for uuid, (cpid, logf) in session_procs(rows).items():
        z = sorted(p for p, pp, st, _a in rows if pp == cpid and st.startswith("Z"))
        if not z:
            continue
        try:
            st = os.stat(logf)
        except OSError:
            continue
        prev = seen.get(uuid) or {}
        keep = (set(prev.get("z") or []) & set(z)) and prev.get("size") == st.st_size
        rec = {"z": z, "size": st.st_size,
               "since": prev.get("since", now_ts) if keep else now_ts}
        nxt[uuid] = rec
        silent = now_ts - st.st_mtime
        if now_ts - rec["since"] >= WEDGE_S and silent >= WEDGE_S:
            out.append((uuid, cpid, int(silent), z))
    state["wedge"] = nxt
    return out


def reap_wedged(reg, state, dry=False):
    """Unfreeze or restart what find_wedged() and find_frozen() found.

    First the main thread's stack goes to FREEZE_DIR (the next freeze must
    explain itself; 09-28 d3eb05f1 froze 90 min and left no trace), then pipe
    waiters are released, and only if the screen still does not move is the
    tree killed and marked "dead", so this same pass's top-up relaunches it
    with --resume (history kept). A turn cut off mid-way gets a nudge once the
    new process is up (deliver_nudges), so the work goes on by itself instead
    of sitting at «No response requested.» until the owner notices."""
    rows = ps_tree()
    procs = session_procs(rows)
    hits, tcc = {}, {}
    for uuid, cpid, service in find_tcc_blocked(state, procs):
        tcc[uuid] = service
        hits[uuid] = (cpid, _silent(procs[uuid][1]),
                      "the kernel holds it for a macOS privacy prompt (%s)" % _tcc_name(service))
    for uuid, cpid, silent, z in find_wedged(state, rows):
        hits.setdefault(uuid, (cpid, silent, "%d unreaped child(ren)" % len(z)))
    for uuid, cpid, silent in find_frozen(state, procs, rows):
        hits.setdefault(uuid, (cpid, silent, "a turn left open"))
    for uuid, (cpid, silent, why) in hits.items():
        rec = reg.get("sessions", {}).get(uuid) or {}
        name = rec.get("name") or uuid[:8]
        log(f"WEDGED {uuid[:8]} '{name}': screen silent {silent // 60} min, "
            f"{why} — {'would restart' if dry else 'trying to unfreeze'}")
        if dry:
            continue
        if uuid in tcc:
            _tcc_alert(state, tcc[uuid], name, uuid)
        recent = [t for t in rec.get("wedge_restarts") or [] if time.time() - t < 7200]
        if len(recent) >= 3:
            if not rec.get("wedge_parked"):
                rec["wedge_parked"] = now()
                log(f"  {uuid[:8]} froze {len(recent)} times in 2 h, leaving it for a human")
                notify(f"🧊 {name} ({uuid[:8]}) froze {len(recent) + 1} times in 2 hours; "
                       f"I stopped restarting it. It needs a look.")
            continue
        leaf, dump = (None, None) if uuid in tcc else freeze_dump(uuid, cpid)
        if dump:
            log(f"  main thread in {leaf or '?'}; stack + open files in {dump}")
        logf = (procs.get(uuid) or (None, None))[1]
        size0 = _size(logf)
        # a privacy wait is not a pipe: nothing to release, and only a click
        # on the mini's screen would end it
        moved, woke = (False, []) if uuid in tcc else try_unfreeze(cpid)
        if woke:
            log(f"  released pipe waiter(s): {', '.join(woke)}")
        if moved or (size0 is not None and _size(logf) != size0):
            log(f"  {uuid[:8]} runs again without a restart")
            for k in ("wedge", "frozen", "tcc"):
                state.get(k, {}).pop(uuid, None)
            notify(f"Session {name} ({uuid[:8]}) hung on a system file "
                   f"({', '.join(os.path.basename(w) for w in woke) or 'unknown'}); "
                   f"unfrozen without a restart.")
            continue
        started = proc_start(cpid)
        owed = turn_owed(uuid, after=(started + SETTLE_S) if started else None) is True
        log(f"  no pipe released it — restarting"
            + (", will ask it to carry on" if owed else ""))
        kill_session_tree(cpid)
        for k in ("wedge", "frozen", "tcc"):
            state.get(k, {}).pop(uuid, None)
        if rec and not rec.get("retired"):
            rec["state"] = "dead"
            rec["wedged_at"] = now()
            rec["wedge_kill"] = True
            rec["wedge_restarts"] = recent + [time.time()]
            rec.pop("wedge_parked", None)
            if owed:
                rec["nudge_at"] = time.time()
                rec["nudge_tries"] = 0
                rec["nudge_tcc"] = tcc.get(uuid)
                rec["nudge_kind"] = "freeze"
        notify(f"Session hung and was restarted: {name} ({uuid[:8]}). The screen was silent "
               f"{silent // 60} min, the process did not answer. History is kept"
               + ("; it will finish the interrupted step itself." if owed else
                  "; if you wrote to it meanwhile, send it again."))
    return list(hits)


def _size(path):
    try:
        return os.stat(path).st_size if path else None
    except OSError:
        return None


# 09-29: the owner granted Full Disk Access to CLAUDE_FIXED. Sessions still on an
# old versions/2.1.xxx binary are not covered and hang on the next protected
# folder, so move them over, but only when it costs nothing: no turn open, no
# message for MIGRATE_IDLE_S, no background job under claude (a kill would take
# it along), a couple per pass. --resume keeps history and the app card.
MIGRATE_IDLE_S = 1800
MIGRATE_PER_PASS = 2


def _last_msg_ts(uuid):
    ts = [(_rec_ts(r) or 0) for r in _tx_tail(uuid)
          if r.get("type") in ("user", "assistant", "queue-operation")]
    return max(ts) if ts else 0


def migrate_to_fixed(reg, dry=False):
    """Restart idle sessions that do not run CLAUDE_FIXED yet."""
    if IS_LINUX or not os.access(CLAUDE_FIXED, os.X_OK):
        return 0
    rows = ps_tree()
    args = {p: a for p, _pp, _st, a in rows}
    moved = 0
    for uuid, (cpid, logf) in session_procs(rows).items():
        if moved >= MIGRATE_PER_PASS:
            break
        if args.get(cpid, "").startswith(CLAUDE_FIXED + " "):
            continue
        rec = reg.get("sessions", {}).get(uuid)
        if not rec or rec.get("retired"):
            continue
        if turn_owed(uuid) is not False:
            continue                        # mid-turn or waiting on a dialog
        if time.time() - _last_msg_ts(uuid) < MIGRATE_IDLE_S:
            continue
        try:
            if time.time() - os.stat(logf).st_mtime < 600:
                continue                    # screen still moving
        except OSError:
            continue
        kids = [p for p in descendants(cpid, rows)
                if not any(p == q and (st.startswith("Z") or "caffeinate" in a)
                           for q, _pp, st, a in rows)]
        if kids:
            continue                        # a background job is running
        moved += 1
        log(f"moving idle {uuid[:8]} '{rec.get('name') or ''}' onto claude-rc "
            f"(full disk access){' (dry)' if dry else ''}")
        if dry:
            continue
        kill_session_tree(cpid)
        rec["state"] = "dead"
        rec["wedge_kill"] = True
    return moved


def _silent(logf):
    try:
        return int(time.time() - os.stat(logf).st_mtime)
    except OSError:
        return 0


# ⛔ ROOT CAUSE of the 09-28 freezes (b28ee05e 20:17, d3eb05f1 21:13, 551f1f1d
# 19:48, 3 h): claude's main thread opened something under ~/Documents, macOS
# privacy protection (TCC) asked for consent, and the kernel parks the open()
# until someone clicks Allow on the mini's screen. Nobody ever sees that
# dialog: the mini is headless, the owner is remote. `sample` shows only
# __openat_nocancel; the kernel log names it, every ~8 s, as
#   (Sandbox) watchdog expired for approval entry N (kTCCService…, pid P)
# Signals do not help: a caught signal interrupts the wait and the kernel
# restarts the open() into a new wait. The permanent fix is one click on the
# mini (Full Disk Access for claude); this is the safety net until then.
TCC_NAMES = {"kTCCServiceSystemPolicyDocumentsFolder": "Documents folder",
             "kTCCServiceSystemPolicyDesktopFolder": "Desktop folder",
             "kTCCServiceSystemPolicyDownloadsFolder": "Downloads folder",
             "kTCCServiceSystemPolicyAllFiles": "Full Disk Access",
             "kTCCServiceSystemPolicyNetworkVolumes": "network volumes",
             "kTCCServiceSystemPolicyRemovableVolumes": "removable volumes",
             "kTCCServiceSystemPolicyAppData": "other apps' data",
             "kTCCServiceMediaLibrary": "Media & Apple Music",
             "kTCCServicePhotos": "Photos"}
# ⛔ 09-29 85d2fa4f: the service can carry a suffix
# (`kTCCServiceMediaLibrary-telemetry-movies`, the Movies folder on macOS 26);
# \w+ stopped at the hyphen, so this detector missed it and the slower
# find_frozen() caught it after 5 min. Match [\w-]+ and name it by its base.


def _tcc_name(service):
    base = service.split("-")[0]
    return TCC_NAMES.get(base, base.replace("kTCCService", ""))


def tcc_waits(window_s=90):
    """{pid: service} the kernel is holding for a privacy approval right now."""
    if IS_LINUX:
        return {}
    try:
        out = subprocess.run(
            ["/usr/bin/log", "show", "--last", "%ds" % window_s, "--style", "compact",
             "--predicate", 'eventMessage CONTAINS "approval entry"'],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {int(pid): svc for svc, pid in
            re.findall(r"approval entry \d+ \((kTCC[\w-]+), pid (\d+)\)", out)}


def find_tcc_blocked(state, procs):
    """[(uuid, claude_pid, service)] held for a privacy approval on two passes."""
    waits = tcc_waits()
    seen = state.get("tcc") or {}
    nxt, out = {}, []
    now_ts = time.time()
    for uuid, (cpid, _logf) in procs.items():
        svc = waits.get(int(cpid))
        if not svc:
            continue
        prev = seen.get(uuid) or {}
        since = prev.get("since", now_ts) if prev.get("pid") == cpid else now_ts
        nxt[uuid] = {"pid": cpid, "since": since}
        if now_ts - since >= 45:
            out.append((uuid, cpid, svc))
    state["tcc"] = nxt
    return out


def _tcc_alert(state, service, name, uuid):
    """Tell the owner once per 6 h per kind of prompt what the one-time fix is."""
    sent = state.setdefault("tcc_alerted", {})
    if time.time() - sent.get(service, 0) < 6 * 3600:
        return
    sent[service] = time.time()
    notify(f"🔒 {name} ({uuid[:8]}) froze: macOS is waiting for someone to allow "
           f"claude into the {_tcc_name(service)} on the mini's screen, and nobody "
           f"sees that dialog. Restarting it. One-time fix via Screen Sharing: "
           f"System Settings → Privacy & Security → Full Disk Access → + → "
           f"⇧⌘G ~/.local/bin/claude-rc, switch it on.")


# ⛔ 09-28 d3eb05f1: frozen 90 min right after a Bash result, and find_wedged()
# never fired: it needs a <defunct> child, and there was none. The screen is
# the better tell. A TUI in the middle of a turn redraws its spinner and timer
# every second, so with `script -F` its log grows all the time; a turn that
# owes an answer (the last record is the user's prompt or a tool result) with
# a screen that has not grown for FREEZE_S is a main thread that stopped.
# Excluded on purpose: an assistant tool_use still waiting for its result (a
# question or permission dialog is a static screen too), and buffered
# pre-09-25 wrappers (no -F), whose log proves nothing.
FREEZE_S = 180
FREEZE_DIR = CFG.state("rc-freeze")
_NOT_OWED = ("<command-name>", "<local-command", "<bash-input>", "<bash-stdout>",
             "<bash-stderr>", "[Request interrupted")


_TX_ANYWHERE = {}


def _tx_file(uuid, anywhere=False):
    """The session's transcript. A session started from another folder (the
    a bot job auto-fix runs from ~) keeps writing into THAT folder's project
    even when it is resumed from RC_WORKDIR, so `anywhere` looks there too."""
    path = os.path.join(PROJ, uuid + ".jsonl")
    if not anywhere or os.path.exists(path):
        return path
    if uuid not in _TX_ANYWHERE:
        import glob
        hits = glob.glob(os.path.join(HOME, ".claude/projects", "*", uuid + ".jsonl"))
        _TX_ANYWHERE[uuid] = max(hits, key=os.path.getmtime) if hits else path
    return _TX_ANYWHERE[uuid]


def _tx_tail(uuid, anywhere=False):
    """Records from the last 256 KB of the session's transcript."""
    path = _tx_file(uuid, anywhere)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 262144))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _rec_ts(r):
    try:
        return datetime.fromisoformat((r.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def proc_start(pid):
    """Epoch the process started, or None."""
    out = sh(f"ps -o lstart= -p {int(pid)}").strip()
    try:
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


# ⛔ 09-28 23:16: a resume replays what was pending (551f1f1d got the stopped
# background task's <task-notification> as a user record 1 s after start) and
# leaves it unanswered. Read as "owes an answer", the idle resumed session was
# flagged frozen again and the resume note was skipped as "it carried on".
# Only what arrived after the process settled counts.
SETTLE_S = 15


def turn_owed(uuid, after=None):
    """True if the transcript ends on something Claude owes an answer to (a
    prompt or a tool result) written after `after`, None if on a tool call
    still waiting for its result, False otherwise (answered, interrupted,
    local command, or only leftovers from before `after`)."""
    for r in reversed(_tx_tail(uuid)):
        kind = r.get("type")
        if kind == "system" and r.get("subtype") == "turn_duration":
            return False
        if kind not in ("user", "assistant"):
            continue
        if r.get("isMeta") or r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly"):
            continue
        c = (r.get("message") or {}).get("content")
        blocks = c if isinstance(c, list) else [{"type": "text", "text": c or ""}]
        types = {b.get("type") for b in blocks if isinstance(b, dict)}
        if kind == "assistant":
            if "tool_use" in types:
                return None
            if types <= {"thinking", "redacted_thinking"}:
                continue            # thinking written, the answer not yet
            return False
        if after and (_rec_ts(r) or 0) < after:
            return False
        if "tool_result" in types:
            return True
        text = " ".join(b.get("text") or "" for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text").lstrip()
        return not text.startswith(_NOT_OWED)
    return False


def _answered_since(uuid, after, anywhere=False):
    return any(r.get("type") == "assistant" and (_rec_ts(r) or 0) > after
               for r in _tx_tail(uuid, anywhere))


def find_frozen(state, procs, rows):
    """[(uuid, claude_pid, screen_silent_s)]: a turn owes an answer and the
    unbuffered screen has not grown for FREEZE_S, seen on two passes."""
    now_ts = time.time()
    seen = state.get("frozen") or {}
    parent = {p: pp for p, pp, _st, _a in rows}
    args = {p: a for p, _pp, _st, a in rows}
    nxt, out = {}, []
    for uuid, (cpid, logf) in procs.items():
        if "script -q -F " not in args.get(parent.get(cpid), ""):
            continue
        try:
            st = os.stat(logf)
        except OSError:
            continue
        silent = now_ts - st.st_mtime
        if silent < FREEZE_S:
            continue
        started = proc_start(cpid)
        if turn_owed(uuid, after=(started + SETTLE_S) if started else None) is not True:
            continue
        prev = seen.get(uuid) or {}
        same = prev.get("size") == st.st_size and prev.get("pid") == cpid
        rec = {"size": st.st_size, "pid": cpid,
               "since": prev.get("since", now_ts) if same else now_ts}
        if same and prev.get("idle"):
            rec["idle"] = True              # already sampled idle at this size
        nxt[uuid] = rec
        if now_ts - rec["since"] < 45 or rec.get("idle"):
            continue
        # ⛔ 10-01 4b2021c5: silence + "owes an answer" is not a freeze. The
        # model's reply was stopped by Anthropic's safety classifier ("Opus
        # 5.5's safeguards stopped the response"), the session went idle,
        # and this restarted it; the resume note drew the same stop again.
        # A real freeze has the main thread stuck in a syscall (open() on a
        # privacy prompt or a pipe); idle in kevent is just waiting.
        idle = main_thread_idle(cpid)
        if idle:
            rec["idle"] = True
            log(f"{uuid[:8]} silent {int(silent) // 60} min with a turn open, but its "
                f"main thread is idle in the event loop: waiting, not frozen")
            continue
        out.append((uuid, cpid, int(silent)))
    state["frozen"] = nxt
    return out


_IDLE_LEAVES = ("kevent", "kevent64", "kevent_id", "__psynch_cvwait", "mach_msg2_trap",
                "mach_msg_trap", "__workq_kernreturn", "__semwait_signal")


def main_thread_leaf(sample_text):
    """The frame the main thread sat in for most samples, from `sample` output:
    the leaf (a line with no deeper line under it) with the highest count."""
    lines, main = [], False
    for line in sample_text.splitlines():
        if "com.apple.main-thread" in line:
            main = True
            continue
        if main:
            if re.match(r"\s*\d+\s+Thread_\d+", line) or not line.strip():
                break
            m = re.match(r"^([\s+!:|]*)(\d+)\s+(\S+)\s+\(in ", line)
            if m:
                lines.append((len(m.group(1)), int(m.group(2)), m.group(3)))
    best = {}
    for i, (ind, n, sym) in enumerate(lines):
        if i + 1 < len(lines) and lines[i + 1][0] > ind:
            continue                        # has a child: not a leaf
        best[sym] = best.get(sym, 0) + n
    return max(best, key=best.get) if best else None


def main_thread_idle(cpid):
    """True if claude's main thread is parked in its event loop (waiting, not
    stuck), False if it sits in anything else, None if it cannot be sampled."""
    try:
        out = subprocess.run(["/usr/bin/sample", str(int(cpid)), "1", "-mayDie"],
                             capture_output=True, text=True, timeout=40).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    leaf = main_thread_leaf(out)
    if not leaf:
        return None
    return leaf.lstrip("_").startswith(tuple(x.lstrip("_") for x in _IDLE_LEAVES))


def freeze_dump(uuid, cpid):
    """Save the frozen process's stacks, open files and every named pipe with
    who holds it; return (main-thread leaf frame, dump path). Keeps 40."""
    try:
        os.makedirs(FREEZE_DIR, exist_ok=True)
    except OSError:
        return None, None
    path = os.path.join(FREEZE_DIR, time.strftime("%Y%m%d-%H%M%S-") + uuid[:8] + ".txt")
    leaf = None
    try:
        subprocess.run(["/usr/bin/sample", str(int(cpid)), "1", "-mayDie", "-file", path],
                       capture_output=True, timeout=40)
        with open(path, encoding="utf-8", errors="replace") as fh:
            leaf = main_thread_leaf(fh.read())
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n==== lsof -p %s\n" % cpid)
            fh.write(subprocess.run(["lsof", "-nP", "-p", str(int(cpid))], capture_output=True,
                                    text=True, timeout=20).stdout)
            fh.write("\n==== unheld named pipes (tmp + user temp dir)\n")
            fh.write("\n".join(_unheld_fifos(True)) + "\n")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        old = sorted(os.listdir(FREEZE_DIR))
        for f in old[:-40]:
            os.unlink(os.path.join(FREEZE_DIR, f))
    except OSError:
        pass
    return leaf, path


NUDGE_TEXT = ("(automatic) This session froze in the middle of a step and was "
              "restarted with its history intact. Carry on with the task from "
              "where you stopped.")
# ⛔ 09-28 23:18: the first version named ~/Documents, ~/Desktop, ~/Downloads
# here; the resumed session obediently wrote them into its next command as
# `find -prune` exclusions and froze again on the same prompt. Merely
# mentioning a protected path in a command or its output is enough. Name no
# paths.
NUDGE_TCC = (" It froze because macOS held a file access for a privacy prompt "
             "({what}) that nobody can click on this headless Mac. Do not write "
             "any path inside the home folder's protected folders (the ones "
             "named after documents, desktop and downloads) in commands, not "
             "even to exclude them; search specific directories instead of the "
             "whole home folder.")
NUDGE_TTL_S = 900
# 20.11 the owner: the a bot job auto-fix sat 16 h on «You've hit your weekly limit».
# After a switch the migration brings such a session back, but its turn is over,
# so it would just sit there on the new account too. Say «carry on» once it is up.
NUDGE_LIMIT = ("(automatic) The Claude account this Mac was on ran out of its usage "
               "limit, so the Mac moved to another account and relaunched this session "
               "with its history intact. Carry on with the task from where you stopped.")
NUDGE_MARK = {"freeze": "froze in the middle of a step",
              "limit": "ran out of its usage limit"}
# a migration relaunches 3 a minute and many compact first: give them longer
NUDGE_TTL_LIMIT_S = 2700
_NUDGE_KEYS = ("nudge_at", "nudge_tries", "nudge_tcc", "nudge_kind")


def stopped_on_limit(uuid):
    """The session's last turn ended on Claude Code's «out of limit» reply."""
    for r in reversed(_tx_tail(uuid, anywhere=True)):
        kind = r.get("type")
        if kind not in ("user", "assistant"):
            continue
        if r.get("isMeta") or r.get("isCompactSummary") or r.get("isVisibleInTranscriptOnly"):
            continue
        return kind == "assistant" and r.get("apiError") == "usage_limit_reached"
    return False


def mark_limit_nudges(queue):
    """Of the sessions a migration brings back, flag the ones a used-up limit
    stopped; deliver_nudges() tells them to carry on once they are up."""
    n = 0
    for uuid, rec in queue:
        if rec.get("retired") or rec.get("nudge_at"):
            continue
        try:
            if not stopped_on_limit(uuid):
                continue
        except Exception:
            continue
        rec["nudge_at"] = time.time()
        rec["nudge_kind"] = "limit"
        rec.pop("nudge_tries", None)
        n += 1
    return n


def deliver_nudges(reg, dry=False):
    """Type NUDGE_TEXT into a session reap_wedged() restarted mid-turn (or
    NUDGE_LIMIT into one a used-up limit stopped, after a migration), once its
    new process is up and idle. Up to 3 tries, 15 min (45 for a limit);
    confirmed by the transcript, not by the write."""
    procs = None
    for uuid, rec in reg.get("sessions", {}).items():
        at = rec.get("nudge_at")
        if not at:
            continue
        kind = rec.get("nudge_kind") or "freeze"
        anywhere = kind == "limit"
        ttl = NUDGE_TTL_LIMIT_S if kind == "limit" else NUDGE_TTL_S
        tries = rec.get("nudge_tries") or 0
        landed = _nudge_landed(uuid, at, kind)
        if landed or rec.get("retired") or tries >= 3 or time.time() - at > ttl:
            if not landed:
                log(f"gave up asking {uuid[:8]} to carry on after the "
                    f"{'account switch' if kind == 'limit' else 'restart'} ({tries} tries)")
            for k in _NUDGE_KEYS:
                rec.pop(k, None)
            continue
        if dry:
            continue
        if procs is None:
            procs = session_procs()
        if uuid not in procs:
            continue                       # not relaunched yet
        cpid, logf = procs[uuid]
        started = proc_start(cpid)
        try:
            if (not started or time.time() - started < 20
                    or time.time() - os.stat(logf).st_mtime < 4):
                continue                   # still drawing its first screen
        except OSError:
            continue
        if _answered_since(uuid, started + SETTLE_S, anywhere):   # it carried on by itself
            for k in _NUDGE_KEYS:
                rec.pop(k, None)
            continue
        try:
            fd = os.open(fifo_path(uuid), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, b"\x7f" * 400)
                time.sleep(0.4)
                text = NUDGE_LIMIT if kind == "limit" else NUDGE_TEXT
                if rec.get("nudge_tcc") and kind != "limit":
                    text += NUDGE_TCC.format(what=_tcc_name(rec["nudge_tcc"]))
                os.write(fd, text.encode("utf-8"))
                time.sleep(0.2)
                os.write(fd, b"\r")
            finally:
                os.close(fd)
            rec["nudge_tries"] = tries + 1
            log(f"asked {uuid[:8]} to carry on after the "
                f"{'account switch' if kind == 'limit' else 'restart'}")
        except OSError as exc:
            rec["nudge_tries"] = tries + 1
            log(f"could not type into {uuid[:8]} after the restart: {exc}")


def _nudge_landed(uuid, at, kind="freeze"):
    mark = NUDGE_MARK.get(kind) or NUDGE_MARK["freeze"]
    return any(r.get("type") in ("user", "queue-operation")
               and mark in json.dumps(r, ensure_ascii=False)
               and (_rec_ts(r) or 0) >= at - 5
               for r in _tx_tail(uuid, anywhere=kind == "limit"))


def kill_restored():
    """Sessions restored under the previous account cannot survive a switch:
    their OAuth is gone and their app cards belong to the other account.

    ⛔ 10-16: this used to be `pkill -f '--resume .* --remote-control'`, which
    only matches sessions brought back with --resume. Sessions started from the
    panel (launch_new) run WITHOUT --resume, so a switch left 15 of them on the
    old account; at 22:51 its token ran out and they all died in one go. Their
    registry rows still said «restored», so the steady state took them for
    closed by hand and nobody brought them back («почти все активные сессии
    ушли в архив»). Kill every process live_uuids() would count, by pid.
    """
    husks = set(husk_pids())
    pids = []
    for pid, args in ps_lines():
        if not is_rc_args(args) or "--fork-session" in args:
            continue
        if RESUME_RE.search(args) or new_session_uuid(args) or pid in husks:
            pids.append(pid)
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    time.sleep(2)
    for pid in pids:                     # anything that ignored TERM
        try:
            os.kill(int(pid), 0)
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    return len(pids)


def kickstart_daemon():
    if IS_LINUX:
        subprocess.run(["systemctl", "--user", "restart", DAEMON_UNIT],
                       capture_output=True)
    else:
        uid = os.getuid()
        subprocess.run(["/bin/bash", "-lc",
                        f"launchctl kickstart -k gui/{uid}/{DAEMON_LABEL}"],
                       capture_output=True)
    # give the daemon time to re-register under the new account
    for _ in range(20):
        time.sleep(3)
        if daemon_pid():
            return True
    return False


def can_restore(reg):
    live = len(live_uuids()) + live_daemon_children()
    if live >= MAX_LIVE:
        return False, f"already {live} live sessions (cap {MAX_LIVE})"
    mb = free_mb()
    if mb < MIN_FREE_MB:
        return False, f"only {mb} MB free (need {MIN_FREE_MB})"
    why = pressure_reason()
    if why:
        return False, why
    return True, ""


def restore_batch(reg, candidates, limit=MAX_PER_RUN):
    """Launch the whole batch first, then collect app links on one shared
    deadline — waiting per session would make a pass outlast its 60s interval."""
    live = live_uuids()
    started = []
    digests = load(DIGEST_PATH, {})
    for uuid, rec in candidates:
        if len(started) >= limit:
            break
        if uuid in live:
            continue
        ok, why = can_restore(reg)
        if not ok:
            log(f"restore paused: {why}")
            break
        logf = launch(uuid, rec["name"], *launch_opts(uuid, rec, digests))
        rec["restores"] = rec.get("restores", 0) + 1
        rec["last_restore"] = now()
        rec.pop("wedge_kill", None)
        rec["state"] = "restored"
        started.append((uuid, rec, logf))
        log(f"launched {uuid[:8]} '{rec['name']}'")
        time.sleep(3)

    done = []
    deadline = time.time() + RESTORE_TIMEOUT
    pending = list(started)
    while pending and time.time() < deadline:
        time.sleep(4)
        for item in list(pending):
            uuid, rec, logf = item
            link = read_link(logf)
            if link:
                rec["app_session"] = link
                pending.remove(item)
                done.append((uuid, rec, link))
                log(f"restored {uuid[:8]} -> {link}")
    for uuid, rec, _logf in pending:
        done.append((uuid, rec, rec.get("app_session", "")))
        log(f"restored {uuid[:8]} (link not captured yet)")
    return done


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def backfill_links(reg, live):
    """The app link often lands in the pty log after the restore window closes.
    Pick it up on a later cycle so RC_LIVE.md ends up complete."""
    import glob
    for uuid in live:
        rec = reg.get("sessions", {}).get(uuid)
        if not rec or rec.get("app_session"):
            continue
        for logf in glob.glob(f"/tmp/rc-restore-{uuid[:8]}-*.log") + \
                    glob.glob(f"/tmp/rc-resume-*{uuid[:8]}*.log"):
            link = read_link(logf)
            if link:
                rec["app_session"] = link
                break


def snapshot(reg, ws, live):
    lines = [
        "# Remote Control — live working set",
        "",
        f"_updated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · account {account_id()}_",
        "",
        "Restore any of these by hand with:",
        "```bash",
        'claude --resume <uuid> --remote-control "<name>"',
        "```",
        "",
        "| last active | uuid | name | app link |",
        "|---|---|---|---|",
    ]
    for uuid, rec in ws:
        mark = "🟢" if uuid in live else "⚪"
        link = rec.get("app_session", "")
        link = f"https://claude.ai/code/{link}" if link else ""
        lines.append(
            f"| {mark} {rec.get('last_active','')[5:16].replace('T',' ')} | `{uuid}` "
            f"| {rec.get('name','')} | {link} |"
        )
    # Retired sessions still belong in this file. `working_set()` skips them so
    # they stop competing for MAX_LIVE slots, but dropping their row would break
    # the one promise this file makes — that nothing is ever lost. They stay
    # listed, hand-restorable, inside the same 7-day window.
    cutoff = time.time() - WORKING_WINDOW_H * 3600
    retired = [(u, r) for u, r in reg.get("sessions", {}).items()
               if r.get("kind") == "rc" and r.get("retired")
               and r.get("last_active_ts", 0) >= cutoff]
    retired.sort(key=lambda kv: kv[1].get("last_active_ts", 0), reverse=True)
    if retired:
        lines += [
            "",
            f"## Retired ({len(retired)})",
            "",
            "Not queued for auto-restore, still resumable by hand. "
            "`--unretire <prefix>` puts one back in the queue.",
            "",
            "| last active | uuid | name |",
            "|---|---|---|",
        ]
        for uuid, rec in retired:
            mark = "🗄" if rec.get("retired_by") == ARCHIVE_BY else "⛔"   # 🗄 archived in the app
            lines.append(
                f"| {mark} {rec.get('last_active','')[5:16].replace('T',' ')} | `{uuid}` "
                f"| {rec.get('name','')} |"
            )
    lines.append("")
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    with open(SNAP, "w") as fh:
        fh.write("\n".join(lines))


def notify(text):
    if not os.path.exists(TG):
        return
    subprocess.run([sys.executable, TG, text], capture_output=True, timeout=30)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _bail(_sig, _frm):
    # launchd will not start the next pass while this one runs, so a hung pass
    # would silently disable the guardian. Never let a pass outlive 150s.
    log("pass exceeded 150s — aborting so the next cycle can run")
    os._exit(1)


LOCK_PATH = CFG.state("rc-guardian.lock")


def take_lock():
    """Two passes running at once lose registry updates: each loads the file at
    start and writes it back at the end, so the slower one clobbers the faster."""
    try:
        st = os.stat(LOCK_PATH)
        if time.time() - st.st_mtime < 160:
            with open(LOCK_PATH) as fh:
                pid = fh.read().strip()
            if _pid_alive(pid):
                return False
    except OSError:
        pass
    with open(LOCK_PATH, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def _pid_alive(pid):
    if not str(pid).isdigit():
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def resolve_uuid(reg, needle):
    """Accept a full uuid or any unambiguous prefix (8 chars is what --status prints)."""
    hits = [u for u in reg.get("sessions", {}) if u.startswith(needle)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no session matches '{needle}'")
    raise SystemExit(f"'{needle}' is ambiguous: {', '.join(h[:8] for h in hits)}")


def preflight(argv):
    """--preflight [--retire-older-than H] — run this BEFORE an account switch.

    The guardian can only hold MAX_LIVE sessions. Everything touched in the last
    RESTORE_FRESH_H hours competes for those slots, and on 08-18 that was 50
    sessions for 13 slots: the newest 13 came back and the other 37 stayed ⚪ in
    RC_LIVE.md. Worse, `can_restore()` counts app/phone-created chats too, so a
    chat started during the switch takes a slot the queue was waiting for.

    This prints what will actually survive the next switch and, with
    --retire-older-than, clears the finished chats out of the way in ONE locked
    process (97 sequential --retire calls race the launchd cycle, which writes
    its stale registry copy back at the end of its pass).
    """
    if "--preflight" not in argv:
        return False
    hours = None
    if "--retire-older-than" in argv:
        i = argv.index("--retire-older-than")
        try:
            hours = float(argv[i + 1])
        except (IndexError, ValueError):
            raise SystemExit("usage: --preflight --retire-older-than <hours>")
    for _ in range(6):
        if take_lock():
            break
        time.sleep(5)
    else:
        raise SystemExit("a guardian pass is holding the lock — retry in a minute")

    reg = load(REG_PATH, {})
    # Read the app's archive state now: this runs right before a switch, and
    # whatever is archived by then must not be counted, nor carried.
    state = load(STATE_PATH, {})
    if state.get("account") in (None, account_id()):
        sync_archive(reg, state, force=True)
        save(REG_PATH, reg)
        save(STATE_PATH, state)
    live = live_uuids()
    app = live_daemon_children()
    fresh_cut = time.time() - RESTORE_FRESH_H * 3600
    queue = [(u, r) for u, r in working_set(reg, live)
             if u in live or r.get("last_active_ts", 0) >= fresh_cut]

    if hours is not None:
        cut = time.time() - hours * 3600
        hit = [(u, r) for u, r in queue
               if u not in live and r.get("last_active_ts", 0) < cut]
        for _u, rec in hit:
            rec["retired"] = True
        save(REG_PATH, reg)
        print(f"retired {len(hit)} session(s) idle more than {hours:g}h "
              f"(--unretire <prefix> brings one back)")
        for _u, rec in hit:
            print(f"  {_u[:8]}  {rec.get('last_active','')[5:16]}  {rec.get('name','')[:60]}")
        queue = [(u, r) for u, r in queue if not r.get("retired")]

    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = -1
    cores = os.cpu_count() or 8
    swap = swap_used_mb()
    slots = MAX_LIVE - (len(live) + app)

    print(f"account       : {account_id()}")
    print(f"restored live : {len(live)}   app-created: {app}   cap: {MAX_LIVE}")
    print(f"free slots    : {slots}")
    print(f"load1         : {load1:.1f} on {cores} cores "
          f"(restores block above {MAX_LOAD_PER_CPU * cores:.0f})")
    print(f"swap          : {swap} MB (logged, never blocks)")
    print(f"app archive   : {archive_line(reg, state)}")
    print(f"queue         : {len(queue)} session(s) active in the last {RESTORE_FRESH_H}h")
    print()
    verdict = "READY" if load1 <= MAX_LOAD_PER_CPU * cores and len(queue) <= MAX_LIVE \
        else "NOT READY"
    print(f"verdict       : {verdict}")
    if load1 > MAX_LOAD_PER_CPU * cores:
        print(f"  ⛔ load {load1:.1f} blocks every restore. Drain it first, then switch.")
    if len(queue) > MAX_LIVE:
        drop = len(queue) - MAX_LIVE
        print(f"  ⛔ {len(queue)} sessions for {MAX_LIVE} slots: the newest {MAX_LIVE} "
              f"come back, {drop} stay hand-restorable in RC_LIVE.md only.")
        print(f"     Clear them:  the Queue section of the panel")
    print("  ⚠ do not start new chats during the switch — an app-created chat "
          "takes a slot the queue is waiting for.")
    print()
    for n, (uuid, rec) in enumerate(queue):
        mark = "keeps " if n < MAX_LIVE else "DROPS "
        print(f"  {mark} {uuid[:8]}  {rec.get('last_active','')[5:16]}  "
              f"{rec.get('name','')[:58]}")
    return True


def edit_registry(argv):
    """--rename / --retire / --unretire. Returns True if it handled the invocation.

    Takes the lock like a normal pass: a cycle that started before us would
    load the registry, then write its stale copy back over our edit."""
    for flag in ("--rename", "--retire", "--unretire"):
        if flag in argv:
            break
    else:
        return False
    for _ in range(6):
        if take_lock():
            break
        time.sleep(5)
    else:
        raise SystemExit("a guardian pass is holding the lock — retry in a minute")
    reg = load(REG_PATH, {})
    i = argv.index(flag)
    rest = argv[i + 1:]
    if not rest:
        raise SystemExit(f"usage: {flag} <uuid-or-prefix>" +
                         (" <new name>" if flag == "--rename" else ""))
    uuid = resolve_uuid(reg, rest[0])
    rec = reg["sessions"][uuid]
    if rec.get("kind") != "rc":
        raise SystemExit(f"{uuid[:8]} is not a remote-control session")
    if flag == "--rename":
        name = " ".join(" ".join(rest[1:]).split())
        if not name:
            raise SystemExit("usage: --rename <uuid-or-prefix> <new name>")
        old, rec["name"] = rec.get("name", ""), name
        # The card carries the name passed to `claude --resume … --remote-control`,
        # so a live card keeps the old one until it is next re-materialised.
        note = " (live card keeps the old name until its next restore)" \
            if uuid in live_uuids() else ""
        print(f"{uuid[:8]}  {old!r} -> {name!r}{note}")
    else:
        rec["retired"] = (flag == "--retire")
        # a hand retire is the operator's, and unarchiving in the app must not undo it
        rec.pop("retired_by", None)
        print(f"{uuid[:8]}  {'retired' if rec['retired'] else 'un-retired'}  {rec.get('name','')}")
    save(REG_PATH, reg)
    return True


def main():
    argv = sys.argv[1:]
    signal.signal(signal.SIGALRM, _bail)
    signal.alarm(150)
    if preflight(argv):
        return
    if edit_registry(argv):
        return
    if "--stop-sessions" in argv:
        # uninstall: stop the sessions the panel or the guardian started, and
        # only those (a --resume session the user started by hand is not ours)
        known = set((load(REG_PATH, {}).get("sessions") or {}).keys())
        pids = []
        for pid, args in ps_lines():
            if not is_rc_args(args):
                continue
            m = RESUME_RE.search(args)
            if (m and m.group(1) in known) or new_session_uuid(args):
                pids.append(int(pid))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"stopped {len(pids)} session(s)")
        return
    if "--status" not in argv and not take_lock():
        log("another pass is still running — skipping this cycle")
        return
    force_all = "--restore-all" in argv
    migrate = "--migrate" in argv
    dry = "--dry-run" in argv

    reg = load(REG_PATH, {})
    state = load(STATE_PATH, {})

    if "--status" in argv:
        live = live_uuids()
        ws = working_set(reg, live)
        print(f"account       : {account_id()}")
        print(f"daemon        : pid {daemon_pid() or 'DOWN'}")
        print(f"app sessions  : {live_daemon_children()}")
        print(f"restored live : {len(live)}")
        print(f"working set   : {len(ws)}  (window {WORKING_WINDOW_H}h)")
        print(f"free memory   : {free_mb()} MB")
        pend = [u for u, r in ws if r.get("state") == "dead" and u not in live]
        print(f"pending       : {len(pend)}")
        retired = [r for r in reg.get("sessions", {}).values()
                   if r.get("kind") == "rc" and r.get("retired")]
        if retired:
            print(f"retired       : {len(retired)}  (--unretire to bring one back)")
        print(f"app archive   : {archive_line(reg, state)}")
        for uuid, rec in ws:
            flag = "LIVE" if uuid in live else rec.get("state", "-")
            print(f"  {flag:<9} {uuid[:8]}  {rec.get('last_active','')[5:16]}  {rec.get('name','')}")
        return

    if not net_ok():
        # Never restore into a dead relay: the sessions would just die again.
        # Still refresh the snapshot so nothing is ever lost, and warn early —
        # the daemon wipes every session once the relay is gone for 13 minutes.
        census(reg)
        ws = working_set(reg, set())
        live = live_uuids()
        snapshot(reg, ws, live)
        save(REG_PATH, reg)
        down = state.get("net_down_cycles", 0) + 1
        state["net_down_cycles"] = down
        if down == 4 and (len(live) + live_daemon_children()) > 0:
            notify(f"Network down for {down} min. In ~13 min the RC daemon kills all "
                   f"live sessions. The list is saved; rc-guardian brings them back "
                   f"automatically once the network returns.")
        save(STATE_PATH, state)
        log(f"network down ({down} cycles) — snapshot only, {len(ws)} in working set")
        return
    if state.get("net_down_cycles"):
        log(f"network back after {state['net_down_cycles']} cycles")
        state["net_down_cycles"] = 0

    dpid = daemon_pid()
    prev_app = state.get("app_count", 0)
    chunk = read_new_rclog(state)

    n = census(reg)
    bt = boot_time()
    if bt and state.get("boot") and abs(bt - state["boot"]) > 5:
        was_live = state.get("live_uuids") or []
        back = 0
        for _u in was_live:
            _r = reg.get("sessions", {}).get(_u)
            if _r and _r.get("kind") == "rc" and not _r.get("retired"):
                if not dry:
                    _r["state"] = "dead"
                    _r["died_at"] = now()
                    _r["reboot_keep"] = time.time()
                back += 1
        log(f"machine rebooted: {back} session(s) were live, queued to come back")
    if bt:
        state["boot"] = bt
    if not dry:
        # Do this before live_uuids(): a husk holds a MAX_LIVE slot forever and
        # would block the whole restore queue behind it.
        #
        # A reaped husk is work waiting to be picked up again, not an idle
        # session. Leaving state="restored" parks it in the panel's
        # «Простаивают», which is neither the queue nor anything the operator
        # can act on in bulk; "dead" is what «В очереди» is built from.
        #
        # Not one archived in the app: "dead" would bring it back by itself the
        # moment it is unarchived, and it must never be restored (09-24).
        for _u in reap_husks():
            _r = reg.get("sessions", {}).get(_u)
            if _r and _r.get("retired_by") != ARCHIVE_BY:
                _r["state"] = "dead"
    # A frozen session holds its slot and swallows every message: restart it
    # before live_uuids(), so this pass's top-up brings it back (see WEDGE_S).
    reap_wedged(reg, state, dry)
    deliver_nudges(reg, dry)
    migrate_to_fixed(reg, dry)
    if not dry:
        reap_stale_wrappers()
        keep_runtime_files()
        reap_unbound_new()
    live = live_uuids()
    if not dry:
        reap_orphan_fifos(live, state)
    # a running process is ground truth; heal any stale "dead" left by a crash
    for _uuid, _rec in reg.get("sessions", {}).items():
        if _uuid in live and _rec.get("state") == "dead":
            _rec["state"] = "restored"
    backfill_links(reg, live)
    cur_live = len(live) + live_daemon_children()

    victims, reason = detect_wipe(state, chunk, dpid, prev_app)

    acct = account_id()
    prev_acct = state.get("account")
    # "unknown" means ~/.claude.json was mid-write — never treat that as a switch,
    # a false positive here would kill every live session.
    acct_changed = (bool(prev_acct) and prev_acct != acct
                    and "unknown" not in (prev_acct, acct))

    # Before the working set, so a chat archived since the last read is out of
    # every queue below in this same pass. Not on a switch: the Keychain holds
    # the NEW account now, and the old one's cards are what the cache is for.
    # Forced before a manual migrate and while a sign-in runs (the last minute
    # the old account can still be read).
    if not acct_changed:
        sync_archive(reg, state, force=migrate or signin_running())

    ws = working_set(reg, live)
    restored = []

    if migrate or acct_changed:
        why = f"account switched to {acct}" if acct_changed else f"manual migrate to {acct}"
        # Carry over what was actually in use on the old account, not the whole
        # 7-day window — the rest are sessions already finished with. "In use"
        # is a restored process (live) OR a transcript touched in the last 24h:
        # app-created chats are children of the daemon, never appear in live,
        # and the kickstart below kills them, so keying on `live` alone would
        # drop exactly the sessions started from the phone or the web app.
        fresh = time.time() - RESTORE_FRESH_H * 3600
        queue = [(u, r) for u, r in ws
                 if u in live or r.get("last_active_ts", 0) >= fresh] or ws
        log(f"MIGRATE: {why} — re-materialising {len(queue)} sessions "
            f"({len(live)} were live)")
        if not dry:
            killed = kill_restored()
            kickstart_daemon()
            for _uuid, rec in queue:
                rec["state"] = "dead"
            # The kill is unconditional and instant; a 3/min drip afterwards is
            # not. On 08-08 that asymmetry killed 13 and restored 0, and on 08-18
            # it killed 12 and restored 3 of 46. The kill just freed `killed`
            # slots under MAX_LIVE, so spend them now instead of leaking them to
            # app-created chats over the next 20 cycles. Later cycles still top
            # up MAX_PER_RUN at a time.
            headroom = max(MAX_PER_RUN,
                           MAX_LIVE - (len(live_uuids()) + live_daemon_children()))
            restored = restore_batch(reg, queue, limit=headroom)
            n_lim = mark_limit_nudges(queue)
            log(f"migrate: killed {killed} old-account sessions, restored {len(restored)}"
                + (f", {n_lim} stopped by the limit will be told to carry on" if n_lim else ""))
        if dry:
            # rehearsal: say what the real run would do, page nobody
            log(f"dry-run: would kill {len(live)} restored session(s), kickstart the "
                f"daemon and re-materialise {len(queue)}, {MAX_PER_RUN}/min, "
                f"capped at MAX_LIVE={MAX_LIVE}")
            for _u, _r in queue:
                log(f"  queued {_u[:8]}  {_r.get('last_active', '')[:16]}  {_r.get('name', '')}")
        else:
            notify(f"Claude account → {acct}. Moving sessions to the new account: "
                   f"{len(restored)} already up, {max(0, len(queue) - len(restored))} queued "
                   f"(3 per minute).")
    elif force_all:
        log(f"manual --restore-all over {len(ws)} sessions")
        if not dry:
            restored = restore_batch(reg, ws, limit=MAX_PER_RUN)
    elif victims:
        # Pick victims from sessions that are NOT currently running: the newest
        # entries in ws include still-live restores, and slicing before that
        # filter would silently drop the real casualties.
        fresh = time.time() - RESTORE_FRESH_H * 3600
        pool = [(u, r) for u, r in ws
                if u not in live and r.get("last_active_ts", 0) >= fresh][:victims]
        log(f"WIPE detected: {victims} victim(s), {len(pool)} recoverable — {reason}")
        if not dry:
            # a dry run must not persist "dead" — the registry is saved either way
            for uuid, rec in pool:
                rec["state"] = "dead"
                rec["died_at"] = now()
            restored = restore_batch(reg, pool)
        if restored:
            body = "\n".join(
                f"• {r[1]['name']} — https://claude.ai/code/{r[2]}" if r[2]
                else f"• {r[1]['name']}: restoring"
                for r in restored
            )
            notify(f"Remote Control: sessions went down ({reason}).\n"
                   f"Restored {len(restored)} of {victims} automatically:\n{body}\n"
                   f"The rest follow in the next cycles.")
    else:
        # steady state: top up anything the previous cycles could not finish
        # A transcript written in the last 5 minutes has a process behind it,
        # even when that process is an app-created chat the guardian cannot see
        # in `live`. Restoring one of those spawns a SECOND claude on the same
        # transcript (happened 08-04 01:35 to 72f9e411). Never top those up.
        warm = time.time() - 300
        # Not for one reap_wedged() killed itself: nothing else runs it, and
        # waiting out the 5 minutes only leaves it dead (551f1f1d, 09-28).
        pending = [(u, r) for u, r in ws
                   if r.get("state") == "dead" and u not in live
                   and (r.get("last_active_ts", 0) < warm or r.get("wedge_kill")
                        or time.time() - float(r.get("reboot_keep") or 0) < REBOOT_KEEP_S)]
        # A restore that died within 5 minutes did not "get closed by the user" —
        # it failed to come up. Re-queue those; leave older ones alone.
        for uuid, rec in ws:
            if rec.get("state") != "restored" or uuid in live:
                continue
            try:
                age = time.time() - datetime.fromisoformat(rec["last_restore"]).timestamp()
            except (KeyError, ValueError):
                continue
            if age < 300 and rec.get("restores", 0) < 3:
                rec["state"] = "dead"
                pending.append((uuid, rec))
                log(f"re-queueing {uuid[:8]} — restore did not stay up")
        if pending:
            log(f"topping up {len(pending)} pending restore(s)")
            if not dry:
                restored = restore_batch(reg, pending)

    snapshot(reg, working_set(reg, live), live_uuids())
    state["live_uuids"] = sorted(live_uuids())
    state.update({"daemon_pid": dpid, "live_count": cur_live,
                  "app_count": live_daemon_children(), "account": acct,
                  "last_run": now()})
    save(REG_PATH, reg)
    save(STATE_PATH, state)
    swap_mb = swap_used_mb()
    log(f"ok — {n} rc transcripts, {cur_live} live, {len(ws)} in working set, "
        f"swap {swap_mb} MB"
        + (f", restored {len(restored)}" if restored else ""))
    if not dry:
        account_policy()


def account_policy():
    """Out of limit → switch to a saved account with room, or page the owner every
    10 min (rc_accounts.py). After the registry is saved and with the pass lock
    let go: a switch runs `claude auth login` for some seconds, and the panel's
    registry edits must not wait on it. A failure here never fails the pass."""
    try:
        with open(LOCK_PATH) as fh:
            if fh.read().strip() == str(os.getpid()):
                os.unlink(LOCK_PATH)
    except OSError:
        pass
    # a switch gets its own budget, still bounded: up to 3 candidates checked
    # (sign-in + /usage each) and the switch itself
    signal.alarm(300)
    try:
        import rc_accounts
        if not CFG.DEFAULT_PROVIDER:      # no Claude accounts to watch without a subscription
            rc_accounts.tick(log=log, notify=notify)
    except Exception as exc:
        log(f"accounts: policy pass failed: {exc!r}"[:300])


if __name__ == "__main__":
    main()
