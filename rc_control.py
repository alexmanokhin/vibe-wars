"""rc_control — shared session control for the web panel and the Telegram bot.

Everything that mutates the registry goes through rc_guardian's own lock, so a
launchd guardian cycle cannot load the registry, then write its stale copy back
over our edit. Unlike a guardian pass, these processes are long-lived: the lock
is released by DELETING the lock file, because take_lock() treats a missing file
as free. Holding it by staying alive would wedge the guardian for 160 s a turn.

Nothing here re-implements guardian behaviour — launch/retire go through the
guardian's own functions so the two can never disagree about a session.
"""

import codecs
import errno
import hashlib
import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
import time
import unicodedata

sys.path.insert(0, CFG.HERE)
import rc_guardian as G  # noqa: E402

WINDOW_FROM = "2026-08-15"          # matches the guardian's 168 h working window
LOCK_BUDGET_S = 30.0    # how long an edit waits for a guardian pass to finish
LOCK_POLL_S = 0.2       # ⛔ was 5 s per try: a pass holds the lock ~0.4 s, and an
                        # edit that met one slept the full 5 s after it had ended
GUARDIAN = os.path.join(CFG.HERE, "rc_guardian.py")
PY = "/usr/bin/python3"


# ---------------------------------------------------------------- lock helper

class LockBusy(RuntimeError):
    pass


# The lock FILE tells processes apart by pid, and every panel thread has the
# same pid: two threads that both find it free both "take" it and one registry
# write is lost. The panel used to hide that behind one global action lock,
# which also made a model change wait out a 25 s message delivery. Threads of
# this process now queue here instead; RLock because some edits nest.
_REG_MUTEX = threading.RLock()


def _codex_cid(uuid):
    """The Codex thread id behind a registry key (rc_codex.key_for), as it
    shows in argv; the key itself for every other session."""
    try:
        import rc_codex
        return rc_codex.cid_for(uuid)
    except Exception:
        return uuid


def with_lock(fn):
    """Run fn() holding the guardian pass lock; always release it."""
    deadline = time.time() + LOCK_BUDGET_S
    if not _REG_MUTEX.acquire(timeout=LOCK_BUDGET_S):
        raise LockBusy("another registry edit is still running — try again in a minute")
    try:
        while not G.take_lock():
            if time.time() >= deadline:
                raise LockBusy("guardian pass holds the lock — try again in a minute")
            time.sleep(LOCK_POLL_S)
        try:
            return fn()
        finally:
            try:
                os.unlink(G.LOCK_PATH)
            except OSError:
                pass
    finally:
        _REG_MUTEX.release()


# ---------------------------------------------------------------- process view

def ps_rows():
    out = subprocess.run(G.PS.split() + ["pid=,args="],
                         capture_output=True, text=True,
                         errors="replace").stdout  # см. G.sh(): ps рвёт UTF-8
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            rows.append((int(parts[0]), parts[1]))
    return rows


def ps_tree():
    """(pid, ppid, args) — нужен там, где процесс опознаётся через родителя."""
    out = subprocess.run(G.PS.split() + ["pid=,ppid=,args="],
                         capture_output=True, text=True, errors="replace").stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
    return rows


def pid_for(uuid, rows=None):
    """PID of the real claude process for a restored session.

    Skips the `script -q LOG claude --resume …` wrapper, which carries the same
    argv but is not the process to signal — same test the guardian uses.
    """
    rows = rows or ps_rows()
    vp = _vibe_pid(uuid, rows) if uuid in _codex_paths() else None
    if vp:
        return vp
    for pid, args in rows:
        # a --fork-session copy carries the uuid too, but it is another session:
        # taken for this one it blocked a relaunch as "already live" (09-28)
        if (uuid not in args and _codex_cid(uuid) not in args) or not G.is_rc_args(args) or "--fork-session" in args:
            continue
        if not (args.lstrip().startswith(G.CLAUDE) or _is_codex_main(args)):
            continue
        return pid

    # Сессия от «новой сессии» стартовала без --resume, так что её uuid не
    # попал ни в один argv. Опознаём обёртку по её логу (там uuid лежит рядом,
    # см. G.new_session_uuid) и берём claude, который под ней: обёртку сигналить
    # бесполезно, дерево то же, что у восстановленных — script → claude.
    wrapper = None
    for pid, args in rows:
        if G.new_session_uuid(args) == uuid:
            wrapper = pid
            break
    if wrapper is None:
        return None
    for pid, ppid, args in ps_tree():
        if ppid == wrapper and (args.lstrip().startswith(G.CLAUDE) or _is_codex_main(args)):
            return pid
    return None


def _is_codex_main(args):
    """The codex TUI itself, not its `script` wrapper (rc_codex sessions)."""
    first = args.lstrip().split(" ", 1)[0]
    return G.is_codex_args(args) and os.path.basename(first) == "codex"


def _vibe_pid(uuid, rows):
    """vibe-rs under the session's `script` wrapper (rc_vibe): the TUI process
    carries no flags of its own, so find the wrapper and take its child."""
    wrapper = None
    for pid, args in rows:
        if not (G.is_vibe_args(args) and re.match(r"\s*(\S*/)?script\s", args)):
            continue
        if uuid in args or _codex_cid(uuid) in args or G.new_session_uuid(args) == uuid:
            wrapper = pid
            break
    if wrapper is None:
        return None
    for pid, ppid, args in ps_tree():
        if ppid == wrapper and os.path.basename(args.lstrip().split(" ", 1)[0]).startswith("vibe"):
            return pid
    return None


def app_sessions(rows=None):
    """Chats created inside the app: daemon children, keyed by their cse_ id.

    These are the ones with no card-side kill switch, which is the whole reason
    this module exists. They carry no registry uuid, so they are reported on
    their own terms: cse id, pid, age.
    """
    out = []
    for pid, args in (rows or ps_rows()):
        if "--sdk-url" not in args or "code/sessions/cse_" not in args:
            continue
        m = re.search(r"--session-id\s+(cse_[A-Za-z0-9]+)", args)
        out.append({"pid": pid, "cse": m.group(1) if m else "?"})
    for s in out:
        s["etime"] = _etime(s["pid"])
        # The card id is the bridge id with the prefix swapped: bridge
        # cse_012LPQRwj7rxqZGPUoDbQs5X is card session_012LPQRwj7rxqZGPUoDbQs5X
        # (verified against app_session in the registry). These chats keep no
        # local transcript at all, so the card is the ONLY place to see what
        # they are — without this link the row is a pid and nothing else.
        s["link"] = ("session_" + s["cse"][4:]) if s["cse"].startswith("cse_") else ""
    return out


def _etime(pid):
    r = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                       capture_output=True, text=True).stdout.strip()
    return r or "?"


# ---------------------------------------------------------------- snapshot

# Сессия, которая сейчас работает, непрерывно дописывает свой транскрипт: блоки
# ответа, вызовы инструментов, их результаты. Тишина в файле = она ничего не
# делает. Это точнее, чем CPU: ps отдаёт усреднение за всю жизнь процесса, и у
# работающей сессии оно такое же вялое, как у спящей (замерено 08-23: 0.5-5%
# у всех подряд, при том что писала в тот момент ровно одна).
# Сколько секунд после последней записи в транскрипт сессия ещё считается
# работающей. Было 180 — бейдж WORKING держался три минуты после того, как
# сессия остыла, и список слева расходился с лентой (та считала по 30 с).
# 25-08 замерены 3281 пауза между записями в шести сессиях: половина
# короче 2 с, 90% короче 13 с, 95% короче 21 с. Работающая сессия пишет
# постоянно, так что минуты хватает с большим запасом, а остывшие больше
# не выдают себя за живых.
BUSY_S = 60


def _busy_now(tui, age):
    """Идёт ли ход прямо сейчас.

    Правда — на экране TUI: на долгом шаге транскрипт молчит минутами, и
    «свежесть записи» гасит работающую сессию (08.09, c1c2fb8e: 410 с на одном
    инструменте, экран working, строка в списке уже потухла). Ровно это правило
    лента применяет с 30.08, теперь его знает и сервер, чтобы список и шапка не
    отвечали по-разному.

    `dialog` — не работа, а ожидание человека. Пустой kind означает, что экран
    прочитать не удалось: тогда единственный оставшийся признак — возраст.
    """
    kind = (tui or {}).get("kind") or ""
    if kind in ("working", "compacting"):
        return True
    if kind in ("idle", "dialog"):
        return False
    return age is not None and age < BUSY_S

PROJECTS_PATH = CFG.state("rc-projects.json")

# Сессии не хранят cwd — гардиан поднимает их все из хоума, и все транскрипты
# лежат в одном PROJ. Значит «проект» неоткуда взять, кроме текста запроса.
# Правила живут отдельным файлом: список проектов меняется куда чаще, чем код.
DEFAULT_PROJECTS = [
    # Order matters: the FIRST matching rule wins, so a longer name must sit
    # above a shorter one it contains ("Acme Store Pro" above "Acme Store").
    # Edit <state>/rc-projects.json to define your own projects.
    {"name": "Acme Store Pro", "match": ["acme store pro", "acme pro"]},
    {"name": "Acme Store", "match": ["acme store", "acme"]},
    {"name": "Infrastructure", "match": ["vpn", "tailscale", "vps", "server", "watcher"]},
]

_PROJ_CACHE = {"mtime": None, "rules": None}


def projects():
    """Правила из файла, перечитываются по mtime — правка видна со следующего
    рендера, перезапускать панель не надо."""
    try:
        mt = os.path.getmtime(PROJECTS_PATH)
    except OSError:
        mt = None
    if _PROJ_CACHE["rules"] is not None and _PROJ_CACHE["mtime"] == mt:
        return _PROJ_CACHE["rules"]
    rules = None
    if mt is not None:
        try:
            with open(PROJECTS_PATH, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list) and loaded:
                rules = loaded
        except Exception:
            rules = None          # битый файл не должен ронять панель
    if rules is None:
        rules = DEFAULT_PROJECTS
        try:
            with open(PROJECTS_PATH, "w", encoding="utf-8") as fh:
                json.dump(rules, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    _PROJ_CACHE["mtime"], _PROJ_CACHE["rules"] = mt, rules
    return rules


NO_PROJECT = "No project"


def project_of(rec):
    # Ручная раскладка бьёт правила: автоопределение идёт по словам из первого
    # промпта и мимо него не видит ничего, так что перетащенное руками должно
    # оставаться там, куда его положили.
    manual = rec.get("project_manual")
    if manual:
        return manual
    hay = ((rec.get("name") or "") + " " + (rec.get("prompt") or "")).lower()
    for rule in projects():
        for kw in rule.get("match") or []:
            if kw in hay:
                return rule.get("name") or NO_PROJECT
    return NO_PROJECT


# ------------------------------------------------------------- устройства
#
# Хаб ходит к соседям по HTTP, а не по ssh: на каждой машине и так крутится эта
# же панель в tailnet, и она уже умеет всё — реестр, транскрипты, действия. По
# ssh пришлось бы заново реализовывать то же самое поверх чужого питона, плюс
# держать ключи. Так каждое устройство остаётся хозяином своих сессий, а хаб
# только показывает и передаёт нажатия.

DEVICES_PATH = CFG.state("rc-devices.json")


def _this_device():
    return {"id": "local", "kind": "Linux" if G.IS_LINUX else "Mac mini", "url": "",
            "name": os.uname().nodename.replace(".local", "")}


def devices():
    try:
        with open(DEVICES_PATH) as fh:
            d = json.load(fh)
        if isinstance(d, list) and d:
            return d
    except Exception:
        pass
    d = [_this_device()]
    try:
        with open(DEVICES_PATH, "w") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return d


def device_by(dev_id):
    for d in devices():
        if d.get("id") == dev_id:
            return d
    return None


def act_add_device(target):
    """Добавить соседа. Проверяем связь сразу: молча записанный адрес,
    который не отвечает, потом выглядит как поломка панели."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {name, kind, url}")
    url = (target.get("url") or "").strip().rstrip("/")
    name = (target.get("name") or "").strip()[:40]
    kind = (target.get("kind") or "").strip()[:40] or "Mac"
    if not re.match(r"^https?://[\w.\-]+(:\d+)?$", url):
        raise ValueError("url must look like http://host:port")
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/state", timeout=8) as r:
            probe = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise ValueError("%s did not answer: %s" % (url, str(exc)[:80]))
    host = ((probe.get("meta") or {}).get("host") or "").strip()
    name = name or host or url
    dev_id = re.sub(r"[^\w\-]+", "-", (host or name)).strip("-").lower()[:32] or "peer"
    cur = devices()
    if any(d.get("id") == dev_id or d.get("url") == url for d in cur):
        raise ValueError("this device is already on the list")
    cur.append({"id": dev_id, "name": name, "kind": kind, "url": url})
    with open(DEVICES_PATH, "w") as fh:
        json.dump(cur, fh, ensure_ascii=False, indent=2)
    return "added %s (%s) · %d live there" % (
        name, url, ((probe.get("meta") or {}).get("live_total") or 0))


def act_remove_device(target):
    dev_id = (target or "").strip() if isinstance(target, str) else ""
    if not dev_id or dev_id == "local":
        raise ValueError("cannot remove this device")
    cur = devices()
    left = [d for d in cur if d.get("id") != dev_id]
    if len(left) == len(cur):
        raise ValueError("no device %r" % dev_id)
    with open(DEVICES_PATH, "w") as fh:
        json.dump(left, fh, ensure_ascii=False, indent=2)
    return "removed %s" % dev_id


def _mem_total_mb():
    if G.IS_LINUX:
        return G._meminfo().get("MemTotal", 0) // 1024
    try:
        return int(G.sh("sysctl -n hw.memsize").strip()) // 1048576
    except (ValueError, AttributeError, TypeError):
        return 0


def _mem_used_mb():
    """Занято так, как считает Мониторинг системы: память приложений
    (anonymous минус purgeable) + wired + сжатое. Кэш файлов не входит.

    ⛔ 10-15: полоска Mem считала занятым всё, кроме free + speculative, и на
    16 ГБ показывала 15.8 ГБ, 99%, красным. Ядро в ту же минуту видело
    давление «норма» и 54% свободных: остальные 3 ГБ были кэшем файлов,
    который macOS отдаёт по первому требованию. владелец решил, что память
    забили зомби-сессии. free_mb остаётся гейтом для гардов: там важнее
    перестраховаться, чем показать правду.
    """
    if G.IS_LINUX:
        mi = G._meminfo()
        return max(0, mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)) // 1024
    out = G.sh("vm_stat")
    pages = {}
    for line in out.splitlines():
        m = re.match(r'"?([A-Za-z -]+?)"?:\s+(\d+)\.', line)
        if m:
            pages[m.group(1).lower()] = int(m.group(2))
    m = re.search(r"page size of (\d+)", out)
    psize = int(m.group(1)) if m else 16384
    if "anonymous pages" not in pages:
        return None
    used = (pages["anonymous pages"] - pages.get("pages purgeable", 0)
            + pages.get("pages wired down", 0)
            + pages.get("pages occupied by compressor", 0))
    return used * psize // 1048576


def snapshot():
    reg = G.load(G.REG_PATH, {})
    recs = reg.get("sessions", {})
    rows = ps_rows()
    live = G.live_uuids()
    dg = digests()
    sm = summaries()

    out = []
    for uuid, r in recs.items():
        if r.get("kind") != "rc":
            continue
        la = (r.get("last_active") or "")[:16]
        if la < WINDOW_FROM:
            continue
        is_live = uuid in live
        # A session nobody ever wrote in is a `claude -p` artefact, not a chat —
        # G.has_content() explains where they come from. Hide the dead ones the
        # same way the guardian drops them from the queue, but KEEP a live one
        # listed: it is holding a real slot, and this panel is the only place it
        # can be killed from.
        empty = not G.has_content(r)
        if empty and not is_live:
            continue
        # Возраст и «последняя активность» — по отметке последней записи.
        # mtime остаётся запасным путём: у сессии из приложения записей с
        # отметками может не оказаться в хвосте вовсе.
        ev = _last_event_ts(uuid)
        age = (time.time() - ev) if ev else _tx_age(uuid)
        _log = log_for(uuid, rows) if is_live else None
        _pid = pid_for(uuid, rows) if is_live else None
        _tui = (_live_tui(uuid, _log, tui_status(_log), _pid,
                          bool((dg.get(uuid) or {}).get("waiting")),
                          _screen_is_live(_log, rows))
                if is_live else {"kind": "", "detail": ""})
        _pr = screen_prompt(screen_lines(_log, rows)) if _log else None
        out.append({
            "uuid": uuid,
            "short": uuid[:8],
            "la": la,
            "name": (r.get("name") or "")[:140],
            "empty": empty,
            "digest": dg.get(uuid) or {},
            "summary": (sm.get(uuid) or {}).get("text", ""),
            # Заданное руками имя держим в реестре рядом с project_manual —
            # отдельный файл пришлось бы синхронизировать с ним вручную.
            "title": (r.get("title_manual") or "").strip()
                     or auto_title(r.get("name") or "",
                                   (sm.get(uuid) or {}).get("text", ""),
                                   project_of(r), uuid[:8]),
            "title_manual": bool((r.get("title_manual") or "").strip()),
            # ⛔ Не из реестра: там отметку обновляет census раз в 60 с, и
            # «27 min ago» в табе висело целую минуту после свежего сообщения.
            # mtime транскрипта — правда на момент запроса.
            "la_ts": max(ev if ev else ((time.time() - age) if age is not None
                                        else r.get("last_active_ts", 0)),
                         sent_at(uuid)),
            "retired": bool(r.get("retired")),
            "moved_host": r.get("moved_host") or "",
            "moved_from": r.get("moved_from") or "",
            "move_pending": r.get("move_pending") or None,
            "state": r.get("state") or "",
            "live": is_live,
            "pid": _pid,
            "link": r.get("app_session") or "",
            "provider": r.get("provider") or "",
            "prov_model": _prov_model(r),
            "busy_age": age,
            # Статус с экрана TUI: он говорит и там, где транскрипт молчит.
            "tui": _tui,
            # a dialog on its screen (a background agent's permission prompt):
            # the session waits for the owner, and no transcript says so (10-25)
            "perm": (_pr["summary"] or "a prompt") if _pr and _pr["kind"] != "ask" else "",
            # ⛔ «Работает» считалось по ВОЗРАСТУ последней записи транскрипта, а
            # на долгом шаге он молчит минутами: 08.09 сессия c1c2fb8e гоняла
            # инструмент 410 с, экран показывал working, а строка слева уже
            # погасла. Ленту это давно знает (renderTx смотрит на экран), список
            # — нет, и два места отвечали по-разному на один вопрос. Правда одна
            # и берётся с экрана; возраст остаётся запасным путём на случай,
            # когда экран прочитать не удалось.
            # age == 0 — это «пишет прямо сейчас», поэтому сравнение с None явно:
            # `age or BIG` съело бы ноль как ложь.
            "busy": bool(is_live and _busy_now(_tui, age)),
            "project": project_of(r),
            "model": r.get("model") or "",
            "effort": r.get("effort") or "",
            "pair": r.get("pair") or None,
            "handoff_from": (r.get("handoff_from") or "")[:8],
            "handoff_to": (r.get("handoff_to") or "")[:8],
        })
    out.sort(key=lambda x: x["la"], reverse=True)

    app = app_sessions(rows)
    u = usage_cached()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = -1.0
    try:
        import rc_providers
        providers = rc_providers.available()
    except Exception:
        providers = []
    try:
        import rc_codex
        # installed AND signed in (ChatGPT): a bare binary only fails at launch
        if rc_codex.codex_bin() and os.path.exists(os.path.join(rc_codex.CODEX_HOME, "auth.json")):
            providers.append({"id": "codex", "label": "Codex", "model": "ChatGPT login"})
    except Exception:
        pass
    try:
        import rc_vibe
        if rc_vibe.vibe_bin() and rc_providers.vibe_key():
            providers.append({"id": "vibe", "label": "Mistral Vibe",
                              "model": rc_vibe.MODELS[rc_vibe.DEFAULT_MODEL]["display"]})
    except Exception:
        pass
    meta = {
        "providers": providers,
        "live_restored": len(live),
        "live_app": len(app),
        "live_total": len(live) + len(app),
        "cap": G.MAX_LIVE,
        "queue": sum(1 for x in out if x["state"] == "dead" and not x["retired"]
                     and not x["live"]),
        "retired": sum(1 for x in out if x["retired"]),
        "load1": round(load1, 2),
        "load_gate": G.MAX_LOAD_PER_CPU * (os.cpu_count() or 8),
        "swap_mb": G.swap_used_mb(),
        # Своп без потолка ничего не говорит: 9.5 ГБ — это много или норма?
        # macOS его наращивает, поэтому потолок берём фактический, а не гадаем.
        "swap_total_mb": G.swap_total_mb(),
        "free_mb": G.free_mb(),
        # Всего памяти — знаменатель для полосок «свободно» и «своп». Без него
        # эти цифры не с чем соотнести: 149 МБ свободно на 8 ГБ и на 128 ГБ —
        # разные новости. У соседа постарше ключа может не быть, панель это
        # переживает и просто не рисует полоску.
        "mem_mb": _mem_total_mb(),
        "used_mb": _mem_used_mb(),
        # Цвет полоски Mem: 1 норма, 2 повышенное, 4 критическое (ядро).
        "pressure": G.pressure_level(),
        "account": G.account_id(),
        "busy": sum(1 for x in out if x["busy"]),
        # хаб на несколько машин: строке нужно имя хозяина ещё до того,
        # как появится второй хост
        "host": os.uname().nodename.replace(".local", ""),
        # Лимиты кладём в снимок, а не читаем локально: хабу нужны цифры того
        # устройства, чьи сессии он сейчас показывает.
        "limits": dict((k, u.get(k)) for k in
                       ("session", "session_resets", "week", "week_resets",
                        "week_fable", "week_fable_resets")),
        # Когда цифры сняты на самом деле. Без этого «52%» на экране не
        # отличить от «52% пятнадцатиминутной давности», а при неудачном
        # опросе панель показывала бы старое как свежее (см. stale_since).
        "usage_ts": u.get("ts") or 0,
        "usage_stale": bool(u.get("stale_since")),
        "accounts": _safe_accounts(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {"meta": meta, "rows": out, "app": app}


# ------------------------------------------------------------------- usage

USAGE_CACHE = CFG.state("rc-usage.json")
# Раз в минуту — по просьбе хозяина машины. Цена известна: это не чтение файла,
# а запуск настоящей CLI-сессии, до полуминуты и с её памятью. Поток спит
# ПОСЛЕ вызова, так что фактический период — минута плюс длительность самого
# вызова; наложения не будет.
USAGE_TTL = 60
# ⛔ Своя папка, как у пересказов. `claude -p` кладёт транскрипт по cwd, а cwd
# здесь был домашним — то есть каждая проверка лимитов падала пустышкой прямо
# в PROJ, к настоящим сессиям. При проверке раз в 15 минут это давало под сотню
# записей в сутки (23-08 реестр забило ровно так), а раз в минуту дало бы
# полторы тысячи. Отдельный каталог уводит их в свой проект, где census их
# не видит вовсе.
USAGE_DIR = CFG.state("rc-usage-cwd")
USAGE_KEEP_H = 24         # столько держим транскрипты-пустышки от /usage
# ⛔ Минутный опрос упирается в память, а не в разумность: каждый вызов — это
# процесс claude со своей памятью, и на мини с почти полным свопом он
# соревнуется за неё с живыми сессиями. Когда свободной памяти мало, опрос
# пропускается, а цифры остаются прошлые — но не дольше USAGE_TTL_TIGHT, иначе
# «снято N назад» под полосками честно покажет, что они устарели.
# ⛔ 26-08 этот порог был мягким: при нехватке памяти опрос откладывался, но
# через USAGE_TTL_TIGHT запускался ВСЁ РАВНО. На мини с 62 МБ свободных это
# добивало машину — панель отвечала по 27 секунд, а `claude -p /usage` держал
# 117 МБ. Теперь при нехватке памяти опрос не идёт вовсе: цифры устареют, но
# это видно в «снято N назад», а вот выдавленная сессия не восстанавливается.
USAGE_FREE_MB = 250
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
# ⛔ cwd stays the home directory, and that has a cost worth knowing about.
# Claude keys its transcript folder off cwd, so every poll drops an sdk-cli
# transcript into the guardian's PROJ, where census() cannot tell it from a real
# remote-control chat. The panel and the bot poll every 15 min each: by 08-23
# that was 67 phantom "session xxxxxxxx" entries, and being the newest files on
# disk they took the restore slots ahead of the owner's actual work.
#
# The obvious fix — point cwd at a scratch folder so they land elsewhere — does
# NOT work: an unknown directory has no trust entry in ~/.claude.json, and
# `claude -p "/usage"` there exits without running the command at all (measured
# 08-23: zero API duration, only the cost footer, no percentages to parse).
# Adding the trust entry by hand is worse — every running claude rewrites that
# file, so an outside edit races them. The phantoms are therefore filtered where
# they do damage instead: G.has_content() keeps them out of the restore queue
# and out of the panel.


def usage(force=False):
    """Real limit numbers, straight from `claude -p /usage`.

    Nothing on disk carries live consumption — ~/.claude.json holds the tier
    (default_claude_max_20x) but not what is left of it, and claude-rc.log never
    mentions limits at all. This CLI call is the only source, so it is the one
    we poll, cached because it is a real CLI session rather than a free read.

    Note the CLI's own caveat: the figures cover local sessions on THIS machine
    only — not other devices, not claude.ai. A multi-host setup has to sum them.
    """
    try:
        with open(USAGE_CACHE) as fh:
            c = json.load(fh)
        age = time.time() - c.get("ts", 0)
        if not force and age < USAGE_TTL:
            return c
        # Тесно по памяти — прошлые цифры лучше выдавленной сессии.
        if not force:
            try:
                tight = G.free_mb() < USAGE_FREE_MB
            except Exception:
                tight = False
            if tight and c.get("week") is not None:
                c["stale_since"] = time.time()
                return c
    except Exception:
        pass

    out = {"ts": time.time()}
    if CFG.DEFAULT_PROVIDER:
        # no Claude subscription on this computer: there are no Claude limits
        out["error"] = "no Claude subscription (sessions run on %s)" % CFG.DEFAULT_PROVIDER
        return out
    try:
        try:
            os.makedirs(USAGE_DIR, exist_ok=True)
        except OSError:
            pass
        r = subprocess.run([CLAUDE_BIN, "-p", "/usage"], capture_output=True,
                           text=True, timeout=300,
                           cwd=USAGE_DIR if os.path.isdir(USAGE_DIR)
                           else os.path.expanduser("~"))
        txt = (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        out["error"] = str(exc)[:200]
        return out

    out["raw"] = txt[:4000]
    for label, key in (("Current session", "session"),
                       ("Current week (all models)", "week"),
                       ("Current week (Fable)", "week_fable")):
        m = re.search(re.escape(label) + r":\s*(\d+)%\s*used(?:\s*.\s*resets\s*([^\n]+))?",
                      txt)
        if m:
            out[key] = int(m.group(1))
            out[key + "_resets"] = (m.group(2) or "").strip()

    if "week" not in out:
        # An unparseable poll must not blank a good reading. Measured 08-23: one
        # run that printed only the cost footer overwrote the cache, and the
        # panel showed "—" for the rest of the TTL. Keep the last real numbers
        # and let the next cycle try again.
        prev = usage_cached()
        if "week" in prev:
            prev["stale_since"] = out["ts"]
            return prev

    try:
        with open(USAGE_CACHE, "w") as fh:
            json.dump(out, fh)
    except OSError:
        pass
    try:
        _record_account(out)
    except Exception:
        pass
    try:
        _usage_sweep()
    except Exception:
        pass
    return out


def _usage_sweep():
    """Убрать за собой пустышки от `/usage`.

    Реестру они не мешают — лежат в своём проекте, census туда не смотрит, — но
    при опросе раз в минуту это полторы тысячи файлов в сутки, и каталог рос бы
    без конца. Держим сутки: дольше они не нужны никому.
    """
    root = os.path.dirname(G.PROJ)
    cutoff = time.time() - USAGE_KEEP_H * 3600
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    gone = 0
    for name in names:
        if "rc-usage-cwd" not in name:
            continue
        d = os.path.join(root, name)
        try:
            files = os.listdir(d)
        except OSError:
            continue
        for f in files:
            full = os.path.join(d, f)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.unlink(full)
                    gone += 1
            except OSError:
                pass
    return gone


def usage_cached():
    """Cache-only read. A page render must never wait on a CLI session — on a
    cold cache usage() can take half a minute, and the panel would hang on it.
    """
    try:
        with open(USAGE_CACHE) as fh:
            return json.load(fh)
    except Exception:
        return {}


# 05.11 the owner: «все наши аккаунты с последним треком лимитов». /usage отвечает
# только за тот аккаунт, под которым сейчас залогинен CLI, поэтому каждый
# удачный опрос записываем за этим аккаунтом. После переключения у прошлого
# аккаунта остаются его последние цифры и время, когда они сняты.
USAGE_ACCOUNTS = CFG.state("rc-usage-accounts.json")
# Аккаунты, под которыми эта машина работала (rc-guardian.log, MIGRATE),
# чтобы в списке были и те, по которым цифр ещё нет.
KNOWN_ACCOUNTS = tuple(a.strip() for a in os.environ.get("RC_KNOWN_ACCOUNTS", "").split(",") if a.strip())


def _record_account(u):
    if u.get("week") is None:
        return
    acct = G.account_id()
    if not acct or acct == "unknown":
        return
    try:
        with open(USAGE_ACCOUNTS) as fh:
            led = json.load(fh)
    except Exception:
        led = {}
    led[acct] = dict((k, u.get(k)) for k in
                     ("session", "session_resets", "week", "week_resets",
                      "week_fable", "week_fable_resets"))
    led[acct]["ts"] = u.get("ts") or time.time()
    tmp = USAGE_ACCOUNTS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(led, fh)
    os.replace(tmp, USAGE_ACCOUNTS)


def _safe_accounts():
    try:
        return accounts_usage()
    except Exception:
        return []


def _reset_passed(s, now):
    """«Nov 8 at 10am (Asia/Nicosia)» уже наступило? None, если не разобрать."""
    m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2})(?:,?\s*(\d{4}))?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
                 r"(?:\s*\(([^)]+)\))?", s or "")
    if not m:
        return None
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
        return at.timestamp() <= now
    except Exception:
        return None


def accounts_usage():
    """Все известные аккаунты: последние снятые лимиты, текущий — первым."""
    try:
        with open(USAGE_ACCOUNTS) as fh:
            led = json.load(fh)
    except Exception:
        led = {}
    cur = G.account_id()
    now = time.time()
    try:
        import rc_accounts
        pub = rc_accounts.public_state()
        saved, auto = pub.get("accounts") or {}, bool(pub.get("auto"))
    except Exception:
        saved, auto = {}, False
    names = list(dict.fromkeys([cur] + list(led) + list(KNOWN_ACCOUNTS)))
    out = []
    for a in names:
        if not a or a == "unknown":
            continue
        r = dict(led.get(a) or {})
        r["account"] = a
        r["current"] = a == cur
        # Окно уже обнулилось, а свежих цифр нет: прошлые проценты показывать
        # как текущие было бы враньём.
        r["week_reset_passed"] = bool(_reset_passed(r.get("week_resets"), now)) if r.get("week_resets") else False
        r["session_reset_passed"] = bool(_reset_passed(r.get("session_resets"), now)) if r.get("session_resets") else False
        # 20.11: can this Mac move to it by itself (rc_accounts.py)? No secrets here.
        sv = saved.get(a) or {}
        r["saved"] = bool(sv.get("saved"))
        r["saved_exp"] = sv.get("saved_exp")
        r["saved_bad"] = sv.get("saved_bad") or ""
        r["empty_until"] = sv.get("empty_until")
        r["auto"] = auto
        out.append(r)
    return out


def usage_refresher(every=USAGE_TTL):
    """Background thread that keeps USAGE_CACHE warm for cache-only readers."""
    def loop():
        while True:
            try:
                usage()
            except Exception:
                pass
            time.sleep(every)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def usage_line(u=None):
    u = u or usage()
    if "week" not in u:
        return "limits: unreadable (%s)" % u.get("error", "no data")
    return ("week %d%% · session %d%% · resets %s"
            % (u["week"], u.get("session", 0), u.get("week_resets", "?")))


# ---------------------------------------------------------------- digests

DIGEST_CACHE = CFG.state("rc-digest.json")

# Печать версии дайджеста. Кеш ключуется на (size, mtime), поэтому у
# отстоявшейся сессии файл больше не меняется и новое поле не появилось бы
# НИКОГДА. Любое новое поле — поднять номер, иначе увидят его только те, кто
# что-то написал после выкладки.
DIGEST_V = 4               # 4: a compaction's summary is not «last asked»

# Предел контекста Claude Code в транскрипт не пишет, знаем его только по
# модели. Сверено с таблицей моделей 2026-06-24: у Opus 5, Sonnet 5 и Fable 5
# окно 1M, у Haiku 4.5 — 200K. У Haiku 5.5 уже 1M (changelog CLI), поэтому
# 200K только у четвёртой.
CTX_LIMITS = (("haiku-4", 200000), ("opus", 1000000), ("sonnet", 1000000),
              ("fable", 1000000), ("mythos", 1000000))
CTX_DEFAULT = 1000000


def ctx_limit(model):
    for key, cap in CTX_LIMITS:
        if key in (model or ""):
            return cap
    return CTX_DEFAULT
DIGEST_TAIL = 256 * 1024   # holds the closing exchange in every transcript sampled
DIGEST_EVERY = 45          # seconds between sweeps
DIGEST_BATCH = 60          # files re-read per sweep, so one sweep stays cheap
TX_LIMIT = 700             # messages handed to the drawer
TX_CHARS = 6000            # per-message cap: one pasted log should not be the payload
TX_MAX_BYTES = 40 * 1024 * 1024


INTERRUPT_MARK = "[Request interrupted by user"
# uuid -> epoch of a stop act_interrupt confirmed. The screen log lags behind
# an Esc and the "age" rule counts the interrupt record itself as activity, so
# without this a stopped session read as "working" for up to a minute: the
# Stop button, the typing indicator and the green row all stayed on (09-25).
_STOPPED = {}


_TURN_MEMO = {}


def _turn_ended_at(uuid):
    """Epoch at which the last turn ended, if nothing has happened since; None
    while a turn runs.

    Every finished turn writes `system/turn_duration` (c20be4ba on 09-25:
    each human turn followed by exactly one), a stop writes the interrupt
    record. Anything conversational after them — a message, a reply, a queue
    operation — means a new turn is under way.
    """
    try:
        path = _tx_path(uuid)
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return _turn_state(uuid)[0]


_NOT_ACTIVITY = ("isMeta", "turnCompanion", "isCompactSummary",
                 "isVisibleInTranscriptOnly")


def _turn_state(uuid):
    """(ended_at, last_activity) from the transcript tail; ended_at is None
    while a turn is open, last_activity is the epoch of the newest message,
    reply, tool step or queue operation. Service records a resume writes by
    itself (summaries, meta) are not activity."""
    try:
        path = _tx_path(uuid)
        st = os.stat(path)
    except (OSError, ValueError):
        return None, None
    key = (st.st_size, st.st_mtime_ns)
    hit = _TURN_MEMO.get(uuid)
    if hit and hit[0] == key:
        return hit[1]
    end, last = None, None
    try:
        recs = _tail_records(path, st.st_size)
    except OSError:
        recs = []
    for rec in recs:
        kind = rec.get("type")
        if kind == "system" and rec.get("subtype") == "turn_duration":
            end = _epoch_ts(rec.get("timestamp")) or end
        elif kind == "user" and _is_interrupt(rec):
            end = _epoch_ts(rec.get("timestamp")) or end
        elif kind in ("user", "assistant", "queue-operation") and \
                not any(rec.get(f) for f in _NOT_ACTIVITY):
            end = None
            last = _epoch_ts(rec.get("timestamp")) or last
    _TURN_MEMO[uuid] = (key, (end, last))
    return end, last


_PROC_START = {}


def _proc_start(pid):
    """Epoch the process started, cached per pid."""
    if not pid:
        return None
    if pid in _PROC_START:
        return _PROC_START[pid]
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    try:
        ts = time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None
    _PROC_START[pid] = ts
    return ts


OPEN_TURN_MAX_S = 45 * 60


def _screen_is_live(log, rows=None):
    """True if the session's wrapper flushes its screen log on every write
    (`script -q -F`, since 09-25): then an idle screen is the truth."""
    if not log:
        return False
    return any(("script -q -F " in args and log in args)
               for _pid, args in (rows if rows is not None else ps_rows()))


def _live_tui(uuid, log, tui, pid=None, waiting=False, live_screen=False):
    """What the panel shows as the session's state.

    The screen first (_settled_tui corrects a finished turn to idle). Then the
    transcript fills the gap the screen leaves: ⛔ 09-25, c20be4ba worked for
    five minutes while its row and chat said nothing — its wrapper still
    buffered the screen log (no `script -F`), and a long step writes nothing
    to the transcript either. A turn this process started (a message, a
    background task waking it) that has not written its end is work in
    progress, whatever the stale screen says. Not for a turn an earlier
    process left cut off (7a5d5391, 1063d37b), not while it waits on a
    question, and not past OPEN_TURN_MAX_S of silence (the freeze detector
    owns that case).
    """
    tui = _settled_tui(uuid, log, tui)
    kind = (tui or {}).get("kind") or ""
    if kind in ("working", "compacting", "dialog") or waiting:
        return tui
    # A live screen that says idle is right: turns do end without a
    # turn_duration now and then (23 of 1408 on 09-25), and the rule below
    # would keep those "working" for up to OPEN_TURN_MAX_S.
    if live_screen and kind == "idle":
        return tui
    end, last = _turn_state(uuid)
    if end or not last or time.time() - last > OPEN_TURN_MAX_S:
        return tui
    start = _proc_start(pid)
    if start is None or last < start - 5:
        return tui
    return {"kind": "working", "detail": ""}


def _settled_tui(uuid, log, tui):
    """The TUI status, corrected by what the transcript knows for certain: a
    finished or stopped turn is not "working", whatever the last screen frame
    in the log still shows. Compacting is left alone — it may follow a turn."""
    tui = _stop_tui(uuid, log, tui)
    if (tui or {}).get("kind") == "working" and _turn_ended_at(uuid):
        return {"kind": "idle", "detail": ""}
    return tui


def _stop_tui(uuid, log, tui):
    """The TUI status, or idle if a confirmed stop is newer than the screen."""
    t = _STOPPED.get(uuid)
    if not t:
        return tui
    if time.time() - t > 90:
        _STOPPED.pop(uuid, None)
        return tui
    try:
        drawn = os.path.getmtime(log) if log else 0
    except OSError:
        drawn = 0
    return {"kind": "idle", "detail": ""} if drawn <= t + 1.5 else tui


def _is_interrupt(rec):
    """The user record Claude Code writes when a step is stopped with Esc:
    "[Request interrupted by user]" or "… for tool use]". Not something the
    person typed: the feed shows it as a stop marker, the digest skips it."""
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        c = next((b.get("text") for b in c
                  if isinstance(b, dict) and b.get("type") == "text"), "")
    return isinstance(c, str) and c.lstrip().startswith(INTERRUPT_MARK)


def _is_meta(rec):
    """Запись помечена как user, но человек её НЕ писал.

    ⛔ Claude Code подкладывает в ход содержимое навыков и прочую служебную
    подготовку записями `"type":"user"` — отличить их можно только по флагам
    `isMeta` / `turnCompanion` / `sourceToolUseID`. 25-08 из-за этого в сессии
    27b71a87 посреди переписки всплыл текст навыка про оформление артефактов,
    выглядевший как реплика человека. У настоящей реплики этих флагов нет
    вовсе (проверено на той же сессии: 519 записей, ровно одна подложенная).

    Фильтровать надо ВЕЗДЕ, где читаются реплики: иначе такой текст попадёт и
    в пересказ сессии, и в дайджест «на чём закончили», и может ложно
    подтвердить доставку сообщения.
    """
    return bool(rec.get("isMeta") or rec.get("turnCompanion")
                or rec.get("sourceToolUseID"))


# ⛔ 09-24: a pasted prompt lands in the transcript as
# `<pasted_content id="N">…</pasted_content id="N">`. It starts with "<", so the
# plumbing filter below dropped it: the chat pane never showed it and _delivered
# never confirmed it (c20be4ba: delivered at 16:11, panel said not sent).
_PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")


def _msg_text(rec, flat=True):
    """Readable text of one transcript record, '' if it carries none.

    Slash commands, the local-command caveat and pure tool_use blocks are
    plumbing. Skipping them is what keeps `<command-name>/usage` from being
    rendered as something the owner said.
    """
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "".join(p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(c, str):
        return ""
    c = _PASTE_TAG.sub("", c)
    if flat:
        c = " ".join(c.split())
    else:
        # Markdown is line-based, and " ".join(c.split()) flattens it: every
        # heading, bullet and fenced block arrives as one run of asterisks and
        # hashes. The digest still wants one line — the drawer does not.
        c = "\n".join(ln.rstrip() for ln in c.splitlines()).strip()
    if not c or c.startswith("<") or c.startswith("Caveat"):
        return ""
    return c


# Хвоста в 64 КБ хватает: записи дописываются в конец, а одна запись — это
# редко больше пары килобайт.
EVENT_TAIL = 65536
_EVENT_RE = re.compile(br'"timestamp":"([^"]{19})')
_EVENT_MEMO = {}


def _last_event_ts(uuid):
    """Когда в сессии в последний раз что-то ПРОИСХОДИЛО, в секундах эпохи.

    ⛔ Не mtime файла. Процесс живой сессии переписывает служебные записи
    (permission-mode, atis-latch, bridge-session), не добавляя ни одной новой —
    файл «свежеет» сам по себе, без всякой работы. 25-08 из-за этого e1352db5
    с последней записью от 24-08 показывалась в списке как «2 минуты назад» и
    числилась работающей; тот же обман раньше выглядел как «сессии просыпаются
    сами после перезагрузки».

    JSON не разбираем: нужна одна отметка, а регулярка по хвосту дешевле
    разбора сотен записей на каждой отрисовке страницы.
    """
    try:
        path = _tx_path(uuid)
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, int(st.st_mtime))
    if key in _EVENT_MEMO:
        return _EVENT_MEMO[key]
    # max, а не последнее вхождение: порядок записей в файле обычно совпадает с
    # временем, но полагаться на это незачем.
    # A compact summary or a big tool result can be longer than the tail, and
    # a tail of bridge records alone has no conversation at all: widen it before
    # giving up, or the file mtime (bumped by every restore) wins again.
    best = 0
    for tail in (EVENT_TAIL, 8 * EVENT_TAIL, 64 * EVENT_TAIL):
        try:
            with open(path, "rb") as fh:
                if st.st_size > tail:
                    fh.seek(st.st_size - tail)
                blob = fh.read()
        except OSError:
            return None
        best = _conv_ts(blob)
        if best or st.st_size <= tail:
            break
    out = best or None
    if len(_EVENT_MEMO) > 4000:
        _EVENT_MEMO.clear()
    _EVENT_MEMO[key] = out
    return out


def _conv_ts(blob):
    # ⛔ 10-16: only conversation counts. Every restore writes a timestamped
    # `system` record («/remote-control is active», «Remote Control
    # disconnected»), so 16 sessions brought back after the account switch read
    # as «done 6 min ago · your move» and jumped to the top of HQ, one of them
    # untouched for 3 days. Messages and their tool results are the activity.
    best = 0
    for line in blob.split(b"\n"):
        if b'"type":"assistant"' not in line and not (
                b'"type":"user"' in line and b'"isMeta":true' not in line):
            continue
        for hit in _EVENT_RE.findall(line):
            best = max(best, _epoch_ts(hit.decode("ascii", "replace")))
    return best


def _tx_age(uuid):
    """Сколько секунд назад в транскрипт писали. None — файла ещё нет."""
    try:
        return int(time.time() - os.path.getmtime(_tx_path(uuid)))
    except OSError:
        return None


# ─── Agents a session runs ────────────────────────────────────────────────
# the owner 10-16: «в сессии 72aab730 работаем 5 агентов, но этого нигде не видно».
# Agents a session launches (the Agent tool, forks, Workflow runs) live inside
# its own claude process and write their own transcripts next to its one:
#   <PROJ>/<uuid>/subagents/agent-<id>.jsonl   (+ .meta.json: description, type)
#   <PROJ>/<uuid>/subagents/workflows/<run>/agent-<id>.jsonl   (+ journal.jsonl)
# The session's own turn ends right after a background launch, so its row read
# «idle» for the whole hour five forks were building. An agent counts as
# running while all of these hold:
#   - its transcript does not end on a finished reply (end_turn, no tool call);
#   - this process wrote it: a restart kills every agent and leaves the files
#     cut mid-turn forever, so a file older than the process start is history;
#   - the session has not been told it stopped: a <task-notification> naming
#     it arrives on every stop (completed, failed, killed), and a workflow
#     journal writes a `result` per agent;
#   - it wrote something within AGENT_QUIET_S (a wedged or killed one that
#     slipped past the notice does not count forever).
AGENT_QUIET_S = 30 * 60
AGENT_TAIL = 65536
AGENT_TAIL_CAP = 4 * 1024 * 1024
NOTE_SCAN_FIRST = 4 * 1024 * 1024   # the first look at a session transcript: its tail
_AGENT_MEMO = {}      # agent transcript -> ((size, mtime_ns), (open, last_ts, doing))
_AGENT_META = {}      # meta.json -> (mtime_ns, dict)
_WF_JOURNAL = {}      # journal.jsonl -> ((size, mtime_ns), finished agent ids)
_NOTE_MEMO = {}       # session uuid -> {"off", "first", "done": {agent id: epoch}}
_NOTE_RE = re.compile(br'<task-notification>(?:\\n|\s)*<task-id>(a[0-9a-f]{16})</task-id>')
_DONE_STOPS = ("end_turn", "stop_sequence", "refusal")


def _reply_done(rec):
    """The main agent's finished reply: no tool call, not an API error."""
    msg = rec.get("message") or {}
    blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
    return (rec.get("type") == "assistant" and msg.get("stop_reason") in _DONE_STOPS
            and not rec.get("isApiErrorMessage") and not rec.get("isSidechain")
            and not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks))


def _tail_recs(path, size, first=AGENT_TAIL, cap=AGENT_TAIL_CAP):
    """Records from the end of a JSONL file. Reads further back until a whole
    record is in: one tool result can be a single 300 KB line."""
    n = first
    while True:
        with open(path, "rb") as fh:
            if size > n:
                fh.seek(size - n)
                blob = fh.read()
                cut = blob.find(b"\n")       # the first line is a fragment
                blob = blob[cut + 1:] if cut >= 0 else b""
            else:
                blob = fh.read()
        out = []
        for line in blob.split(b"\n"):
            if line.strip():
                try:
                    out.append(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    pass
        if out or size <= n or n >= cap:
            return out
        n *= 4


def _step_label(name, inp):
    """«Bash: Run the backend tests», «Edit: play.js»: the agent's last step,
    the same line the terminal shows under each running agent."""
    lab = _tool_label(inp)
    if lab and lab[:1] in "/~" and " " not in lab:
        lab = os.path.basename(lab.rstrip("/")) or lab
    return ((name or "step") + (": " + lab if lab else ""))[:90]


def _agent_state(path, st):
    """(open, last_ts, doing) of one agent transcript, memoised on size+mtime.
    open is False once its last message is a finished reply."""
    key = (st.st_size, st.st_mtime_ns)
    hit = _AGENT_MEMO.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        recs = _tail_recs(path, st.st_size)
    except OSError:
        recs = []
    conv = [r for r in recs if r.get("type") in ("user", "assistant")]
    is_open, last, doing = True, 0, ""
    if conv:
        end = conv[-1]
        last = _epoch_ts(end.get("timestamp"))
        msg = end.get("message") or {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        calls = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
        # An API error reply can still be retried; the notice settles it.
        if (end.get("type") == "assistant" and msg.get("stop_reason") in _DONE_STOPS
                and not calls and not end.get("isApiErrorMessage")):
            is_open = False
    for r in reversed(conv):
        c = (r.get("message") or {}).get("content")
        if r.get("type") != "assistant" or not isinstance(c, list):
            continue
        calls = [b for b in c if isinstance(b, dict) and b.get("type") == "tool_use"]
        if calls:
            doing = _step_label(calls[-1].get("name"), calls[-1].get("input"))
            break
    out = (is_open, last or st.st_mtime, doing)
    if len(_AGENT_MEMO) > 2000:
        _AGENT_MEMO.clear()
    _AGENT_MEMO[path] = (key, out)
    return out


def _agent_meta(path):
    try:
        mt = os.stat(path).st_mtime_ns
    except OSError:
        return {}, 0
    hit = _AGENT_META.get(path)
    if hit and hit[0] == mt:
        return hit[1], mt / 1e9
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if len(_AGENT_META) > 2000:
        _AGENT_META.clear()
    _AGENT_META[path] = (mt, meta)
    return meta, mt / 1e9


def _wf_finished(run_dir):
    """Agent ids a workflow run's journal already holds a result for."""
    path = os.path.join(run_dir, "journal.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return set()
    key = (st.st_size, st.st_mtime_ns)
    hit = _WF_JOURNAL.get(path)
    if hit and hit[0] == key:
        return hit[1]
    done = set()
    try:
        with open(path, "rb") as fh:
            for line in fh:
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if (isinstance(rec, dict) and rec.get("agentId")
                        and rec.get("type") not in ("started", "launched")):
                    done.add(rec["agentId"])
    except OSError:
        pass
    _WF_JOURNAL[path] = (key, done)
    return done


def _stop_notes(uuid):
    """{agent id: epoch of the newest notice the session got that it stopped}.

    Read incrementally from where the last call stopped: a session transcript
    runs past 100 MB (72aab730: 113 MB), and only the end of it can matter.
    """
    path = _tx_path(uuid)
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    m = _NOTE_MEMO.get(uuid)
    if m is None or size < m["off"]:
        m = _NOTE_MEMO[uuid] = {"off": max(0, size - NOTE_SCAN_FIRST),
                                "first": True, "done": {}}
    if size <= m["off"]:
        return m["done"]
    try:
        with open(path, "rb") as fh:
            fh.seek(m["off"])
            blob = fh.read(size - m["off"])
    except OSError:
        return m["done"]
    end = blob.rfind(b"\n")
    if end < 0:
        return m["done"]                 # one record still being written
    body = blob[:end]
    if m["first"] and m["off"] > 0:
        cut = body.find(b"\n")           # started mid-record
        body = body[cut + 1:] if cut >= 0 else b""
    m["first"] = False
    m["off"] += end + 1
    if b"<task-notification>" in body:
        for line in body.split(b"\n"):
            if b"<task-notification>" not in line:
                continue
            hit = _EVENT_RE.search(line)
            ts = _epoch_ts(hit.group(1).decode("ascii", "replace")) if hit else time.time()
            for aid in _NOTE_RE.findall(line):
                k = aid.decode("ascii")
                if ts > m["done"].get(k, 0):
                    m["done"][k] = ts
    return m["done"]


def agents_running(uuid, pid, now=None):
    """Agents this live session runs right now, longest-running first:
    [{"id", "desc", "type", "since", "last", "doing", "depth", "wf"}]."""
    base = os.path.join(G.PROJ, uuid, "subagents")
    if not pid or not os.path.isdir(base):
        return []
    now = now or time.time()
    born = _proc_start(pid) or 0
    cands = []

    def scan(folder, wf):
        try:
            ents = list(os.scandir(folder))
        except OSError:
            return []
        for e in ents:
            n = e.name
            if not (n.startswith("agent-") and n.endswith(".jsonl")):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            if st.st_mtime < born - 5 or now - st.st_mtime > AGENT_QUIET_S:
                continue
            cands.append((e.path, st, wf))
        return ents

    for e in scan(base, None):
        if e.name == "workflows" and e.is_dir():
            try:
                runs = [r for r in os.scandir(e.path) if r.is_dir()]
            except OSError:
                runs = []
            for run in runs:
                try:
                    if now - run.stat().st_mtime > AGENT_QUIET_S and \
                            run.stat().st_mtime < born:
                        continue
                except OSError:
                    continue
                scan(run.path, run.path)
    out = []
    notes = None
    for path, st, wf in cands:
        aid = os.path.basename(path)[6:-6]
        is_open, last, doing = _agent_state(path, st)
        if not is_open:
            continue
        if wf and aid in _wf_finished(wf):
            continue
        if notes is None:
            notes = _stop_notes(uuid)
        stopped = notes.get(aid, 0)
        if stopped and stopped >= last - 1:
            continue
        meta, launched = _agent_meta(path[:-6] + ".meta.json")
        since = launched or st.st_mtime
        if stopped and stopped > since:
            since = stopped              # resumed after a stop: this run
        kind = meta.get("agentType") or ""
        if meta.get("isFork"):
            kind = "fork"
        elif kind == "workflow-subagent":
            kind = "workflow"
        out.append({"id": aid, "desc": (meta.get("description") or "")[:120],
                    "type": kind, "since": int(since), "last": int(last),
                    "doing": doing, "depth": int(meta.get("spawnDepth") or 1),
                    "wf": os.path.basename(wf) if wf else ""})
    out.sort(key=lambda a: a["since"])
    return out


# ─── Статус самого Claude Code ────────────────────────────────────────────
# Транскрипт молчит ровно тогда, когда человеку интереснее всего: во время
# компакта, длинного хода и пока сообщение стоит в очереди в него не падает
# ни строки, и бейдж по свежести записи гаснет, хотя работа идёт (30-08,
# «/compact и никакого статуса»). Экран TUI про это говорит прямо, а `script -q`
# пишет его целиком в pty-лог сессии — оттуда и берём.
_TUI_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TUI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_TUI_CHARSET = re.compile(r"\x1b[()][A-Za-z0-9]")
_TUI_LOG_RE = re.compile(r"(/tmp/rc-[^\s]+\.log)")
TUI_TAIL = 120000            # хвоста экрана хватает: спиннер перерисовывается часто
# Экран статичен — значит, сессия не работает. Маркер «работает» иначе
# залипает навсегда: строка «38s · ↓15.9k tokens» остаётся в логе и после
# того, как ход закончился (30-08: сессия стояла 308 с, а статус горел).
# Порог с запасом: у работающей сессии `script` пишет пачками, замеренный
# разрыв доходил до 9 с.
TUI_FRESH_S = 40
_TUI_MEMO = {}

# Порядок не приоритет, а разбор: побеждает тот маркер, что встретился в файле
# ПОЗЖЕ остальных — лог дописывается, поэтому позиция и есть свежесть.
_TUI_MARKS = (
    ("compacting", re.compile(r"Compacting\s*conversation")),
    ("queued", re.compile(r"Press\s*up\s*to\s*edit\s*queued\s*messages")),
    ("working", re.compile(r"(\d+)s\s*·\s*↓\s*([\d.]+k?)\s*tokens")),
    # Модалка «научить авто-режим окружению» встаёт поверх поля ввода у СВЕЖЕЙ
    # сессии и глотает набранное. Экран при этом статичен, поэтому без своего
    # маркера он читается как «idle», панель спокойно печатает в модалку, текст
    # уходит в никуда и остаётся только «not confirmed» без единой причины.
    # 07.09 так пропало сообщение в 56e32cf6, до этого 05.09 — первые сообщения
    # в каждую перевезённую сессию. Маркер узкий по заголовку: любую ДРУГУЮ
    # модалку (вопрос о разрешении, выбор файла) трогать нельзя, на неё человек
    # отвечает сам.
    ("dialog", re.compile(r"Teach\s*auto\s*mode\s*about\s*your\s*environment")),
)

# Сколько ждать, пока модалка уйдёт с экрана после Esc.
DIALOG_ESC_WAIT_S = 1.5


def _dismiss_noise_dialog(uuid, fifo):
    """Снять модалку авто-режима, если она перехватила поле ввода.

    Esc, а не Enter: Enter — это «Continue», то есть согласие сканировать
    историю шелла и чужие репозитории. Такое решение принимает человек, а не
    доставка сообщения. Возвращает True, если модалка была и её сняли.
    """
    log = log_for(uuid)
    if not log or tui_status(log).get("kind") != "dialog":
        return False
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, b"\x1b")
    finally:
        os.close(fd)
    time.sleep(DIALOG_ESC_WAIT_S)
    return True


def log_for(uuid, rows=None):
    """pty-лог живой сессии. None — обёртки нет (сессия не поднята отсюда)."""
    for pid, args in (rows if rows is not None else ps_rows()):
        if "script -q " not in args:
            continue
        m = _TUI_LOG_RE.search(args)
        if not m:
            continue
        if uuid in args or _codex_cid(uuid) in args or G.new_session_uuid(args) == uuid:
            return m.group(1)
    return None


_CODEX_TUI = {}


def tui_status(path):
    """Что Claude Code делает прямо сейчас, по последнему экрану TUI.

    Возвращает {"kind": …, "detail": …}; kind — compacting | queued | working
    | idle. Кэш ключуется на (size, mtime): у спящей сессии лог не растёт, и
    повторное чтение 120 КБ на каждый опрос панели ей не нужно.
    """
    if not path:
        return {"kind": "", "detail": ""}
    try:
        st = os.stat(path)
    except OSError:
        return {"kind": "", "detail": ""}
    if "/rc-" in path:
        # a Codex or Vibe TUI says its state outright (OSC 7501); cached per
        # log version, since every Claude log is asked too and answers ''
        ck = (path, st.st_size, st.st_mtime_ns)
        cx = _CODEX_TUI.get(path)
        if not cx or cx[0] != ck:
            try:
                import rc_codex
                cx = (ck, rc_codex.tui_state(path))
            except Exception:
                cx = (ck, "")
            _CODEX_TUI[path] = cx
        cx = cx[1]
        if cx:
            return {"kind": "working" if cx == "working" else "idle", "detail": ""}
    key = (path, st.st_size, int(st.st_mtime))
    if key in _TUI_MEMO:
        return _tui_fresh(_TUI_MEMO[key], st.st_mtime)
    try:
        with open(path, "rb") as fh:
            if st.st_size > TUI_TAIL:
                fh.seek(st.st_size - TUI_TAIL)
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return {"kind": "", "detail": ""}
    flat = _TUI_CHARSET.sub("", _TUI_OSC.sub("", _TUI_ANSI.sub("", blob)))
    flat = flat.replace("\r", "\n")
    best = None
    for kind, rx in _TUI_MARKS:
        last = None
        for m in rx.finditer(flat):
            last = m
        if last and (best is None or last.start() > best[0]):
            best = (last.start(), kind, last)
    if best is None:
        out = {"kind": "idle", "detail": ""}
    elif best[1] == "working":
        # Секунды с экрана не берём: TUI перерисовывает строку по частям, и
        # в хвосте лога полная строка встречается реже спиннера — счётчик
        # прыгал 10 → 70 → 10 и выглядел сломанным. Сколько идёт работа,
        # панель считает сама по busy_age: он монотонный.
        out = {"kind": "working", "detail": "↓%s" % best[2].group(2)}
    else:
        out = {"kind": best[1], "detail": ""}
    if len(_TUI_MEMO) > 2000:
        _TUI_MEMO.clear()
    _TUI_MEMO[key] = out
    return _tui_fresh(out, st.st_mtime)


def _tui_fresh(out, mtime):
    """Живой статус — только у живого экрана.

    Проверка снаружи кэша: она зависит от «сейчас», а ключ кэша — от файла.
    """
    if out["kind"] in ("working", "compacting", "queued") \
            and time.time() - mtime > TUI_FRESH_S:
        return {"kind": "idle", "detail": ""}
    return out


# ---------------------------------------------------------------- the screen
#
# ⛔ 10-25: sends went into sessions whose screen showed a background agent's
# permission prompt («Dangerous rm operation… Do you want to proceed? ❯ 1. Yes»).
# The text went to the input field or nowhere, the closing \r picked the
# highlighted «Yes», and the panel said «accepted the write, but not in the
# transcript»: the message lost and an `rm` approved on the owner's behalf (ed6b735c
# twice, 7589385a three times; one of those prompts had sat unseen for 8 hours).
# No transcript holds such a prompt, and the markers in tui_status cannot see
# it: Claude Code's renderer writes only the cells that changed, so a prompt
# that sits still is not in the log tail at all, and even its first frame skips
# letters that match the frame before. So the screen is rebuilt: the pty log
# replayed through a small terminal emulator. Claude Code draws a 24x80
# alternate screen with absolute cursor moves, and that subset is all this
# implements; checked against pyte on three live logs at random offsets and
# random chunk sizes, the screens were identical.
SCREEN_ROWS, SCREEN_COLS = 24, 80     # what `script` gives a claude with no terminal
SCREEN_REPLAY_MAX = 16 << 20          # how far back a fresh replay starts
_VT_TOKEN = re.compile(
    r"\x1b\[([0-?]*)[ -/]*([@-~])"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC (the window title)
    r"|\x1b[()][0-9A-Za-z]"                 # charset
    r"|\x1b([0-Z\\^-~])"                    # other ESC; 7 and 8 save/restore the cursor
    r"|([\r\n\x08])"
    r"|[\x00-\x07\x09-\x0c\x0e-\x1a\x1c-\x1f]")
_VT_HOLD_MAX = 4096                   # an unfinished sequence waits for the next read


class VtScreen(object):
    """Just enough of a VT100 to know what Claude Code's screen shows."""

    def __init__(self, rows=SCREEN_ROWS, cols=SCREEN_COLS):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.y = self.x = 0
        self.top, self.bot = 0, rows - 1
        self.saved = (0, 0)
        self.hold = ""
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")

    def _put(self, s):
        cols = self.cols
        for ch in s:
            o = ord(ch)
            if o > 0x2ff and unicodedata.combining(ch):
                continue
            w = 2 if (o > 0x10ff and unicodedata.east_asian_width(ch) in ("W", "F")) else 1
            if self.x + w > cols:
                self.x = 0
                self._lf()
            row = self.grid[self.y]
            row[self.x] = ch
            if w == 2 and self.x + 1 < cols:
                row[self.x + 1] = ""
            self.x += w

    def _lf(self):
        if self.y == self.bot:
            del self.grid[self.top]
            self.grid.insert(self.bot, [" "] * self.cols)
        elif self.y < self.rows - 1:
            self.y += 1

    def feed(self, raw):
        data = self.hold + self.dec.decode(raw)
        self.hold = ""
        cut = data.rfind("\x1b", max(0, len(data) - _VT_HOLD_MAX))
        if cut != -1 and not _VT_TOKEN.match(data, cut):
            self.hold, data = data[cut:], data[:cut]
        pos = 0
        for m in _VT_TOKEN.finditer(data):
            if m.start() > pos:
                self._put(data[pos:m.start()])
            pos = m.end()
            if m.group(2) is not None:
                self._csi(m.group(1), m.group(2))
            elif m.group(4) is not None:
                c = m.group(4)
                if c == "\r":
                    self.x = 0
                elif c == "\n":
                    self._lf()
                elif self.x > 0:
                    self.x -= 1
            elif m.group(3) == "7":
                self.saved = (self.y, self.x)
            elif m.group(3) == "8":
                self.y, self.x = self.saved
        if pos < len(data):
            self._put(data[pos:])

    def _blank(self):
        return [" "] * self.cols

    def _csi(self, params, fin):
        if params[:1] in ("?", ">", "<", "="):
            if fin in ("h", "l") and "1049" in params:     # alternate screen
                self.grid = [self._blank() for _ in range(self.rows)]
                self.y = self.x = 0
            return
        try:
            nums = [int(p) if p else 0 for p in params.split(";")] if params else []
        except ValueError:
            return
        a = nums[0] if nums else 0
        rows, cols = self.rows, self.cols
        if fin in ("H", "f"):
            r = (nums[0] if nums and nums[0] else 1) - 1
            c = (nums[1] if len(nums) > 1 and nums[1] else 1) - 1
            self.y, self.x = min(max(r, 0), rows - 1), min(max(c, 0), cols - 1)
        elif fin == "A":
            self.y = max(0, self.y - (a or 1))
        elif fin == "B":
            self.y = min(rows - 1, self.y + (a or 1))
        elif fin == "C":
            self.x = min(cols - 1, self.x + (a or 1))
        elif fin == "D":
            self.x = max(0, self.x - (a or 1))
        elif fin == "G":
            self.x = min(max((a or 1) - 1, 0), cols - 1)
        elif fin == "d":
            self.y = min(max((a or 1) - 1, 0), rows - 1)
        elif fin == "K":
            row = self.grid[self.y]
            if a == 0:
                row[self.x:] = [" "] * (cols - self.x)
            elif a == 1:
                row[:self.x + 1] = [" "] * (self.x + 1)
            else:
                row[:] = self._blank()
        elif fin == "J":
            if a == 0:
                self.grid[self.y][self.x:] = [" "] * (cols - self.x)
                for r in range(self.y + 1, rows):
                    self.grid[r] = self._blank()
            elif a == 1:
                for r in range(self.y):
                    self.grid[r] = self._blank()
                self.grid[self.y][:self.x + 1] = [" "] * (self.x + 1)
            else:
                self.grid = [self._blank() for _ in range(rows)]
        elif fin == "r":
            t = (nums[0] if nums and nums[0] else 1) - 1
            b = (nums[1] if len(nums) > 1 and nums[1] else rows) - 1
            self.top, self.bot = (t, b) if 0 <= t < b < rows else (0, rows - 1)
            self.y = self.x = 0

    def lines(self):
        return ["".join(r).rstrip() for r in self.grid]


_SCREENS = {}                 # log -> [script pid, bytes fed, VtScreen]
_SCREENS_MU = threading.Lock()


def screen_lines(log, rows=None):
    """The session's screen as it is now: SCREEN_ROWS lines of text, or None.

    One emulator per log, fed only what was written since the last call (the
    first call replays the whole log: ~0.35 s for 4 MB). A relaunch truncates
    the log and starts a new `script`, so the cache is keyed on its pid."""
    if not log:
        return None
    owner = None
    for pid, args in (rows if rows is not None else ps_rows()):
        if "script -q " in args and log in args:
            owner = pid
            break
    if owner is None:
        return None
    try:
        size = os.path.getsize(log)
    except OSError:
        return None
    with _SCREENS_MU:
        ent = _SCREENS.get(log)
        if ent is None or ent[0] != owner or size < ent[1]:
            ent = [owner, max(0, size - SCREEN_REPLAY_MAX), VtScreen()]
            _SCREENS[log] = ent
        try:
            with open(log, "rb") as fh:
                fh.seek(ent[1])
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    ent[2].feed(chunk)
                    ent[1] += len(chunk)
        except OSError:
            _SCREENS.pop(log, None)
            return None
        return ent[2].lines()


_MENU_OPT = re.compile("^\\s*(❯)?\\s*(\\d)\\.\\s+(\\S.*?)\\s*$")
_MENU_BORDER = re.compile("─{40,}\\s*$")


def screen_prompt(lines):
    """A menu waiting at the bottom of the screen, or None.

    {"kind": "permission" | "ask" | "menu", "text": the dialog as drawn,
    "summary": one line of it, "options": [{"n", "label"}], "sig": its hash}.
    A dialog takes the place of the input box, so its «Esc to cancel» footer is
    the last thing on the screen and one option carries the ❯ cursor; the same
    words quoted higher up in the conversation are not a dialog.
    """
    if not lines:
        return None
    rows = list(lines)
    while rows and not rows[-1].strip():
        rows.pop()
    foot = None
    for i in range(len(rows) - 1, max(-1, len(rows) - 3), -1):
        if "Esc to cancel" in rows[i]:
            foot = i
            break
    if foot is None:
        return None
    opts, first, cursor = {}, None, None
    for i in range(foot - 1, max(-1, foot - 16), -1):
        m = _MENU_OPT.match(rows[i])
        if m:
            n = int(m.group(2))
            if n in opts:
                break
            opts[n] = m.group(3)
            first = i
            if m.group(1):
                cursor = n
        elif _MENU_BORDER.search(rows[i]):
            break
    if 1 not in opts or cursor is None:
        return None
    # The dialog starts under its top border. A dialog taller than the screen
    # has none in view: then it starts below the last conversation bullet.
    top = 0
    for i in range(first - 1, -1, -1):
        if _MENU_BORDER.search(rows[i]):
            top = i + 1
            break
        if rows[i].startswith("⏺"):
            top = i + 1
            break
    block = rows[top:foot + 1]
    while block and not block[0].strip():
        block.pop(0)
    text = "\n".join(block)
    labels = [opts[n] for n in sorted(opts)]
    tail = " ".join(rows[max(0, foot - 1):foot + 1])
    if "Enter to select" in tail or "to navigate" in tail:
        kind = "ask"
    elif any(x.startswith("Yes") for x in labels) and any(x.startswith("No") for x in labels):
        kind = "permission"
    else:
        kind = "menu"
    # One line for the row and the refusal: the dialog's title, then the
    # warning above the question («Dangerous rm operation…»), or the command
    # between the two ╌ rules when there is no warning.
    def para(i):
        parts = []
        while i >= top:
            s = rows[i].strip().lstrip("│").strip()
            if not s or s.startswith("╌"):
                break
            parts.insert(0, s)
            i -= 1
        return " ".join(parts), i

    ask = None
    for i in range(first - 1, top - 1, -1):
        if rows[i].strip():
            ask = i
            break
    summary = ""
    if ask is not None:
        i = ask - 1
        while i >= top and not rows[i].strip():
            i -= 1
        summary, i = para(i)
        if not summary and i >= top and rows[i].strip().startswith("╌"):
            summary, _ = para(i - 1)
        summary = summary or rows[ask].strip()
    title = block[0].strip() if block else ""
    if (title and title[0] not in "│╌⏺" and title != summary
            and ask is not None and block[0] != rows[ask]):
        summary = title + " — " + summary
    return {"kind": kind, "text": text, "summary": summary[:300],
            "options": [{"n": n, "label": opts[n]} for n in sorted(opts)],
            "sig": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]}


def open_prompt(uuid, rows=None, log=None):
    """The dialog a live session is waiting on, unless it is an AskUserQuestion
    the transcript knows about (the chat shows that one with its options)."""
    if rows is None:
        rows = ps_rows()
    log = log or log_for(uuid, rows)
    pr = screen_prompt(screen_lines(log, rows)) if log else None
    if pr and pr["kind"] == "ask" and ask_open(uuid):
        return None
    return pr


def _local_ts(v):
    """Привести отметку записи к локальному времени машины.

    Claude Code пишет их в UTC с суффиксом Z, а панель читают из МСК — без
    перевода лента отставала ровно на смещение пояса (08-24: 09:53 вместо 12:53).
    """
    if not isinstance(v, str) or len(v) < 19:
        return ""
    try:
        dt = datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return v[:19]
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _epoch_ts(v):
    """Отметка записи в секундах эпохи — чтобы лента могла показывать «5 минут
    назад», а не абсолютное время: страница живёт часами, и абсолютное к вечеру
    ничего не говорит."""
    if not isinstance(v, str) or len(v) < 19:
        return 0
    try:
        dt = datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _clean_text(v, flat=True):
    """Отсеять служебное — обёртки команд и caveat.

    flat=False сохраняет переводы строк: разметка построчная, и схлопывание
    превращает ответ в одну простыню без абзацев (08-24 — ровно на это и
    жаловались, пока разбор блоков звал плоский вариант).
    """
    if not isinstance(v, str):
        return ""
    v = _PASTE_TAG.sub("", v)
    if flat:
        v = " ".join(v.split())
    else:
        v = "\n".join(ln.rstrip() for ln in v.splitlines()).strip()
    if not v or v.startswith("<") or v.startswith("Caveat"):
        return ""
    return v


def _cap(txt):
    return txt if len(txt) <= TX_CHARS else txt[:TX_CHARS] + " …[cut]"


# Порядок важен: у Bash есть description — короткая человеческая строка, ровно
# та, что показана шагом в приложении. У остальных берём первое осмысленное поле.
_LABEL_KEYS = ("description", "file_path", "path", "pattern", "query",
               "url", "command", "prompt")


def _tool_label(inp):
    if not isinstance(inp, dict):
        return ""
    for k in _LABEL_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:160]
    return ""


ASK_TOOL = "AskUserQuestion"

# «вопрос»="ответ", как их печатает Claude Code. Экранированную кавычку внутри
# пропускаем, иначе пара обрывается на середине ответа.
_ANSWER_RE = re.compile(r'"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"')


def _ask_payload(inp):
    """Вопросы Claude в компактном виде: текст, заголовок, варианты."""
    out = []
    if not isinstance(inp, dict):
        return out
    for q in (inp.get("questions") or []):
        if not isinstance(q, dict):
            continue
        opts = []
        for o in (q.get("options") or []):
            if isinstance(o, dict) and o.get("label"):
                opts.append({
                    "label": " ".join(str(o.get("label")).split())[:120],
                    "desc": " ".join(str(o.get("description") or "").split())[:300],
                })
        text = " ".join(str(q.get("question") or "").split())[:400]
        if not text and not opts:
            continue
        out.append({"q": text,
                    "header": " ".join(str(q.get("header") or "").split())[:40],
                    "multi": bool(q.get("multiSelect")),
                    "options": opts})
    return out


def _ask_answers(text):
    """Ответ человека на AskUserQuestion → пары «вопрос/ответ».

    Ответом бывает не метка варианта, а свободный текст (вариант «Other»),
    поэтому берём его как есть. Если формат не узнали — отдаём сырьё без
    служебного хвоста, адресованного модели, чтобы ответ не пропал совсем.
    """
    if isinstance(text, list):
        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
    if not isinstance(text, str):
        return [], ""
    pairs = [{"q": a.replace('\\"', '"'), "a": b.replace('\\"', '"')}
             for a, b in _ANSWER_RE.findall(text)]
    if pairs:
        return pairs, ""
    raw = text.split("Read the answers carefully")[0]
    raw = raw.replace("The user answered:", "").strip()
    return [], " ".join(raw.split())[:600]


# Путь в тексте: файл часто создают через Bash, и структурированного
# file_path в записи тогда нет вовсе — он лежит внутри строки команды или в
# ответе Claude. Ловим по виду и ПРОВЕРЯЕМ СУЩЕСТВОВАНИЕ: без этой проверки в
# чипы лезли бы «package.json» из общих рассуждений и куски версий.
# ⛔ Перед путём в тексте стоит что угодно: «document=@/Users/…», кавычка,
# скобка. Исключаем только словесный символ — иначе абсолютный путь после «@»
# обрезался и превращался в мусор вроде «os/docs/…».
PATH_RE = re.compile(r"(?<![\w])((?:~|\.{1,2})?/?[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]{1,6})")
# ⛔ Путь с пробелом в имени PATH_RE не берёт: класс [\w.\-] пробела не знает,
# а скриншот macOS всегда называется «Screenshot 2026-08-26 at 1.24.56 PM.png».
# Из-за этого вложение-скриншот не попадало в paths, клипса не рисовалась, и
# в переписке от фотографии оставался голый путь текстом. Панель сама берёт
# путь вложения в кавычки — по ним и находим то, что регулярка пропускает.
QUOTED_RE = re.compile(r"\"([^\"\n]{6,400})\"")
PATH_PER_MSG = 6


TITLE_MAX = 34
# Первый промпт почти всегда начинается одинаково — «я хочу…», «надо…»,
# «давай…». В названии таба это шум: одинаковый у всех сессий и съедает те
# самые три-четыре слова, ради которых название и заводится.
TITLE_DROP = ("claude", "claude", "привет", "слушай", "давайте", "давай",
              "я хочу", "хочу", "мне нужно", "нужно", "надо", "хотел бы",
              "продолжаем", "продолжим", "работаем в проекте", "работаем",
              "please", "i want to", "i want", "let's", "lets")


# Обрыв на предлоге («…источники для семей, не») читается как ошибка, а не как
# сокращение: слово есть, а смысла за ним нет. Хвостовые служебные слова
# снимаем.
TITLE_TAIL = {"и", "а", "но", "не", "же", "ли", "в", "во", "на", "с", "со",
              "для", "по", "из", "от", "к", "ко", "за", "до", "при", "о",
              "об", "у", "что", "как", "это", "the", "a", "of", "to", "for",
              "and", "or", "in", "on", "with"}


def _clip_words(text, limit=TITLE_MAX):
    """Первые слова, но не разрывая слово посередине."""
    out = []
    for w in (text or "").split():
        if out and len(" ".join(out + [w])) > limit:
            break
        out.append(w)
        if len(" ".join(out)) >= limit:
            break
    while len(out) > 1 and out[-1].strip(" ,.;:!?-—«»\"'·").lower() in TITLE_TAIL:
        out.pop()
    return " ".join(out).strip(" ,.;:!?-—«»\"'·")


def _strip_opening(text):
    """Снимает служебный зачин, сколько бы их ни было подряд."""
    cur = (text or "").strip()
    for _ in range(4):
        low = cur.lower()
        for d in TITLE_DROP:
            if low.startswith(d) and (len(low) == len(d)
                                      or not low[len(d)].isalnum()):
                cur = cur[len(d):].lstrip(" ,.:;—-")
                break
        else:
            return cur
    return cur


def auto_title(rec_name, summary, project, short):
    """Название таба, когда своего человек не задал.

    Считается на каждый снимок и стоит ноль: никаких платных вызовов, только
    то, что уже посчитано. Пересказ появляется не сразу, поэтому у только что
    рождённой сессии название берётся из первого промпта — «с самого начала»
    здесь важнее, чем «идеально».
    """
    sm = " ".join((summary or "").split())
    if sm:
        # Пересказ обычно уже устроен как «Тема: подробности» — левая часть и
        # есть готовое короткое имя.
        head = sm.split(":")[0].strip()
        if 3 <= len(head) <= TITLE_MAX:
            return head
        got = _clip_words(sm)
        if len(got) >= 3:
            return got
    first = _clip_words(_strip_opening(rec_name or ""))
    if len(first) >= 3:
        return first
    if project and project != NO_PROJECT:
        return project
    return short


def _find_paths(text, cwd, cache):
    if not text:
        return []
    out = []

    def take(raw):
        """Кладёт путь в out, если такой файл есть. True — набрали лимит."""
        if len(raw) < 6:
            return False
        cand = os.path.expanduser(raw)
        # ⛔ Одного cwd мало: у подагентов он указывает в служебную папку
        # (…/projects/…/memory), а путь в тексте дан от корня репозитория или
        # от домашней. Перебираем базы и берём ту, где файл реально есть.
        bases = [cand] if cand.startswith("/") else [
            os.path.normpath(os.path.join(b, cand))
            for b in (cwd, os.path.expanduser("~")) if b]
        for full in bases:
            if full in out:
                break
            ok = cache.get(full)
            if ok is None:
                try:
                    ok = os.path.isfile(full)
                except OSError:
                    ok = False
                cache[full] = ok
            if ok:
                out.append(full)
                break
        return len(out) >= PATH_PER_MSG

    # Кавычки идут первыми: они точнее регулярки и переживают пробелы в имени.
    # Проверка isfile() внутри take() отсеивает обычные цитаты — путём
    # считается только то, что на диске действительно лежит.
    for m in QUOTED_RE.finditer(text):
        raw = m.group(1).strip()
        if "/" not in raw:
            continue
        if take(raw):
            return out
    for m in PATH_RE.finditer(text):
        if take(m.group(1)):
            break
    return out


def _prov_model(rec):
    """What a Codex or Mistral Vibe session runs now, read from its thread."""
    path = rec.get("rollout") or ""
    if not path:
        return ""
    try:
        if rec.get("provider") == "codex":
            import rc_codex
            m, e = rc_codex.rollout_model(path)
            return m + ((" · " + e) if m and e else "")
        if rec.get("provider") == "vibe":
            import rc_vibe
            a = rc_vibe.session_model(path)
            return (rc_vibe.MODELS.get(a) or {}).get("display", a)
    except Exception:
        return ""
    return ""


_CODEX_MAP = {"mt": None, "map": {}}


def _codex_paths():
    """{uuid: thread path} of Codex and Mistral Vibe sessions, cached per
    registry version: _tx_path runs on every poll and must stay a dict lookup."""
    try:
        mt = os.stat(G.REG_PATH).st_mtime_ns
    except OSError:
        return {}
    if _CODEX_MAP["mt"] != mt:
        m = {}
        for u, r in (G.load(G.REG_PATH, {}).get("sessions") or {}).items():
            if isinstance(r, dict) and r.get("provider") in ("codex", "vibe") and r.get("rollout"):
                m[u] = r["rollout"]
        _CODEX_MAP.update(mt=mt, map=m)
    return _CODEX_MAP["map"]


def _tx_path(uuid):
    return _codex_paths().get(uuid) or os.path.join(G.PROJ, uuid + ".jsonl")


def _tail_records(path, size):
    """Parse only the end of a transcript. They run to 5 MB and the panel shows
    ~90 of them — reading every one in full on a page render is not an option."""
    with open(path, "rb") as fh:
        if size > DIGEST_TAIL:
            fh.seek(size - DIGEST_TAIL)
            blob = fh.read()
            cut = blob.find(b"\n")       # the first line is a fragment, drop it
            blob = blob[cut + 1:] if cut >= 0 else b""
        else:
            blob = fh.read()
    out = []
    for line in blob.split(b"\n"):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except ValueError:
            continue
    return out


def digest_for(path):
    """Last thing asked, last thing answered — «на чём закончили» without
    spending a token on a real summary."""
    st = os.stat(path)
    last_u = last_a = ""
    ts = ""
    ctx = 0
    ctx_model = ""
    ctx_effort = ""
    for rec in reversed(_tail_records(path, st.st_size)):
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        if kind == "user" and (_is_meta(rec) or rec.get("isCompactSummary")):
            continue
        # Сколько контекста занято — это размер последнего запроса плюс ответ:
        # вход + оба вида кеша + выход. Кеш обязателен: при живом префиксе
        # input_tokens почти нулевой, и без cache_read счётчик показывал бы ноль
        # на сессии с сотней тысяч токенов.
        if kind == "assistant" and not ctx:
            msg = rec.get("message") or {}
            u = msg.get("usage") or {}
            if u:
                ctx = (u.get("input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0)
                       + u.get("cache_read_input_tokens", 0)
                       + u.get("output_tokens", 0))
                ctx_model = msg.get("model") or ""
                # effort лежит в самой записи, рядом с timestamp, а не внутри
                # message — это единственное место, где видно, с чем сессия
                # реально работает сейчас, а не что ей выставили в реестре.
                ctx_effort = rec.get("effort") or ""
        txt = _msg_text(rec)
        if not txt:
            continue
        if kind == "user" and not last_u and not txt.lstrip().startswith(INTERRUPT_MARK):
            last_u = txt[:400]
        elif kind == "assistant" and not last_a:
            last_a = txt[:400]
        if not ts:
            ts = _local_ts(rec.get("timestamp"))
        if last_u and last_a and ctx:
            break
    # Незакрытый вопрос — единственное, что останавливает сессию насовсем:
    # она не «работает» и не «простаивает», она ждёт человека. Без отметки в
    # строке это видно, только открыв шторку, а их полтора десятка.
    pend = {}
    for rec in _tail_records(path, st.st_size):
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == ASK_TOOL:
                qs = _ask_payload(b.get("input"))
                if qs:
                    pend[b.get("id")] = qs[0]["q"] or qs[0]["header"]
            elif b.get("type") == "tool_result":
                pend.pop(b.get("tool_use_id"), None)
    waiting = list(pend.values())[-1] if pend else ""

    return {"last_user": last_u, "last_assistant": last_a, "ts": ts,
            "waiting": waiting, "v": DIGEST_V,
            "ctx": ctx, "ctx_max": ctx_limit(ctx_model), "model_id": ctx_model,
            "effort_now": ctx_effort,
            "size": st.st_size, "mtime": int(st.st_mtime)}


def digests():
    """Cache-only read: a page render must never wait on disk sweeps."""
    try:
        with open(DIGEST_CACHE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def digest_sweep(limit=DIGEST_BATCH):
    """Refresh stale digests, bounded per sweep. Keyed on (size, mtime), so a
    settled session is re-read exactly once and live ones stay current."""
    cache = digests()
    reg = G.load(G.REG_PATH, {})
    done = 0
    for uuid, rec in reg.get("sessions", {}).items():
        if rec.get("kind") != "rc" or not G.has_content(rec):
            continue
        if (rec.get("last_active") or "")[:16] < WINDOW_FROM:
            continue
        try:
            st = os.stat(_tx_path(uuid))
        except OSError:
            continue
        old = cache.get(uuid)
        # «waiting» in old — печать версии дайджеста. Ключ (size, mtime) сам по
        # себе означает «пересчитывать нечего», поэтому запись, посчитанная до
        # появления нового поля, осталась бы без него навсегда: у отстоявшейся
        # сессии файл больше не меняется. Отсутствие ключа гонит её на пересчёт
        # ровно один раз.
        if (old and old.get("v") == DIGEST_V
                and old.get("size") == st.st_size
                and old.get("mtime") == int(st.st_mtime)):
            continue
        try:
            cache[uuid] = digest_for(_tx_path(uuid))
        except OSError:
            continue
        done += 1
        if done >= limit:
            break
    if done:
        tmp = DIGEST_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, DIGEST_CACHE)
    return done


def digest_refresher(every=DIGEST_EVERY):
    def loop():
        while True:
            try:
                digest_sweep()
            except Exception:
                pass
            time.sleep(every)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------- саммари
#
# Дайджест «просил / ответил» — это цитаты, а не пересказ: по ним видно
# последнюю реплику, но не «чем сессия занята». Заголовок от самого Claude Code
# (`ai-title`) не годится: проверено 24-08 на 40 свежих транскриптах — он ни
# разу не поменялся и есть лишь у 6 из 40, то есть это имя при рождении, а не
# состояние работы.
#
# ⛔ cwd здесь СВОЙ, и это не косметика. `claude -p` кладёт транскрипт в папку
# по cwd; из `~` он падал бы прямо в PROJ, к настоящим сессиям, — ровно та беда,
# что 23-08 забила реестр 66 пустышками от `/usage`. Отдельный каталог уводит их
# в свой проект, где census их не видит вовсе.

SUM_CACHE = CFG.state("rc-summary.json")
SUM_DIR = CFG.state("rc-sum")
SUM_V = 3
SUM_EVERY = 150           # пауза между проходами
SUM_BATCH = 2             # сессий за проход — это платные вызовы, не чтение
# ⛔ Было 12000, и короткий обмен репликами порога не брал: человек написал,
# сессия ответила абзацем — описание в табе оставалось от прошлой задачи.
# Расход это не разгоняет: за проход пересказывается не больше SUM_BATCH
# сессий, так что потолок вызовов задаёт батч, а не порог.
SUM_GROWTH = 3000         # меньший прирост пересказывать нечего
SUM_FRESH_H = 30          # сессии старше просто не трогаем
SUM_MODEL = "haiku"
# Обычный проход пропускает сессию, пока транскрипт не подрос на SUM_GROWTH, —
# и после короткого обмена репликами описание в табе оставалось вчерашним.
# Отправка из панели помечает сессию, и помеченную пересказываем независимо от
# прироста. Ждём затишья: пересказ, снятый в момент, когда ответ ещё пишется,
# получился бы про вопрос, а не про сделанное.
SUM_DIRTY_EVERY = 20      # как часто заглядывать в помеченные
SUM_SETTLE_S = 12         # столько тишины считаем «ответ дописан»
SUM_DIRTY_TRIES = 3       # предохранитель: не долбить платной моделью впустую
_sum_dirty = {}
_sum_dirty_lock = threading.Lock()


# ⛔ Отправка и запись в транскрипте — РАЗНЫЕ моменты. Занятая сессия держит
# ввод в очереди TUI, пока не доработает текущее, и записи всё это время нет —
# а порядок в колонке считается по последней записи. Из-за этого строка
# поднималась наверх только когда сессия домалывала ответ, то есть спустя
# минуты после того, как человек ей написал. Для порядка «последнее, что я
# трогал» правда — момент ОТПРАВКИ, и знает его только панель.
_SENT_AT = {}
_SENT_LOCK = threading.Lock()
SENT_KEEP_S = 36 * 3600   # дольше суток отметка на порядок уже не влияет


def note_sent(uuid, when=None):
    """Запомнить, что в сессию только что писали."""
    now = when or time.time()
    with _SENT_LOCK:
        _SENT_AT[uuid] = now
        if len(_SENT_AT) > 400:
            cut = now - SENT_KEEP_S
            for k in [k for k, v in _SENT_AT.items() if v < cut]:
                _SENT_AT.pop(k, None)


def sent_at(uuid):
    with _SENT_LOCK:
        return _SENT_AT.get(uuid, 0)


def mark_summary_stale(pref):
    """Пометить сессию: её описание устарело, пересказать вне очереди."""
    try:
        uuid = resolve(pref)
    except Exception:
        return
    with _sum_dirty_lock:
        _sum_dirty.setdefault(uuid, 0)


def summary_dirty_sweep():
    with _sum_dirty_lock:
        pending = list(_sum_dirty.items())
    if not pending:
        return 0
    cache = summaries()
    done, failed = [], []
    now = time.time()
    for uuid, tries in pending:
        ev = _last_event_ts(uuid)
        if ev and now - ev < SUM_SETTLE_S:
            continue          # ещё пишет — подождём следующего захода
        try:
            got = summary_for(_tx_path(uuid))
        except Exception:
            got = None
        if got:
            cache[uuid] = got
            done.append(uuid)
        else:
            failed.append(uuid)
    if done:
        tmp = SUM_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, SUM_CACHE)
    with _sum_dirty_lock:
        for uuid in done:
            _sum_dirty.pop(uuid, None)
        for uuid in failed:
            if uuid not in _sum_dirty:
                continue
            _sum_dirty[uuid] += 1
            if _sum_dirty[uuid] >= SUM_DIRTY_TRIES:
                _sum_dirty.pop(uuid, None)
    return len(done)
# ⛔ Служебные вызовы НЕ должны тянуть пользовательские настройки. В
# ~/.claude/settings.json живут хуки UserPromptSubmit («ENGLISH-COACH» и
# «RUSSIAN-TO-ENGLISH»), которые на каждый промпт требуют сперва выдать
# английский перевод блоком EN:. Это учебный инструмент хозяина машины, но в
# пересказ и правку терминов он прилетал переводом МОЕГО промпта вместо
# результата — и защита от сочинительства его отбраковывала.
NO_USER_SETTINGS = ["--setting-sources", "project"]

SUM_PROMPT = (
    "You write a one-line status for a list of sessions. Between the "
    "<conversation> tags is the tail of a conversation between a person and "
    "Claude Code. Reply with exactly one line, at most 90 characters: what "
    "this session is working on and where it stopped. Write in English, "
    "unless the person writes in another language: then use that language. "
    "Describe only the conversation: use no tools, do not mention this "
    "request or any folder. No quotes, no preamble, no prefix.\n\n")


def summaries():
    """Только чтение кеша: рендер страницы не должен ждать модель."""
    try:
        with open(SUM_CACHE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _sum_source(path, size):
    out = []
    for rec in _tail_records(path, size):
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        if kind == "user" and _is_meta(rec):
            continue
        txt = _msg_text(rec)
        if txt:
            out.append(("Person: " if kind == "user" else "Assistant: ") + txt[:600])
    return "\n".join(out[-30:])[:10000]


def helper_run(prompt, model, timeout):
    """One `claude -p` for a background helper (titles, voice fixes, routing).
    Without a Claude subscription here (CFG.DEFAULT_PROVIDER) it runs on that
    provider's fast model instead."""
    env = None
    if CFG.DEFAULT_PROVIDER:
        import rc_providers
        p = rc_providers.PROVIDERS.get(CFG.DEFAULT_PROVIDER)
        if not p:
            # Codex / Mistral Vibe only: no `claude -p` here; the callers fall
            # back to the first prompt as the title
            raise RuntimeError("no helper model without Claude")
        env = dict(os.environ)
        with open(rc_providers.env_file(CFG.DEFAULT_PROVIDER)) as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k:
                    env[k] = v.strip("'")
        model = p.get("fast") or model
    return subprocess.run([CLAUDE_BIN, "-p", prompt, "--model", model] + NO_USER_SETTINGS,
                          capture_output=True, text=True, errors="replace",
                          timeout=timeout, cwd=SUM_DIR, env=env)


def summary_for(path):
    st = os.stat(path)
    src = _sum_source(path, st.st_size)
    if not src.strip():
        return None
    try:
        os.makedirs(SUM_DIR)
    except OSError:
        pass
    try:
        r = helper_run(SUM_PROMPT + "<conversation>\n" + src + "\n</conversation>",
                       SUM_MODEL, 180)
    except Exception:
        return None
    # ⛔ Не схлопывать весь stdout в одну строку. На мини стоит глобальный
    # стиль, который иногда сперва печатает перевод запроса строкой «EN: …»;
    # схлопывание склеивало его с ответом, и в превью уезжал кусок промпта
    # (поймано 24-08 на d112ec9c). Пересказ — последняя содержательная строка.
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("EN:")]
    if not lines:
        return None
    line = " ".join(lines[-1].split())
    if not line or len(line) > 400:
        return None
    return {"text": line[:160], "v": SUM_V,
            "size": st.st_size, "mtime": int(st.st_mtime), "ts": time.time()}


def summary_sweep(limit=SUM_BATCH):
    cache = summaries()
    reg = G.load(G.REG_PATH, {})
    fresh = time.time() - SUM_FRESH_H * 3600
    done = 0
    # Сначала самые свежие: пересказ нужен тому, над чем работают сейчас.
    items = sorted(reg.get("sessions", {}).items(),
                   key=lambda kv: kv[1].get("last_active_ts", 0), reverse=True)
    for uuid, rec in items:
        if done >= limit:
            break
        if rec.get("kind") != "rc" or not G.has_content(rec):
            continue
        if rec.get("last_active_ts", 0) < fresh:
            continue
        try:
            st = os.stat(_tx_path(uuid))
        except OSError:
            continue
        old = cache.get(uuid)
        if (old and old.get("v") == SUM_V
                and st.st_size - old.get("size", 0) < SUM_GROWTH):
            continue
        got = summary_for(_tx_path(uuid))
        if not got:
            continue
        cache[uuid] = got
        done += 1
    if done:
        tmp = SUM_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, SUM_CACHE)
    return done


def _memory_tight():
    """Мало памяти — фоновым CLI-вызовам сейчас не место.

    И пересказ, и `/usage` поднимают настоящий процесс claude на сотню
    мегабайт. На забитой машине это соревнование за память с живыми сессиями,
    и проигрывает в нём не фоновая задача, а чей-то рабочий чат.
    """
    try:
        return G.free_mb() < USAGE_FREE_MB
    except Exception:
        return False


def summary_refresher(every=SUM_EVERY):
    def loop():
        last_full = 0.0
        while True:
            try:
                # ⛔ На забитой машине пересказ ждёт: он поднимает процесс
                # claude на сотню мегабайт и соревнуется за память с живыми
                # сессиями. Описание в табе подождёт, чужой рабочий чат — нет.
                if _memory_tight():
                    time.sleep(every)
                    continue
                # Помеченные — часто и дёшево (обычно их нет вовсе), полный
                # обход — по-прежнему раз в `every`: это платные вызовы.
                summary_dirty_sweep()
                if time.time() - last_full >= every:
                    summary_sweep()
                    last_full = time.time()
            except Exception:
                pass
            time.sleep(SUM_DIRTY_EVERY)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


TX_OPEN_LIMIT = 200         # first paint of a chat; "show earlier" asks for TX_LIMIT
TX_CACHE_MAX = 16
_TX_CACHE = {}
_TX_CACHE_MU = threading.Lock()


def transcript(pref, limit=TX_LIMIT, sig=None):
    """The whole conversation of one session, read from the local transcript.

    Not from the card: after an account switch the card is empty by design (see
    G.has_content and the history-suppression note), and this file is the only
    place the conversation still exists in readable form.

    `sig` is what the caller already has: when the feed is unchanged the answer
    carries only the live fields (age, tui, turn) and `same: true`. ⛔ The chat
    polls every 1.5 s, and a long session answered up to 480 KB each time —
    over a 70-250 ms link from abroad to the main Mac that was most of the
    "chat takes ten seconds" (09-24). The parse itself is cached per file
    size+mtime, so an idle chat costs a stat, not a 40 MB re-read.
    """
    uuid = resolve(pref)
    path = _tx_path(uuid)
    parsed = _tx_parsed(uuid, path)
    msgs = parsed["msgs"]
    try:
        limit = max(20, min(TX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = TX_LIMIT
    shown = msgs[-limit:]
    mine = parsed["sigs"].get(limit)
    if mine is None:
        blob = json.dumps([shown, parsed["arts"], parsed["clipped"]],
                          ensure_ascii=False, sort_keys=True)
        mine = hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]
        parsed["sigs"][limit] = mine
    rec = G.load(G.REG_PATH, {}).get("sessions", {}).get(uuid) or {}
    said = parsed["said"]
    _log = log_for(uuid)
    out = {"short": uuid[:8], "name": rec.get("name") or uuid[:8],
           "total": said, "steps": len(msgs) - said,
           "clipped": parsed["clipped"], "sig": mine, "limit": limit,
           "more": max(0, len(msgs) - len(shown)),
           # То же определение, что и в списке слева: два разных ответа на
           # вопрос «работает ли она» уже разводили ленту с карточкой.
           "age": _tx_age(uuid) if _last_event_ts(uuid) is None
                  else int(time.time() - _last_event_ts(uuid)),
           # Лента рисуется по этому ответу, и «пишет ответ…» внизу раньше
           # решалось по возрасту записи — то есть гасло ровно на долгом
           # шаге. Статус с экрана TUI едет сюда же, чтобы низ ленты знал
           # правду, не дожидаясь второго запроса.
           "tui": _live_tui(uuid, _log, tui_status(_log), pid_for(uuid),
                            any(m.get("role") == "ask" and m.get("pending")
                                for m in msgs[-60:]),
                            _screen_is_live(_log)),
           # the dialog on its screen, with its options, for the chat to show
           # (10-25); always present, null included: a «same» answer is merged
           # over the cached one, and a missing key would keep a stale prompt
           "perm": open_prompt(uuid, log=_log) if _log else None,
           "turn": parsed["turn"]}
    if sig and sig == mine:
        out["same"] = True
        return out
    out["artifacts"] = parsed["arts"]
    out["shown"] = shown
    return out


def _tx_parsed(uuid, path):
    """_tx_parse() once per version of the file."""
    if os.path.isdir(path):
        # a Mistral Vibe session folder (rc_vibe)
        key = G.vibe_version(path)
        with _TX_CACHE_MU:
            hit = _TX_CACHE.get(uuid)
        if hit and hit[0] == key:
            return hit[1]
        import rc_vibe
        msgs = rc_vibe.session_rows(path)[-TX_LIMIT * 4:]
        parsed = {"msgs": msgs, "clipped": False, "arts": [], "turn": None, "sigs": {},
                  "said": sum(1 for m in msgs if m["role"] in ("user", "assistant"))}
        with _TX_CACHE_MU:
            _TX_CACHE.pop(uuid, None)
            _TX_CACHE[uuid] = (key, parsed)
        return parsed
    st = os.stat(path)
    key = (st.st_size, st.st_mtime_ns)
    with _TX_CACHE_MU:
        hit = _TX_CACHE.get(uuid)
    if hit and hit[0] == key:
        return hit[1]
    if "/.codex/sessions/" in path:
        import rc_codex
        msgs = rc_codex.rollout_rows(path)[-TX_LIMIT * 4:]
        parsed = {"msgs": msgs, "clipped": False, "arts": [], "turn": None, "sigs": {},
                  "said": sum(1 for m in msgs if m["role"] in ("user", "assistant"))}
    else:
        parsed = _tx_parse(path, st.st_size)
    with _TX_CACHE_MU:
        _TX_CACHE.pop(uuid, None)
        _TX_CACHE[uuid] = (key, parsed)
        while len(_TX_CACHE) > TX_CACHE_MAX:
            _TX_CACHE.pop(next(iter(_TX_CACHE)))
    return parsed


STREAK_GAP_S = 120


def _tx_parse(path, size):
    """Read a transcript into feed items. Returns a dict the cache keeps; the
    caller must not mutate it."""
    clipped = size > TX_MAX_BYTES
    msgs = []
    # Расход по ходам: (позиция в msgs, usage). Сумму за текущий ход считаем
    # после разбора — от последней реплики человека. Стрелка в статусе раньше
    # показывала счётчик ОДНОГО запроса с экрана TUI, а спрашивают, сколько
    # ушло на всю задачу.
    usages = []
    arts = []           # артефакты сессии — списком, помимо ленты
    pcache = {}         # существование пути спрашиваем у диска один раз
    asked = {}          # id вопроса → индекс его строки в msgs
    # ⛔ 10-16: «минуты работы снова неверные — показывает время шага». The
    # clock ran from the LAST user line, and a message sent while the session
    # worked (queued) is one: the owner wrote mid-turn and «working 16 min» became
    # «working 2 min». A turn starts at the first prompt after the previous
    # turn ended (turn_duration or a stop), or at a background-task wake-up.
    # A turn that starts within STREAK_GAP_S of the previous one's end is the
    # same stretch of work: a message sent mid-turn ends the turn and starts
    # the next one at once, and the clock must not drop to zero for it.
    turn = {"pos": -1, "tse": None, "fresh": True, "end": None}

    def _turn_open(pos, tse):
        if not turn["fresh"]:
            return
        turn["fresh"] = False
        if turn["end"] and tse and turn["pos"] >= 0 and tse - turn["end"] < STREAK_GAP_S:
            return
        turn["pos"], turn["tse"] = pos, tse
    with open(path, "rb") as fh:
        if clipped:
            fh.seek(size - TX_MAX_BYTES)
            fh.readline()
        for raw in fh:
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = rec.get("type")
            # ⛔ Сообщение, отправленное ЗАНЯТОЙ сессии, TUI кладёт в свою
            # очередь и пишет сюда типом queue-operation. Записи "user" при
            # этом не появляется ВООБЩЕ — ни при постановке, ни когда очередь
            # разберут. Без разбора этого типа реплика человека пропадала с
            # экрана насовсем, а панель бесконечно висела на «sending…»,
            # дожидаясь записи, которой никогда не будет.
            # Операций у очереди как минимум три, и ведут они себя ПО-РАЗНОМУ:
            # "remove" — сообщение ушло в работу, записи "user" не будет вовсе
            # (aaa738dd); "dequeue" — следом пишется настоящая запись "user"
            # (512ee5c8), и тогда enqueue — дубль. Полагаться на имя операции
            # нельзя, их может быть больше. Поэтому собираем все enqueue, а
            # ниже, после разбора файла, выбрасываем те, для которых запись
            # "user" всё-таки нашлась.
            if kind == "system" and rec.get("subtype") == "turn_duration":
                turn["fresh"] = True
                turn["end"] = _epoch_ts(rec.get("timestamp"))
                continue
            if kind == "queue-operation":
                if rec.get("operation") == "remove":
                    # taken into the running turn: no "user" record will ever
                    # follow, so this enqueue is the only trace of the message
                    rtxt = " ".join((_clean_text(rec.get("content"), flat=False) or "").split())
                    for m in reversed(msgs):
                        if (m.get("queued") and not m.get("removed")
                                and " ".join((m.get("text") or "").split()) == rtxt):
                            m["removed"] = True
                            break
                if rec.get("operation") != "enqueue":
                    continue          # remove — отметка «забрали в работу»
                qtxt = _clean_text(rec.get("content"), flat=False)
                if qtxt:
                    qcwd = rec.get("cwd") or ""
                    _turn_open(len(msgs), _epoch_ts(rec.get("timestamp")))
                    msgs.append({"role": "user",
                                 "ts": _local_ts(rec.get("timestamp")),
                                 "tse": _epoch_ts(rec.get("timestamp")),
                                 "paths": _find_paths(qtxt, qcwd, pcache),
                                 "text": _cap(qtxt), "queued": True})
                continue
            if kind not in ("user", "assistant"):
                continue
            if kind == "user" and _is_meta(rec):
                continue
            if kind == "assistant":
                _u = (rec.get("message") or {}).get("usage") or {}
                if _u:
                    usages.append((len(msgs), _u))
                # 10-18: a finished reply ends the turn as well: turns end
                # without turn_duration now and then (23 of 1408 on 09-25),
                # and the next prompt then ran on the old clock.
                if _reply_done(rec):
                    turn["fresh"] = True
                    turn["end"] = _epoch_ts(rec.get("timestamp"))
            ts = _local_ts(rec.get("timestamp"))
            tse = _epoch_ts(rec.get("timestamp"))
            if kind == "user" and _is_interrupt(rec):
                msgs.append({"role": "stop", "ts": ts, "tse": tse, "text": "stopped"})
                turn["fresh"] = True
                turn["end"] = None          # a stop ends the stretch for real
                continue
            if kind == "user" and turn["fresh"]:
                _raw = (rec.get("message") or {}).get("content")
                if isinstance(_raw, str) and _raw.lstrip().startswith("<task-notification>"):
                    # a background task woke the agent: that turn is its own
                    _turn_open(len(msgs), tse)
            cwd = rec.get("cwd") or ""
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                # ⛔ Раньше здесь брались только text-блоки, и вся работа —
                # 285 вызовов инструментов в одной сессии — просто исчезала:
                # в приложении шаги видны, в шторке не были. Разбираем блоки
                # по отдельности, порядок сохраняется.
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = _clean_text(part.get("text"), flat=False)
                        if txt:
                            if kind == "user" and not rec.get("isCompactSummary"):
                                _turn_open(len(msgs), tse)
                            msgs.append({"role": kind, "ts": ts, "tse": tse,
                                         "paths": _find_paths(txt, cwd, pcache),
                                         "text": _cap(txt)})
                    elif ptype == "tool_use":
                        # Вопрос Claude — не «шаг»: у него нет ни одного из
                        # _LABEL_KEYS, так что раньше он падал в свёрнутую
                        # группу пустой строкой и не доходил до человека вовсе.
                        if part.get("name") == ASK_TOOL:
                            qs = _ask_payload(part.get("input"))
                            if qs:
                                asked[part.get("id")] = len(msgs)
                                msgs.append({"role": "ask", "ts": ts, "tse": tse,
                                             "questions": qs, "pending": True})
                                continue
                        inp = part.get("input")
                        fp = ""
                        if isinstance(inp, dict):
                            for k in ("file_path", "path", "notebook_path"):
                                v = inp.get(k)
                                if not isinstance(v, str) or not v:
                                    continue
                                # ⛔ Путь бывает ОТНОСИТЕЛЬНЫМ — Claude Code
                                # пишет его как есть, от cwd записи. Без этого
                                # ровно такие файлы не открывались вовсе.
                                fp = v if v.startswith("/") else (
                                    os.path.join(cwd, v) if cwd else "")
                                break
                        tool = part.get("name") or "?"
                        label = _tool_label(inp)
                        step = {"role": "step", "ts": ts, "tse": tse,
                                "tool": tool, "path": fp, "text": label}
                        if not fp:
                            found = _find_paths(label, cwd, pcache)
                            if found:
                                step["path"] = found[0]
                        msgs.append(step)
                        if tool == "Artifact" and fp:
                            art = {"path": fp, "tse": tse, "ts": ts,
                                   "name": os.path.basename(fp),
                                   "title": " ".join(str(
                                       (inp or {}).get("title")
                                       or (inp or {}).get("description")
                                       or "").split())[:120]}
                            arts.append(art)
                            # Отдельной строкой в ленте, а не только шагом:
                            # артефакт — результат работы, его ищут глазами,
                            # а шаги свёрнуты в группу.
                            msgs.append(dict(art, role="artifact"))
                    elif ptype == "tool_result":
                        # Ответы на всё остальное — это вывод инструментов, он
                        # в ленте не нужен; берём только ответы на вопросы.
                        at = asked.get(part.get("tool_use_id"))
                        if at is None:
                            continue
                        msgs[at]["pending"] = False
                        pairs, raw = _ask_answers(part.get("content"))
                        msgs.append({"role": "answer", "ts": ts, "tse": tse,
                                     "pairs": pairs, "text": raw})
                    elif ptype == "thinking":
                        txt = _clean_text(part.get("thinking"), flat=False)
                        if txt:
                            msgs.append({"role": "think", "ts": ts, "tse": tse, "text": _cap(txt)})
                continue
            txt = _msg_text(rec, flat=False)
            if txt:
                # ⛔ 10-18: a compaction writes its summary as a "user" record.
                # It opened a turn nobody answered, and the next real prompt
                # two days later ran on that clock (c3a76ea9: «working 45h»).
                if kind == "user" and not rec.get("isCompactSummary"):
                    _turn_open(len(msgs), tse)
                # ⛔ Здесь "paths" ЗАБЫЛИ, а ветка эта — самая обычная: реплика
                # человека приходит простой строкой, а не списком блоков. Из-за
                # этого у сообщений человека вложения не показывались никогда,
                # даже когда путь распознавался без труда: клипсу рисовать было
                # нечем, и от прикреплённой фотографии оставался голый путь.
                msgs.append({"role": kind, "ts": ts, "tse": tse,
                             "paths": _find_paths(txt, cwd, pcache),
                             "text": _cap(txt)})
    turn_use = _turn_usage(msgs, usages, turn)
    # Быстро разобранная очередь попадает в транскрипт ДВАЖДЫ: enqueue со своим
    # текстом и следом настоящая запись user. Оставляем только те enqueue, для
    # которых записи user так и не появилось. Считаем по счётчику, а не по
    # множеству: один и тот же текст, посланный дважды намеренно, должен
    # остаться в ленте дважды.
    # ⛔ 10-14: the count ignored ORDER. A message resent with the same text
    # while the session was busy was eaten by the EARLIER real record of the
    # first send: it vanished from the feed, its bubble hung on «queued», and
    # the owner sent it a third time (c3a76ea9 22:30:46, 72aab730 22:40:23). An
    # enqueue is a duplicate only of a real record written AFTER it.
    if any(m.get("queued") for m in msgs):
        norm = [" ".join((m.get("text") or "").split()) if m["role"] == "user" else None
                for m in msgs]
        used = set()
        drop = set()
        for i, m in enumerate(msgs):
            if not m.get("queued") or m.get("removed"):
                continue
            for j in range(i + 1, len(msgs)):
                n = msgs[j]
                if (j in used or n["role"] != "user" or n.get("queued")
                        or norm[j] != norm[i]):
                    continue
                if (n.get("tse") or 0) + 2 < (m.get("tse") or 0):
                    continue
                used.add(j)
                drop.add(i)
                break
        msgs = [m for i, m in enumerate(msgs) if i not in drop]
    said = sum(1 for m in msgs
               if m["role"] in ("user", "assistant", "ask", "answer"))
    # Дубли по пути: артефакт часто переиздают, в списке нужен последний.
    seen_art = {}
    for a in arts:
        seen_art[a["path"]] = a
    return {"msgs": msgs, "said": said, "clipped": clipped,
            "arts": sorted(seen_art.values(),
                           key=lambda x: x.get("tse") or 0, reverse=True),
            "turn": turn_use, "sigs": {}}


def _turn_usage(msgs, usages, turn=None):
    """Расход с последней реплики человека — то есть за текущий прогон.

    Якорь тот же, что у времени в статусе: последняя запись с ролью user в
    ленте. Иначе время и токены считались бы от разных точек и не сходились.

    Чтения кэша в заглавное число не идут: за ход их набегает 5,6 млн — это
    один и тот же контекст, перечитанный на каждом шаге, и как «потрачено»
    он врёт на два порядка. Наверху — то, что модель написала.
    """
    anchor = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            anchor = i
    tse = msgs[anchor].get("tse") if anchor >= 0 else None
    # the start of the running turn, not the newest line (see _tx_parse)
    if turn and turn.get("pos", -1) >= 0 and not turn.get("fresh"):
        anchor = turn["pos"] - 1 if turn["pos"] >= len(msgs) or msgs[turn["pos"]].get("role") != "user" else turn["pos"]
        tse = turn.get("tse") or tse
    # Отдаём и саму точку отсчёта: лента у клиента обрезана лимитом, и на
    # длинном ходе он бы не нашёл в ней реплику человека — время поехало бы
    # от более старой, а токены считались бы от правильной.
    out = {"out": 0, "in": 0, "cache_read": 0, "steps": 0, "tse": tse}
    for pos, u in usages:
        if pos <= anchor:
            continue
        out["steps"] += 1
        out["out"] += u.get("output_tokens", 0)
        out["in"] += (u.get("input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0))
        out["cache_read"] += u.get("cache_read_input_tokens", 0)
    return out


# ----------------------------------------------------------- смена аккаунта
#
# Паролей и токенов здесь нет: `claude auth login` сам ведёт штатный OAuth —
# печатает ссылку и ждёт код, который человек получает в браузере. Панель только
# вылавливает ссылку из его вывода и передаёт обратно введённый код.
#
# Каталог лежит в ~/.claude, а не в /tmp: /tmp общий и world-writable, а тут
# внутри и FIFO, и лог флоу.

AUTH_DIR = CFG.state("rc-auth")


def _auth_paths():
    j = lambda n: os.path.join(AUTH_DIR, n)
    return j("in"), j("out.log"), j("run.sh"), j("pid"), j("before")


# 20.11 «keep» mode: sign in to an account only to SAVE it for the automatic
# switch (rc_accounts.py). The CLI runs with its own CLAUDE_CONFIG_DIR, so its
# result goes to a separate Keychain item and this Mac stays on its account; no
# session restarts. When the flow ends, adopt_iso() files it and wipes the rest.
AUTH_ISO = os.path.join(AUTH_DIR, "iso")
AUTH_KEEP = os.path.join(AUTH_DIR, "keep")
AUTH_ADDED = os.path.join(AUTH_DIR, "added")


def _auth_keep_result():
    """(keep, added email). Files the finished keep-mode sign-in once."""
    if not os.path.exists(AUTH_KEEP):
        return False, ""
    try:
        with open(AUTH_ADDED) as fh:
            return True, fh.read().strip()
    except OSError:
        pass
    if _auth_alive() or not os.path.isdir(AUTH_ISO):
        return True, ""
    try:
        import rc_accounts
        email = rc_accounts.adopt_iso(AUTH_ISO)
    except Exception as exc:
        email = ""
        log_line = "keep sign-in could not be saved: %r" % (exc,)
        try:
            with open(_auth_paths()[1], "a") as fh:
                fh.write("\n" + log_line[:300] + "\n")
        except OSError:
            pass
    if email:
        with open(AUTH_ADDED, "w") as fh:
            fh.write(email)
    return True, email


def _auth_pid():
    try:
        with open(_auth_paths()[3]) as fh:
            return int(fh.read().strip())
    except Exception:
        return 0


_AUTH_PROC = None


def _auth_alive():
    """Жив ли процесс флоу — именно процесс, а не файл.

    Наличие FIFO на диске не значит ничего: она переживает и таймаут, и успешный
    вход. Ровно на этом расхождении первая версия и вставала — os.path.exists()
    говорил «флоу идёт», после чего запись в неё блокировалась навсегда.

    И одного os.kill(pid, 0) мало. Флоу — потомок панели, а умерший потомок,
    которого никто не пожал, остаётся зомби: kill(pid, 0) на нём отвечает
    «жив» до конца жизни панели. Поэтому сначала poll() по сохранённой ручке
    (он же и пожинает зомби), а если ручки нет — панель успела перезапуститься —
    смотрим состояние процесса в ps.
    """
    pid = _auth_pid()
    if not pid:
        return False
    if _AUTH_PROC is not None and _AUTH_PROC.pid == pid:
        return _AUTH_PROC.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    st = subprocess.run(["/bin/ps", "-o", "state=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def _auth_kill():
    pid = _auth_pid()
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            return
        for _ in range(10):
            time.sleep(0.2)
            if not _auth_alive():
                return


def _auth_text(log):
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _auth_url(log):
    # ссылка приходит внутри OSC-8, то есть обёрнута в ESC ]8;; ... BEL
    m = re.search(r"https://[^\s\x07\x1b]*oauth/authorize\?[^\s\x07\x1b]+",
                  _auth_text(log))
    return m.group(0) if m else ""


def _auth_tail(log, n=240):
    t = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;;|[\x00-\x08\x0b-\x1f\x7f]", " ",
               _auth_text(log))
    return re.sub(r"\s+", " ", t).strip()[-n:]


def auth_state():
    """Состояние флоу для опроса из браузера. Ходит по GET, лока не берёт."""
    fifo, log, _sh, _pidf, beforef = _auth_paths()
    alive = _auth_alive()
    if not alive and os.path.exists(fifo):
        # процесс ушёл — снимаем FIFO, чтобы «флоу идёт» больше никогда не врало
        try:
            os.unlink(fifo)
        except OSError:
            pass
    before = ""
    try:
        with open(beforef) as fh:
            before = fh.read().strip()
    except OSError:
        pass
    now = G.account_id()
    keep, added = _auth_keep_result()
    return {"running": alive, "url": _auth_url(log), "account": now,
            "before": before, "tail": _auth_tail(log),
            "switched": bool(not keep and before and now != before and now != "unknown"),
            "keep": keep, "added": added,
            "started": os.path.exists(_auth_paths()[4])}


def act_auth_start(target):
    """Запустить вход в другой аккаунт. Возвращается сразу — ссылку заберёт
    опрос /api/auth, чтобы не держать общий лок панели все эти секунды."""
    email = ((target or {}).get("email") or "").strip()
    if email and not re.match(r"^[^-@\s][^@\s]*@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("does not look like an email: %r" % email[:60])
    keep = bool((target or {}).get("keep"))

    _auth_kill()
    _auth_drop_iso()
    shutil.rmtree(AUTH_DIR, ignore_errors=True)
    os.makedirs(AUTH_DIR, 0o700, exist_ok=True)
    os.chmod(AUTH_DIR, 0o700)
    fifo, log, sh, pidf, beforef = _auth_paths()
    os.mkfifo(fifo, 0o600)
    # лог создаём сами и узко: bash-редирект отдал бы его по umask, а вывод
    # флоу — не то, что стоит оставлять читаемым для всей машины
    os.close(os.open(log, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))

    # FIFO держит открытой на чтение-запись сам скрипт (exec 3<>): иначе CLI
    # поймает EOF, как только мы закроем свой конец, и оборвёт флоу, не дождавшись
    # кода. Это единственный способ разнести запуск и ввод кода на два запроса.
    if keep:
        os.makedirs(AUTH_ISO, 0o700, exist_ok=True)
        with open(AUTH_KEEP, "w") as fh:
            fh.write(email or "-")
    with open(sh, "w") as fh:
        fh.write("#!/bin/bash\n"
                 + ("export CLAUDE_CONFIG_DIR=%s\n" % shlex.quote(AUTH_ISO) if keep else "")
                 + "exec 3<>%s\n"
                 "exec %s auth login --claudeai%s <&3 >%s 2>&1\n"
                 % (shlex.quote(fifo), shlex.quote(CLAUDE_BIN),
                    (" --email " + shlex.quote(email)) if email else "",
                    shlex.quote(log)))
    os.chmod(sh, 0o700)
    global _AUTH_PROC
    p = subprocess.Popen(["/bin/bash", sh], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    _AUTH_PROC = p
    with open(pidf, "w") as fh:
        fh.write(str(p.pid))
    with open(beforef, "w") as fh:
        fh.write(G.account_id())
    if keep:
        return "sign-in started (save only, this Mac keeps its account) · the link appears in a few seconds"
    return "sign-in started · the link appears in a few seconds"


def act_auth_code(target):
    """Передать в идущий флоу код, полученный в браузере."""
    code = ((target or {}).get("code") or "").strip()
    if not re.match(r"^[A-Za-z0-9._~#\-]{8,400}$", code):
        raise ValueError("that does not look like a confirmation code")
    fifo = _auth_paths()[0]
    if not _auth_alive():
        shutil.rmtree(AUTH_DIR, ignore_errors=True)
        raise ValueError("no sign-in flow running — start it again")
    try:
        # O_NONBLOCK обязателен: без него open() на FIFO без читателя виснет в
        # ядре навсегда и уносит с собой общий лок панели
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("nothing to accept the code, the flow died (%s) — start again"
                         % exc.strerror)
    try:
        os.write(fd, (code + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return "code sent · watching for the account switch"


def _auth_drop_iso():
    if os.path.isdir(AUTH_ISO):
        try:
            import rc_accounts
            rc_accounts.drop_iso(AUTH_ISO)
        except Exception:
            pass


def act_auth_cancel(_=None):
    _auth_kill()
    _auth_drop_iso()
    shutil.rmtree(AUTH_DIR, ignore_errors=True)
    return "sign-in flow stopped"


def act_acct_switch(target):
    """Move this Mac to an account whose sign-in is saved: no browser, no code.
    The guardian's next pass migrates the sessions, as after any switch."""
    email = ((target or {}).get("email") or "").strip()
    if not re.match(r"^[^-@\s][^@\s]*@[^@\s]+\.[^@\s]+$", email):
        raise ValueError("does not look like an email: %r" % email[:60])
    if _auth_alive():
        raise ValueError("a sign-in is running, finish or cancel it first")
    import rc_accounts
    ok, msg = rc_accounts.switch_to(email, why="panel")
    if not ok:
        raise ValueError(msg)
    return "switched to %s · the guardian moves the agents within a minute" % email


def act_acct_auto(target):
    """Turn the automatic switch on a used-up limit on or off."""
    import rc_accounts
    on = rc_accounts.set_auto(bool((target or {}).get("on")))
    return "auto-switch is " + ("on" if on else "off")


def act_acct_forget(target):
    """Drop an account's saved sign-in (it stays in the list, just not usable)."""
    email = ((target or {}).get("email") or "").strip()
    if not email:
        raise ValueError("no email")
    import rc_accounts
    rc_accounts.forget(email)
    return "saved sign-in for %s removed" % email


# ------------------------------------------------------- account migration

def preflight_text():
    """The guardian's own READY / NOT READY report, run before switching account.

    Safe to call at any time: preflight() returns before main() takes the pass
    lock, so this never competes with a running cycle.
    """
    r = subprocess.run([PY, GUARDIAN, "--preflight"],
                       capture_output=True, text=True, timeout=180)
    return (r.stdout or "") + (r.stderr or "") or "(empty)"


def act_migrate(_=None):
    """Re-materialise the working set under the current account.

    Deliberately a subprocess: --migrate is a full guardian pass that takes its
    own lock, kills every restored session and kickstarts the daemon. Calling it
    while we hold the lock ourselves would deadlock it out for 30 s and then let
    it run half-blind.

    Destructive by nature — the caller must confirm before reaching this.
    """
    r = subprocess.run([PY, GUARDIAN, "--migrate"],
                       capture_output=True, text=True, timeout=900)
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return "migration finished: " + (" · ".join(tail[-3:]) if tail else "no output")


# ---------------------------------------------------------------- resolve

def resolve(pref):
    reg = G.load(G.REG_PATH, {})
    hits = [u for u in reg.get("sessions", {}) if u.startswith(pref)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise KeyError("no session with prefix %s" % pref)
    raise KeyError("prefix %s is ambiguous (%d matches)" % (pref, len(hits)))


# ---------------------------------------------------------------- actions

def kill_tree(pid):
    """SIGTERM the whole process group, then SIGKILL what survives.

    launch() starts each session with setsid(), so the bash wrapper, `script`
    and claude itself share one process group — signalling the group is what
    actually ends a session instead of orphaning half of it.
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return "the process is already gone"
    # ⛔ The group is not the whole session: a background shell it started
    # lives in its own session and outlived the kill on 09-25 as an endless
    # `until grep …; do sleep 5; done`. Collected now, while they still hang
    # under claude — after the kill they belong to launchd.
    rows = G.ps_tree()
    below = [int(p) for p in G.descendants(pid, rows)]
    # and the launch wrapper above claude (bash → cat, script): left alive it
    # keeps the session's channel open and the next send breaks on it
    wrap = G.wrapper_pgid(pid, rows)

    def _signal(sig):
        for g in (pgid, wrap):
            if g:
                try:
                    os.killpg(g, sig)
                except OSError:
                    pass
        for p in below:
            try:
                os.kill(p, sig)
            except OSError:
                pass
    _signal(signal.SIGTERM)
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            _signal(signal.SIGKILL)       # stragglers of the tree, if any
            return "stopped"
    _signal(signal.SIGKILL)
    return "stopped (SIGKILL)"


# ----------------------------------------------------------- процессы машины
# Не _UUID_RE: такое имя ниже уже занято якорной версией (^…$) для resolve().
_UUID_IN_ARGV_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _proc_rows():
    """[(pid, pgid, ppid, rss_kb, cpu, etime, command)] для процессов этого
    пользователя. `ps` без заголовков, чтобы не парсить шапку."""
    # ⛔ -ww обязателен: без него ps режет command до 80 символов даже в
    # трубу, и uuid из `--resume <uuid>` не влезает. -U — выбор по пользователю
    # (строчная -u на macOS — это формат вывода, а не фильтр).
    out = subprocess.run(
        ["ps", "-x", "-ww", "-U", str(os.getuid()),
         "-o", "pid=,pgid=,ppid=,rss=,pcpu=,etime=,command="],
        capture_output=True, text=True, timeout=10).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]),
                         int(parts[3]), float(parts[4]), parts[5], parts[6]))
        except ValueError:
            continue
    return rows


def proc_list(limit=40):
    """Верхние процессы по памяти и к какой сессии RC каждый относится.

    Сессия — это process group (launch() делает setsid), поэтому uuid ищем по
    всей группе: он есть в argv обёртки `/tmp/rc-launch-<uuid>.sh` и в
    `claude --resume <uuid>`, а у дочерних node/bash его нет.
    """
    rows = _proc_rows()
    # Реестр знает про каждую сессию uuid, имя и pid главного claude — берём
    # оттуда, а не вычисляем заново.
    titles, mains, by_prefix = {}, {}, {}
    try:
        for r in snapshot().get("rows", []):
            uid = r.get("uuid")
            if not uid:
                continue
            titles[uid] = (r.get("title") or r.get("name") or "").strip()
            mains[uid] = r.get("pid")
            by_prefix[uid[:8]] = uid
    except Exception:
        pass
    pid2uuid = {v: k for k, v in mains.items() if v}
    by_pg = {}
    for pid, pgid, ppid, rss, cpu, et, cmd in rows:
        if pgid in by_pg:
            continue
        # Главный claude «новой» сессии стартовал без --resume: uuid в argv нет,
        # зато реестр помнит его pid.
        if pid in pid2uuid:
            by_pg[pgid] = pid2uuid[pid]
            continue
        m = _UUID_IN_ARGV_RE.search(cmd)
        if m and "--resume" in cmd:
            by_pg[pgid] = m.group(0)
            continue
        # Обёртка «новой» сессии стартует без --resume: у неё в argv только
        # /tmp/rc-launch-<8 символов>.sh — сопоставляем по префиксу uuid.
        w = re.search(r"rc-launch-([0-9a-f]{8})", cmd)
        if w and w.group(1) in by_prefix:
            by_pg[pgid] = by_prefix[w.group(1)]
    me, parent = os.getpid(), os.getppid()
    procs, tot = [], {"sessions_mb": 0.0, "chrome_mb": 0.0, "other_mb": 0.0,
                      "total_mb": 0.0, "count": 0}
    for pid, pgid, ppid, rss, cpu, et, cmd in rows:
        mb = rss / 1024.0
        uuid = by_pg.get(pgid)
        tot["count"] += 1
        tot["total_mb"] += mb
        if uuid:
            tot["sessions_mb"] += mb
        elif "Google Chrome" in cmd:
            tot["chrome_mb"] += mb
        else:
            tot["other_mb"] += mb
        head = cmd.split()[0] if cmd.split() else cmd
        base = os.path.basename(head)
        if base.endswith("Chrome") or "Google Chrome" in head:
            base = "Chrome"
        args = cmd[len(head):].strip()
        short = (base + " " + args)[:70].strip()
        procs.append({
            "pid": pid, "pgid": pgid, "rss_mb": round(mb, 1), "cpu": round(cpu, 1),
            "etime": et, "cmd": short, "full": cmd[:240], "uuid": uuid,
            "title": titles.get(uuid, "") if uuid else "",
            "main": bool(uuid) and mains.get(uuid) == pid,
            "killable": pid not in (1, me, parent),
        })
    procs.sort(key=lambda x: -x["rss_mb"])
    for k in ("sessions_mb", "chrome_mb", "other_mb", "total_mb"):
        tot[k] = round(tot[k], 1)
    return {"procs": procs[:limit], "totals": tot}


def act_kill_proc(target):
    """Убить ОДИН процесс по pid: SIGTERM, через 5 с SIGKILL. Не группу —
    кнопка у обычного процесса должна снимать только его, а сессию целиком
    снимает act_kill по uuid (с реестром и очередью)."""
    pid = int((target or {}).get("pid") or 0) if isinstance(target, dict) else int(target or 0)
    if pid <= 1 or pid in (os.getpid(), os.getppid()):
        raise ValueError("pid %d cannot be killed from the panel" % pid)
    name = ""
    for p, _, _, rss, _, _, cmd in _proc_rows():
        if p == pid:
            name = "%s (%d MB)" % (cmd[:60], rss // 1024)
            break
    if not name:
        return "pid %d no longer exists" % pid
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return "stopped %s" % name
    os.kill(pid, signal.SIGKILL)
    return "stopped (SIGKILL) %s" % name


# ------------------------------------------------------- перенос между машинами
#
# Сессия переезжает как разговор: транскрипт, tip-файл, каталог вложений и
# запись реестра. ⛔ Окружение НЕ переезжает — рабочие каталоги старой машины
# здесь недоступны, и сессия об этом не знает: она уверенно назовёт каталог
# старой машины, пока её не заставят выполнить pwd (проверено 03-09). Поэтому
# после подъёма на приёмнике ей первым же сообщением объясняют, где она.
#
# Приёмник — панель соседа, а не ssh: у неё уже есть реестр, лок гардиана и
# подъём. Источник только собирает tar и убивает свой процесс — двух живых
# копий одного uuid быть не должно, иначе две истории под одним номером.

IMPORT_MAX = 300 * 1024 * 1024
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MOVED_NOTE = (
    "⚠️ Эта сессия перенесена с машины «%s» на «%s» (%s). Рабочие каталоги "
    "старой машины здесь недоступны; пути вида /Users/olduser/… ведут через "
    "симлинк в новый домашний каталог, и репозитории там могут лежать не все. "
    "Прежде чем продолжать, выполни `pwd; hostname; ls ~` и сверь, где лежит "
    "нужный проект. Не рассуждай по памяти о том, что есть на диске.")


def _own_panel_url():
    return "http://%s:%s" % (os.environ.get("RC_PANEL_BIND", ""),
                             os.environ.get("RC_PANEL_PORT", "8788"))


def _bundle_paths(uuid):
    base = os.path.join(G.PROJ, uuid)
    return base + ".jsonl", base + ".ccr-tip.json", base


def make_bundle(uuid):
    """tar без сжатия: meta.json (запись реестра, откуда), транскрипт, tip,
    каталог вложений сессии. Без сжатия — транскрипт до 40 МБ, tailnet быстрый,
    а gzip на мини стоил бы больше, чем сэкономил."""
    import io
    import tarfile
    reg = G.load(G.REG_PATH, {})
    rec = reg.get("sessions", {}).get(uuid)
    if rec is None:
        raise KeyError("no registry record for %s" % uuid[:8])
    tp, tip, sd = _bundle_paths(uuid)
    if not os.path.exists(tp):
        raise ValueError("no transcript on disk for %s" % uuid[:8])
    repos = [repo_state(d) for d in repos_touched(uuid)]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        meta = json.dumps({"uuid": uuid, "rec": rec,
                           "from": os.uname().nodename.replace(".local", ""),
                           "workdir": G.WORKDIR, "repos": repos},
                          ensure_ascii=False).encode("utf-8")
        ti = tarfile.TarInfo("meta.json")
        ti.size = len(meta)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(meta))
        tar.add(tp, arcname=uuid + ".jsonl")
        if os.path.exists(tip):
            tar.add(tip, arcname=uuid + ".ccr-tip.json")
        if os.path.isdir(sd):
            tar.add(sd, arcname=uuid)
        # env-файлы затронутых репозиториев: через git они не едут, а без них
        # проект на приёмнике мёртв. Свои машины в tailnet — везём как есть.
        for r in repos:
            for f in r["envs"]:
                fp = os.path.join(G.HOME, r["dir"], f)
                try:
                    if os.path.getsize(fp) <= ENV_FILE_MAX:
                        tar.add(fp, arcname="envs/%s/%s" % (r["dir"], f))
                except OSError:
                    continue
    return buf.getvalue()


# --- git-проверка перед переездом --------------------------------------------
#
# Окружение не едет, значит его везёт git. Что делает СЕССИЯ (коммиты,
# хэндофф) и что проверяет ПАНЕЛЬ (чисто ли дерево, запушено ли) — разные
# вещи: сессии верить на слово нельзя, а `git status` детерминирован.

REPO_PATH_RE = re.compile(r"(?:%s)/([A-Za-z0-9._\-]+)"
                          % re.escape(G.HOME))
SKIP_DIRS = {"Library", "Downloads", "Desktop", "Documents", "bin",
             "rc-work", "rc-stage", "node_modules"}
REPOS_MAX = 8
ENV_FILE_MAX = 1024 * 1024

PREPARE_NOTE = (
    "⚠️ Готовим перенос этой сессии на другую машину. Переедет только "
    "разговор, окружение — нет, поэтому всё нужное должно оказаться в GitHub. "
    "Сделай сейчас, без обсуждения:\n"
    "1. В репозиториях %s — `git status`; закоммить все изменения осмысленными "
    "сообщениями и `git push` (ветке без upstream — `git push -u origin <ветка>`).\n"
    "2. Обнови HANDOFF.md в каждом затронутом репозитории: что сделано, что не "
    "закончено, с чего продолжать на новой машине.\n"
    "3. ⛔ Не коммить .env и файлы с ключами.\n"
    "4. Когда всё запушено, ответь одной строкой: RC-MOVE-READY.")


def _git(d, *args, timeout=120):
    try:
        r = subprocess.run(["git", "-C", os.path.join(G.HOME, d)] + list(args),
                           capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)[:200]
    return r.returncode, (r.stdout or r.stderr).strip()


def repos_touched(uuid, limit_bytes=60_000_000):
    """Git-репозитории под домом, которые сессия упоминала; по частоте."""
    path = os.path.join(G.PROJ, uuid + ".jsonl")
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit_bytes).decode("utf-8", "replace")
    except OSError:
        return []
    counts = {}
    for m in REPO_PATH_RE.finditer(data):
        d = m.group(1)
        if d.startswith(".") or d in SKIP_DIRS:
            continue
        counts[d] = counts.get(d, 0) + 1
    out = []
    for d, _n in sorted(counts.items(), key=lambda x: -x[1]):
        if os.path.isdir(os.path.join(G.HOME, d, ".git")):
            out.append(d)
        if len(out) >= REPOS_MAX:
            break
    return out


def repo_state(d):
    _, branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
    _, sha = _git(d, "rev-parse", "--short", "HEAD")
    rc, remote = _git(d, "remote", "get-url", "origin")
    if rc:
        remote = ""
    _, st = _git(d, "status", "--porcelain")
    dirty = len([ln for ln in st.splitlines() if ln.strip()])
    rc, ahead = _git(d, "rev-list", "--count", "@{u}..HEAD")
    ahead = int(ahead) if rc == 0 and ahead.isdigit() else -1   # -1: нет upstream
    hand = os.path.join(G.HOME, d, "HANDOFF.md")
    try:
        handoff_h = round((time.time() - os.path.getmtime(hand)) / 3600, 1)
    except OSError:
        handoff_h = None
    try:
        envs = sorted(f for f in os.listdir(os.path.join(G.HOME, d))
                      if f.startswith(".env")
                      and os.path.isfile(os.path.join(G.HOME, d, f)))
    except OSError:
        envs = []
    return {"dir": d, "branch": branch, "sha": sha, "remote": remote,
            "dirty": dirty, "ahead": ahead, "handoff_h": handoff_h, "envs": envs,
            "ready": bool(remote) and dirty == 0 and ahead == 0}


def move_check(uuid):
    repos = [repo_state(d) for d in repos_touched(uuid)]
    return {"id": uuid[:8], "repos": repos,
            "ready": all(r["ready"] for r in repos)}


def _repo_line(r):
    bits = []
    if r["dirty"]:
        bits.append("%d uncommitted" % r["dirty"])
    if r["ahead"] > 0:
        bits.append("%d unpushed" % r["ahead"])
    if r["ahead"] < 0:
        bits.append("no upstream")
    if not r["remote"]:
        bits.append("no remote")
    return "%s (%s@%s%s)" % (r["dir"], r["branch"], r["sha"],
                             (": " + ", ".join(bits)) if bits else "")


_MOVE_WAITERS = {}
MOVE_WAIT_S = 1800


def _set_pending(uuid, val):
    def edit():
        reg = G.load(G.REG_PATH, {})
        rec = reg.get("sessions", {}).get(uuid)
        if rec is None:
            return
        if val is None:
            rec.pop("move_pending", None)
        else:
            rec["move_pending"] = val
        G.save(G.REG_PATH, reg)
    try:
        with_lock(edit)
    except Exception:
        pass


def _ready_marker(uuid, offset):
    """Ответила ли сессия RC-MOVE-READY ПОСЛЕ просьбы (offset — размер
    транскрипта в момент просьбы). Сама просьба тоже содержит эти слова, поэтому
    смотрим только ответы ассистента."""
    path = os.path.join(G.PROJ, uuid + ".jsonl")
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in tail.splitlines():
        if "RC-MOVE-READY" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        c = (d.get("message") or {}).get("content")
        texts = [c] if isinstance(c, str) else [
            b.get("text", "") for b in (c or []) if isinstance(b, dict)]
        if any("RC-MOVE-READY" in x for x in texts):
            return True
    return False


def _move_waiter(uuid, to, up, notify, offset):
    """Фон: дождаться READY от сессии и чистого git, потом перевезти.

    Второй клик человека не нужен: намерение он уже подтвердил. Готовность
    считаем по ДВУМ признакам — маркер в ответе сессии и `move_check` — иначе
    можно уехать между «код запушен» и «HANDOFF.md дописан»."""
    deadline = time.time() + MOVE_WAIT_S
    _set_pending(uuid, {"to": to, "since": G.now(), "state": "waiting"})
    last = ""
    while time.time() < deadline:
        time.sleep(20)
        try:
            if not _ready_marker(uuid, offset):
                continue
            chk = move_check(uuid)
            if not chk["ready"]:
                last = "; ".join(_repo_line(r) for r in chk["repos"] if not r["ready"])
                continue
            _set_pending(uuid, {"to": to, "since": G.now(), "state": "moving"})
            msg = act_transfer({"id": uuid[:8], "to": to, "up": up,
                                "notify": notify, "force": False})
            _set_pending(uuid, None)
            G.log("auto-move %s: %s" % (uuid[:8], msg))
            return
        except Exception as exc:
            last = str(exc)[:200]
            G.log("auto-move %s: %s" % (uuid[:8], last))
    _set_pending(uuid, {"to": to, "since": G.now(), "state": "timeout",
                        "last": last})
    G.log("auto-move %s: gave up after %ds (%s)" % (uuid[:8], MOVE_WAIT_S, last))


def act_move_prepare(target):
    """Попросить сессию закоммитить, запушить и обновить HANDOFF.md — и, если
    дан `to`, дождаться READY и перевезти самому.

    Это то, что делает сессия; проверяет потом всё равно панель — move_check
    перед самим переносом."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {id}")
    uuid = resolve(target.get("id") or "")
    to = (target.get("to") or "").strip().rstrip("/")
    chk = move_check(uuid)
    todo = [r for r in chk["repos"] if not r["ready"]]
    if not todo:
        return "%s: repos clean and pushed — ready to move" % uuid[:8]
    if pid_for(uuid) is None:
        raise ValueError("%s is not running — launch it first, or move as is"
                         % uuid[:8])
    try:
        offset = os.path.getsize(os.path.join(G.PROJ, uuid + ".jsonl"))
    except OSError:
        offset = 0
    act_say({"id": uuid[:8],
             "text": PREPARE_NOTE % ", ".join(_repo_line(r) for r in todo)})
    if to and re.match(r"^https?://[\w.\-]+(:\d+)?$", to):
        w = _MOVE_WAITERS.get(uuid)
        if not (w and w.is_alive()):
            w = threading.Thread(
                target=_move_waiter,
                args=(uuid, to, bool(target.get("up", True)),
                      bool(target.get("notify", True)), offset),
                daemon=True)
            w.start()
            _MOVE_WAITERS[uuid] = w
        return ("%s asked to commit, push and update HANDOFF.md in: %s — will "
                "move to %s by itself once it answers RC-MOVE-READY and git is "
                "clean (up to %d min)"
                % (uuid[:8], ", ".join(r["dir"] for r in todo), to,
                   MOVE_WAIT_S // 60))
    return ("%s asked to commit, push and update HANDOFF.md in: %s. "
            "Choose move again when it answers RC-MOVE-READY."
            % (uuid[:8], ", ".join(r["dir"] for r in todo)))


def act_transfer(target):
    """Перевезти сессию на другую машину: {id, to (URL панели), up, notify}.

    `to` — URL, а не id устройства: списки устройств у машин свои, и «local»
    у хаба — это чужой хост у источника. URL один и тот же отовсюду.
    """
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, to}")
    uuid = resolve(target.get("id") or "")
    to = (target.get("to") or "").strip().rstrip("/")
    if not re.match(r"^https?://[\w.\-]+(:\d+)?$", to):
        raise ValueError("to must look like http://host:port")
    if to == _own_panel_url():
        raise ValueError("that is this machine")
    up = bool(target.get("up", True))
    notify = bool(target.get("notify", True))
    force = bool(target.get("force", False))
    chk = move_check(uuid)
    if not chk["ready"] and not force:
        raise ValueError("not ready — %s. Prepare first, or move as is"
                         % "; ".join(_repo_line(r) for r in chk["repos"]
                                     if not r["ready"]))

    import urllib.request
    try:
        with urllib.request.urlopen(to + "/api/state", timeout=10) as r:
            host = ((json.loads(r.read().decode("utf-8", "replace")).get("meta")
                     or {}).get("host") or to)
    except Exception as exc:
        raise ValueError("%s did not answer: %s" % (to, str(exc)[:80]))

    # ⛔ Сначала убить, потом собирать: транскрипт должен быть дописан, и на
    # источнике не должно остаться процесса, который допишет его после отъезда.
    pid = pid_for(uuid)
    note = kill_tree(pid) if pid is not None else "was not running"
    blob = make_bundle(uuid)

    req = urllib.request.Request(
        to + "/import?up=%d&notify=%d" % (up, notify), data=blob,
        headers={"Content-Type": "application/x-tar"})
    try:
        # долго: приёмник клонирует/подтягивает репозитории до подъёма
        with urllib.request.urlopen(req, timeout=1500) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise ValueError("import on %s failed: %s — сессия осталась здесь, снята "
                         "с очереди (%s)" % (host, str(exc)[:100], note))
    if not resp.get("ok"):
        raise ValueError("import on %s refused: %s" % (host, resp.get("msg")))

    def edit():
        reg = G.load(G.REG_PATH, {})
        rec = reg["sessions"][uuid]
        rec["state"] = "dead"
        rec["retired"] = True
        rec["moved_to"] = to
        rec["moved_host"] = host
        rec["moved_at"] = G.now()
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    return "%s → %s: %s (%.1f MB; здесь %s, снята с очереди)" % (
        uuid[:8], host, resp.get("msg"), len(blob) / 1048576.0, note)


def _land_repos(repos, envs):
    """Довезти окружение по манифесту: клонировать отсутствующие репозитории,
    подтянуть имеющиеся (только чистые), положить env-файлы, которых нет.
    Детерминированно и до подъёма сессии — ей остаётся сверить, а не тянуть."""
    lines = []
    for r in repos:
        d, remote, branch = r.get("dir"), r.get("remote"), r.get("branch")
        if not d or not re.match(r"^[A-Za-z0-9._\-]+$", d):
            continue
        dest = os.path.join(G.HOME, d)
        if not os.path.isdir(dest):
            if not remote:
                lines.append("%s: нет remote у источника — не клонирован" % d)
                continue
            rc, out = _git(".", "clone", "--quiet", remote, dest, timeout=1200)
            if rc:
                lines.append("%s: clone не удался (%s)" % (d, out[-120:]))
                continue
            if branch and branch != "HEAD":
                rc, out = _git(d, "checkout", "--quiet", branch)
                if rc:
                    _git(d, "checkout", "--quiet", "-b", branch, "origin/" + branch)
            _, sha = _git(d, "rev-parse", "--short", "HEAD")
            lines.append("%s: склонирован, %s@%s" % (d, branch, sha))
        else:
            st = repo_state(d)
            if st["dirty"]:
                lines.append("%s: уже есть, но с %d незакоммиченными — не трогал (%s@%s)"
                             % (d, st["dirty"], st["branch"], st["sha"]))
                continue
            _git(d, "fetch", "--quiet", "--all", timeout=600)
            if branch and branch != "HEAD" and branch != st["branch"]:
                rc, out = _git(d, "checkout", "--quiet", branch)
                if rc:
                    _git(d, "checkout", "--quiet", "-b", branch, "origin/" + branch)
            rc, out = _git(d, "pull", "--quiet", "--ff-only", timeout=600)
            _, sha = _git(d, "rev-parse", "--short", "HEAD")
            lines.append("%s: обновлён, %s@%s%s"
                         % (d, branch, sha, "" if rc == 0 else " (pull не ff: " + out[-80:] + ")"))
        if sha_src := r.get("sha"):
            if sha_src and sha_src != _git(d, "rev-parse", "--short", "HEAD")[1]:
                lines[-1] += " ⚠️ у источника был %s" % sha_src
    for name, blob in envs.items():
        _, d, f = name.split("/")
        dest = os.path.join(G.HOME, d, f)
        if not os.path.isdir(os.path.join(G.HOME, d)):
            continue
        if os.path.exists(dest):
            continue
        try:
            with open(dest, "wb") as fh:
                fh.write(blob)
            os.chmod(dest, 0o600)
            lines.append("%s/%s: env-файл привезён" % (d, f))
        except OSError as exc:
            lines.append("%s/%s: env не записан (%s)" % (d, f, exc))
    return lines


def import_bundle(blob, up=True, notify=True):
    """Принять сессию от соседа: распаковать в PROJ, занести в реестр, поднять."""
    import io
    import tarfile
    if not blob:
        raise ValueError("empty bundle")
    if len(blob) > IMPORT_MAX:
        raise ValueError("bundle too large")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
        names = tar.getnames()
        if "meta.json" not in names:
            raise ValueError("no meta.json in bundle")
        meta = json.loads(tar.extractfile("meta.json").read().decode("utf-8"))
        uuid = meta.get("uuid") or ""
        if not _UUID_RE.match(uuid):
            raise ValueError("bad uuid in bundle")
        allowed = (uuid + ".jsonl", uuid + ".ccr-tip.json")
        env_members = []
        for n in names:
            parts = n.split("/")
            if ".." in parts or n.startswith("/"):
                raise ValueError("bad path in bundle: %r" % n)
            if n == "meta.json" or n in allowed or n == uuid or n.startswith(uuid + "/"):
                continue
            if (len(parts) == 3 and parts[0] == "envs" and parts[2].startswith(".env")
                    and re.match(r"^[A-Za-z0-9._\-]+$", parts[1])):
                env_members.append(n)
                continue
            raise ValueError("unexpected member in bundle: %r" % n)
        if pid_for(uuid) is not None:
            raise ValueError("%s уже живая здесь" % uuid[:8])
        os.makedirs(G.PROJ, exist_ok=True)
        # mtime сохраняется: гардиан поднимает сам всё, чей транскрипт трогали
        # за 48 часов, и без mtime свежесть считалась бы с момента приёма
        members = [m for m in tar.getmembers()
                   if m.name != "meta.json" and m.name not in env_members]
        tar.extractall(G.PROJ, members=members)
        envs = {n: tar.extractfile(n).read() for n in env_members}

    repo_lines = _land_repos(meta.get("repos") or [], envs)

    rec = dict(meta.get("rec") or {})
    rec["retired"] = False
    rec["state"] = "dead"
    rec.pop("app_session", None)
    rec["moved_from"] = meta.get("from") or ""
    rec["moved_from_workdir"] = meta.get("workdir") or ""
    rec["moved_at"] = G.now()
    rec["moved_repo_lines"] = repo_lines

    def edit():
        reg = G.load(G.REG_PATH, {})
        reg.setdefault("sessions", {})[uuid] = rec
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    msg = "%s принята от %s" % (uuid[:8], rec["moved_from"] or "?")
    if up:
        msg += " · " + act_up(uuid)
        if notify:
            text = moved_note_text(uuid, repo_lines)
            msg += " · " + _notify_moved(uuid, text)
    return msg


def moved_note_text(uuid, repo_lines=None):
    reg = G.load(G.REG_PATH, {})
    rec = reg.get("sessions", {}).get(uuid, {})
    here = os.uname().nodename.replace(".local", "")
    text = MOVED_NOTE % (rec.get("moved_from") or "?", here, G.WORKDIR)
    if repo_lines is None:
        repo_lines = rec.get("moved_repo_lines") or []
    if repo_lines:
        text += "\n\nРепозитории на этой машине:\n" + "\n".join(
            "- " + ln for ln in repo_lines)
        text += ("\nЗависимости (node_modules, venv) не переезжают — "
                 "поставь при необходимости.")
    return text


def _notify_moved(uuid, text, budget_s=300):
    """Донести заметку о переезде и УБЕДИТЬСЯ, что она в транскрипте.

    ⛔ Слать через 5 с после подъёма нельзя: возобновлённая сессия первым делом
    компактит контекст, и набранное в это время пропадает — обе первые
    перевезённые сессии (04-09) заметку не получили. Поэтому ждём, пока TUI
    не компактит, отправляем, проверяем по транскрипту, повторяем."""
    line = " ".join(text.split())
    deadline = time.time() + budget_s
    tries = 0
    while time.time() < deadline and tries < 3:
        st = tui_status(log_for(uuid)).get("kind")
        if st == "compacting" or not _channel_ready(G.fifo_path(uuid)):
            time.sleep(10)
            continue
        tries += 1
        try:
            act_say({"id": uuid[:8], "text": text})
        except Exception as exc:
            time.sleep(15)
            continue
        for _ in range(9):
            time.sleep(10)
            if _delivered(uuid, line):
                return "предупреждена о переезде (попытка %d)" % tries
    return ("⚠️ заметка о переезде не подтверждена транскриптом — нажми "
            "«move note» ещё раз или скажи ей pwd сам")


def act_move_note(target):
    """Повторно отправить сессии заметку о переезде (с тем же списком репозиториев)."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {id}")
    uuid = resolve(target.get("id") or "")
    reg = G.load(G.REG_PATH, {})
    rec = reg.get("sessions", {}).get(uuid, {})
    if not rec.get("moved_from"):
        raise ValueError("%s не переезжала сюда" % uuid[:8])
    return "%s: %s" % (uuid[:8], _notify_moved(uuid, moved_note_text(uuid)))


def act_kill(pref):
    """Stop a live session AND take it out of the queue.

    «Убить» has to mean gone. Leaving retired=False only freed the slot: the
    session dropped straight back into the queue and the guardian restored it
    within the minute, so the button read as a no-op. Reversible — the row moves
    to «Снятые», where «В очередь» and «Поднять» bring it back.
    """
    uuid = resolve(pref)
    pid = pid_for(uuid)
    note = kill_tree(pid) if pid is not None else "was not running"

    def edit():
        reg = G.load(G.REG_PATH, {})
        rec = reg["sessions"][uuid]
        rec["state"] = "dead"
        rec["retired"] = True
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    return "%s %s, retired" % (uuid[:8], note)


def act_kill_requeue(pref):
    """Stop a live session but leave it queued: frees the slot now, comes back
    when one is free again. This is what Убить used to do."""
    uuid = resolve(pref)
    pid = pid_for(uuid)
    if pid is None:
        return "%s is not running" % uuid[:8]
    note = kill_tree(pid)

    def edit():
        reg = G.load(G.REG_PATH, {})
        rec = reg["sessions"][uuid]
        rec["state"] = "dead"
        rec["retired"] = False
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    return "%s %s, left in the queue" % (uuid[:8], note)


def act_kill_app(pid):
    """Stop an app-created chat by pid — the case the app itself cannot do."""
    pid = int(pid)
    rows = {p: a for p, a in ps_rows()}
    args = rows.get(pid, "")
    if "--sdk-url" not in args or "code/sessions/cse_" not in args:
        return "pid %d is not an app session — leaving it alone" % pid
    return "pid %d %s" % (pid, kill_tree(pid))


def _resolve_many(prefs):
    """Resolve a list of prefixes at once, collecting misses instead of raising.

    A bulk action over 40 checkboxes must not die on one stale row — the page
    the user ticked can be a minute old, and a session that finished since then
    is a normal outcome, not an error.
    """
    if isinstance(prefs, str):
        prefs = [p for p in re.split(r"[\s,]+", prefs) if p]
    if not isinstance(prefs, list) or not prefs:
        raise ValueError("no sessions selected")
    if len(prefs) > 200:
        raise ValueError("too many at once: %d" % len(prefs))
    known = list(G.load(G.REG_PATH, {}).get("sessions", {}))
    good, bad = [], []
    for p in prefs:
        hits = [u for u in known if u.startswith(str(p))]
        if len(hits) == 1:
            good.append(hits[0])
        else:
            bad.append(str(p))
    return good, bad


def _tail(bad):
    return (" · not found: %s" % ", ".join(bad[:5])) if bad else ""


def _bulk_flag(prefs, retired):
    """Bulk retire / un-retire. ONE locked pass for the whole selection: N
    separate act_retire() calls each take and free the lock, and a guardian
    cycle slotting in between two of them writes its stale registry copy back
    over the edits already made."""
    good, bad = _resolve_many(prefs)

    def edit():
        reg = G.load(G.REG_PATH, {})
        n = 0
        for uuid in good:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec or rec.get("kind") != "rc":
                continue
            if bool(rec.get("retired")) == retired:
                continue
            rec["retired"] = retired
            n += 1
        if n:
            G.save(G.REG_PATH, reg)
        return n
    n = with_lock(edit)
    word = "retired" if retired else "back in the queue"
    return "%s: %d of %d%s" % (word, n, len(good), _tail(bad))


# Значения уходят в командную строку восстановления. Поэтому они проверяются по
# белому списку, а не «экранируются на всякий случай»: shlex.quote там тоже стоит,
# но единственная защита, про которую можно рассуждать уверенно, — это когда
# в строку физически не может попасть ничего, кроме шести известных слов.
# Полные имена, а не алиасы. Алиас `fable` значит «самая свежая fable», и
# 01-09 он молча переехал с Fable 5 на Fable 5.1: подпись в панели стала
# врать, а сессии с выставленным «fable» сменили бы модель на ближайшем
# подъёме — при том, что человек ничего не трогал. Полное имя так не умеет:
# что выбрано, то и запустится. `claude --model` принимает и то и другое
# («alias for the latest model … or a model's full name»), а незнакомое имя
# ловит своим каталогом ещё до сети — проверено 01-09 на CLI 2.1.257.
# 23-09: вышла Opus 5.5. В каталоге CLI она в секции main и требует клиента
# 2.1.280+ («min_claude_code_version»), а на машине постарше `claude --model
# claude-opus-5-5` падает ещё до сети своим же каталогом — то есть сессия у
# соседа по хабу просто не поднимется. Opus 5 оставлен: он лежит в реестре у
# уже выставленных сессий, и без него панель отвергала бы свою же запись.
# 09-11: вышла Fable 5.2, нужен CLI 2.1.321+ (в 2.1.319 её в каталоге нет).
# Тот же CLI переводит сохранённый в /model выбор Fable 5.1 на алиас `fable`,
# то есть на 5.2; наши сессии с claude-fable-5-1 в реестре это не задевает,
# панель передаёт полное имя флагом. Заодно добавлены Sonnet 5.5 и Haiku 5.5,
# которых в списке не было, хотя CLI их знает давно. Старые оставлены по той
# же причине, что Opus 5: они лежат в реестре у выставленных сессий.
MODEL_CHOICES = (("claude-fable-5-2", "Fable 5.2"),
                 ("claude-fable-5-1", "Fable 5.1"),
                 ("claude-fable-5", "Fable 5"),
                 ("claude-opus-5-5", "Opus 5.5"),
                 ("claude-opus-5", "Opus 5"),
                 ("claude-sonnet-5-5", "Sonnet 5.5"),
                 ("claude-sonnet-5", "Sonnet 5"),
                 ("claude-haiku-5-5", "Haiku 5.5"),
                 ("claude-haiku-4-5", "Haiku 4.5"))
# Старые алиасы остаются разрешёнными: они лежат в реестре у сессий,
# выставленных до перехода на полные имена, и без них панель отвергала бы
# собственную же прошлую запись.
MODELS = tuple(v for v, _ in MODEL_CHOICES) + ("fable", "opus", "sonnet")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _set_opt(target, field, allowed):
    """Проставить model/effort выделенным сессиям. Пустое значение — снять его
    и вернуться к дефолту CLI."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {ids, value}")
    value = (target.get("value") or "").strip()
    if value and value not in allowed:
        raise ValueError("invalid value %r" % value[:40])
    good, bad = _resolve_many(target.get("ids"))

    def edit():
        reg = G.load(G.REG_PATH, {})
        n = 0
        for uuid in good:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec or rec.get("kind") != "rc":
                continue
            if value:
                rec[field] = value
            else:
                rec.pop(field, None)
            n += 1
        if n:
            G.save(G.REG_PATH, reg)
        return n
    n = with_lock(edit)
    head = "%s = %s" % (field, value) if value else "%s reset" % field
    return ("%s · sessions: %d of %d · applies on next launch — "
            "use Restart to apply now%s" % (head, n, len(good), _tail(bad)))


def _other_models():
    """Model ids of Codex (its models cache) and Mistral Vibe (aliases)."""
    out = []
    try:
        import rc_codex
        out += [m for m, _ in rc_codex.models()]
    except Exception:
        pass
    try:
        import rc_vibe
        out += list(rc_vibe.MODELS)
    except Exception:
        pass
    return tuple(out)


def act_set_model(target):
    return _set_opt(target, "model", MODELS + _other_models())


def act_set_effort(target):
    return _set_opt(target, "effort", EFFORTS + ("ultra",))


# ---------------------------------------------------------- the launch bay
#
# 26.11: pick the agent first and connect it here if it is missing, then
# its model; the bay opens on the last choice. RECRUIT shows the
# agents (Claude Code, Codex, Mistral Vibe, MiniMax), each connected or with
# what is missing, then that agent's models and efforts.

BAY_LAST = CFG.state("rc-bay-last.json")
_CLAUDE_LOGIN = {"t": 0.0, "ok": False}


def _claude_signed_in():
    """Claude Code has a login on this computer (cached for a minute)."""
    if time.time() - _CLAUDE_LOGIN["t"] < 60:
        return _CLAUDE_LOGIN["ok"]
    ok = False
    if os.path.exists(os.path.expanduser("~/.claude/.credentials.json")):
        ok = True
    elif sys.platform == "darwin":
        try:
            ok = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                                 "Claude Code-credentials"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        except Exception:
            ok = False
    _CLAUDE_LOGIN.update(t=time.time(), ok=ok)
    return ok


def bay_last():
    try:
        with open(BAY_LAST) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _bay_remember(provider, model, effort, exec_=None):
    """The last launch's choice. A single launch keeps the last executor pick
    (pair off); a pair launch stores it with pair on."""
    prev = bay_last()
    d = {"provider": provider or "claude", "model": model or "", "effort": effort or "",
         "ts": time.time(), "pair": bool(exec_), "exec": exec_ or prev.get("exec") or {}}
    try:
        tmp = BAY_LAST + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, BAY_LAST)
    except OSError:
        pass


def bay_catalog():
    """The agents RECRUIT offers: [{id, label, state, install, models, efforts,
    model_default, effort_default}]. state: ok | install | login | key."""
    import rc_providers
    out = []
    claude_ok = os.path.exists(G.CLAUDE) or bool(shutil.which("claude"))
    out.append({
        "id": "claude", "label": "Claude Code", "sub": "Claude Pro or Max",
        "state": "ok" if claude_ok and _claude_signed_in() else ("login" if claude_ok else "install"),
        "install": "curl -fsSL https://claude.ai/install.sh | bash",
        "models": [["", "CLI default"]] + [list(x) for x in MODEL_CHOICES],
        "efforts": [""] + list(EFFORTS)})
    try:
        import rc_codex
        cbin = rc_codex.codex_bin()
        cmodels = [list(x) for x in rc_codex.models()]
        signed = os.path.exists(os.path.join(rc_codex.CODEX_HOME, "auth.json"))
    except Exception:
        cbin, cmodels, signed = "", [], False
    out.append({
        "id": "codex", "label": "Codex", "sub": "ChatGPT subscription",
        "state": "ok" if cbin and signed else ("login" if cbin else "install"),
        "install": "npm install -g @openai/codex",
        "models": [["", "Codex default"]] + cmodels,
        "efforts": [""] + list(rc_codex.EFFORT_CHOICES) if cbin else []})
    try:
        import rc_vibe
        vbin = rc_vibe.vibe_bin()
        vmodels = [[a, m["display"]] for a, m in rc_vibe.MODELS.items()]
        vdef = rc_vibe.DEFAULT_MODEL
    except Exception:
        vbin, vmodels, vdef = "", [], ""
    out.append({
        "id": "vibe", "label": "Mistral Vibe (DeepSeek, Mistral)", "sub": "Mistral Vibe plan",
        "state": "ok" if vbin and rc_providers.vibe_key() else ("key" if vbin else "install"),
        "install": "curl -LsSf https://mistral.ai/vibe/install.sh | bash",
        "key_url": "https://chat.mistral.ai/code/extensions?focus=key",
        "models": vmodels, "model_default": vdef, "efforts": []})
    for pid, p in rc_providers.PROVIDERS.items():
        if p.get("per_token"):
            continue
        out.append({
            "id": pid, "label": p["label"], "sub": p.get("sub") or (p["label"] + " plan"),
            "state": ("ok" if rc_providers.key(pid) else "key") if claude_ok else "install",
            "install": "curl -fsSL https://claude.ai/install.sh | bash",
            "key_url": p.get("key_url", ""),
            "models": [[p["model"], p["model"]]], "efforts": []})
    last = bay_last()
    for a in out:
        a["model_default"] = a.get("model_default") or (a["models"][0][0] if a["models"] else "")
        a["effort_default"] = ""
        if last.get("provider") == a["id"]:
            if any(m[0] == last.get("model") for m in a["models"]):
                a["model_default"] = last.get("model")
            if last.get("effort") in a["efforts"]:
                a["effort_default"] = last.get("effort")
    ids = [a["id"] for a in out if a["state"] == "ok"]
    first = last.get("provider") if last.get("provider") in ids else (ids[0] if ids else "claude")
    ex = last.get("exec") or {}
    return {"agents": out, "default": first, "pair": bool(last.get("pair")),
            "exec": ex if ex.get("provider") in ids else {}}


def act_provider_key(target):
    """Connect Mistral Vibe or MiniMax from RECRUIT: check the pasted key with one
    cheap call, then keep it in the providers' keys.env (600)."""
    import rc_providers
    provider = ((target or {}).get("provider") or "").strip()
    key = ((target or {}).get("key") or "").strip()
    var = rc_providers.key_var(provider)
    if not var:
        raise ValueError("this agent is not connected with a key")
    if not key:
        raise ValueError("paste the key first")
    ok, why = rc_providers.check_key(provider, key)
    if not ok:
        raise ValueError(why)
    rc_providers.save_key(var, key)
    return "%s connected" % (rc_providers.label(provider) or "Mistral Vibe")


CODEX_LOGIN_LOG = CFG.state("rc-auth-codex.log")
_CODEX_LOGIN = {"proc": None}


def act_codex_login(target=None):
    """Sign Codex in with ChatGPT from RECRUIT: `codex login --device-auth`
    prints a link and a one-time code; the person opens the link, types the
    code, and the CLI writes ~/.codex/auth.json. Polled through codex_login_state."""
    import rc_codex
    cbin = rc_codex.codex_bin()
    if not cbin:
        raise ValueError("Codex is not installed on this computer")
    p = _CODEX_LOGIN.get("proc")
    if p is not None and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
    fd = os.open(CODEX_LOGIN_LOG, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    env = dict(os.environ, NO_COLOR="1")
    _CODEX_LOGIN["proc"] = subprocess.Popen([cbin, "login", "--device-auth"], stdin=subprocess.DEVNULL,
                                            stdout=fd, stderr=fd, env=env, start_new_session=True)
    os.close(fd)
    _CODEX_LOGIN["t0"] = time.time()
    for _ in range(30):
        time.sleep(0.5)
        st = codex_login_state()
        if st.get("code") or st.get("state") != "waiting":
            return st
    return codex_login_state()


def codex_login_state():
    import rc_codex
    try:
        with open(CODEX_LOGIN_LOG, "rb") as fh:
            txt = fh.read().decode("utf-8", "ignore")
    except OSError:
        txt = ""
    txt = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", txt)
    url = (re.findall(r"https://auth\.openai\.com/\S*device\S*", txt) or [""])[-1]
    code = (re.findall(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b", txt) or [""])[-1]
    p = _CODEX_LOGIN.get("proc")
    rc = p.poll() if p is not None else None
    signed = os.path.exists(os.path.join(rc_codex.CODEX_HOME, "auth.json"))
    if "Successfully logged in" in txt or (rc == 0 and signed):
        state = "ok"
    elif rc not in (None, 0) or (p is None and not code):
        state = "failed"
    else:
        state = "waiting"
    err = ""
    if state == "failed":
        m = re.findall(r"(?i)error[^\n]*", txt)
        err = (m[-1] if m else "the sign-in stopped")[:160]
    return {"state": state, "url": url, "code": code, "error": err}


def act_codex_login_state(target=None):
    return codex_login_state()


def act_set_effort(target):
    return _set_opt(target, "effort", EFFORTS + ("ultra",))


def act_bulk_retire(prefs):
    return _bulk_flag(prefs, True)


def act_bulk_unretire(prefs):
    return _bulk_flag(prefs, False)


def act_bulk_kill(prefs):
    """Stop every selected session that is running and take the whole selection
    out of the queue — same meaning as the per-row Убить."""
    good, bad = _resolve_many(prefs)
    rows = ps_rows()
    killed = []
    for uuid in good:
        pid = pid_for(uuid, rows)
        if pid is not None:
            kill_tree(pid)
            killed.append(uuid)

    def edit():
        reg = G.load(G.REG_PATH, {})
        n = 0
        for uuid in good:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec:
                continue
            rec["state"] = "dead"
            rec["retired"] = True
            n += 1
        if n:
            G.save(G.REG_PATH, reg)
        return n
    n = with_lock(edit)
    return ("processes stopped: %d · retired: %d%s"
            % (len(killed), n, _tail(bad)))


def act_bulk_up(prefs):
    """Bulk «Поднять» — the per-row act_up over a whole selection.

    Not a loop over act_up(): that one waits up to 30 s for the app link to
    appear, so N sessions in a row would hold the request for minutes and take
    the guardian lock N times. Here one locked pass launches everything, then a
    single 30 s window collects whatever links showed up, then one locked pass
    stamps them.

    Like act_up this deliberately bypasses the cap — a hand-restore is exactly
    the case the cap should not veto — so the reply reports the overshoot
    instead of hiding it.
    """
    good, bad = _resolve_many(prefs)
    rows = ps_rows()
    already = [u for u in good if pid_for(u, rows) is not None]
    todo = [u for u in good if pid_for(u, rows) is None]

    failed = []

    def edit():
        reg = G.load(G.REG_PATH, {})
        logs = {}
        for uuid in todo:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec:
                failed.append(uuid)
                continue
            try:
                rec["retired"] = False
                logs[uuid] = G.launch(uuid, rec.get("name") or uuid[:8],
                                      *G.launch_opts(uuid, rec))
                rec["state"] = "restored"
                rec["restores"] = rec.get("restores", 0) + 1
                rec["last_restore"] = G.now()
            except Exception:
                # one bad row must not cost the whole selection its edits
                failed.append(uuid)
        if logs:
            G.save(G.REG_PATH, reg)
        return logs
    logs = with_lock(edit)

    links = {}
    for _ in range(10):
        if len(links) == len(logs):
            break
        time.sleep(3)
        for uuid, logf in logs.items():
            if uuid in links:
                continue
            link = G.read_link(logf)
            if link:
                links[uuid] = link

    if links:
        def stamp():
            reg = G.load(G.REG_PATH, {})
            for uuid, link in links.items():
                rec = reg.get("sessions", {}).get(uuid)
                if rec:
                    rec["app_session"] = link
            G.save(G.REG_PATH, reg)
        with_lock(stamp)

    parts = ["launched: %d of %d" % (len(logs), len(good))]
    if already:
        parts.append("already live: %d" % len(already))
    if failed:
        parts.append("failed: %d" % len(failed))
    waiting = len(logs) - len(links)
    if waiting:
        parts.append("waiting for links: %d" % waiting)
    try:
        meta = snapshot()["meta"]
        if meta["live_total"] > meta["cap"]:
            parts.append("%d live at a cap of %d — the guardian will not launch more"
                         % (meta["live_total"], meta["cap"]))
    except Exception:
        pass
    return " · ".join(parts) + _tail(bad)


CLEAR_BACKSPACES = 400   # чем чистим поле ввода перед набором

# Потолок одного сообщения. Стоял 8000 и был взят с запасом ещё до _write_all:
# тогда длинный текст резался на полуслове, и низкий предел прятал это. Замерено
# 08.09 на живой сессии через тот же канал: 18 636 и 40 523 символа доезжают
# ЦЕЛИКОМ, все маркеры на месте, запись занимает 1,6 с. Отказ должен быть явным,
# поэтому предел остаётся — просто честный.
# ⚠️ В ленте панели сообщение всё равно показывается первыми TX_CHARS знаками с
# пометкой «[обрезано]»: это только вид, сессия получает текст целиком.
SAY_CHARS_MAX = 40000


def _write_all(fd, data):
    """Дописать в канал ЦЕЛИКОМ.

    Канал открыт неблокирующим, и os.write может взять только часть, если TUI
    не успевает читать. Раньше возвращаемое значение игнорировалось, и остаток
    молча терялся — на коротких строках это не всплывало, а многострочное
    сообщение обрезалось бы на середине.
    """
    sent = 0
    deadline = time.time() + 10.0
    while sent < len(data):
        try:
            sent += os.write(fd, data[sent:])
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if time.time() > deadline:
                raise ValueError("input channel is not draining")
            time.sleep(0.05)
    return sent


def ask_open(uuid):
    """Висит ли на экране вопрос Claude — то есть меню, а не поле ввода.

    ⛔ Это не косметика. Пока открыт вопрос, TUI показывает МЕНЮ, и `\r`,
    которым act_say заканчивает отправку, выбирает подсвеченный пункт. 26-08
    три сообщения подряд, посланные при открытом вопросе, не дошли ни одним
    словом, зато проставили выбор в первом вопросе за человека — на экране
    осталось «☒ Подход» при том, что он ничего не выбирал.
    """
    try:
        path = _tx_path(uuid)
        recs = _tail_records(path, os.path.getsize(path))
    except OSError:
        return False
    pend = set()
    for rec in recs:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == ASK_TOOL:
                pend.add(b.get("id"))
            elif b.get("type") == "tool_result":
                pend.discard(b.get("tool_use_id"))
    return bool(pend)


def _prompt_refusal(uuid, pr, typed=False):
    """Why a send did not go out, when a dialog holds the session's screen."""
    who, what = uuid[:8], (pr.get("summary") or "")[:160]
    if pr["kind"] == "permission":
        head = "%s is waiting for a permission answer: «%s»" % (who, what)
    else:
        head = "%s has a menu open on its screen: «%s»" % (who, what)
    if typed:
        return (head + " — it came up while your text was being typed. Enter was "
                "not pressed, so nothing was answered and the message was not "
                "sent. Answer the prompt in the panel (above the message box), "
                "then send again.")
    return (head + ". Nothing was typed: keystrokes would have answered it. "
            "Answer it in the panel (it is shown above the message box with its "
            "options), then send again.")


def act_say(target):
    """Написать в живую сессию: текст уходит в её stdin, как набранный руками.

    Канал — FIFO из G.launch(). Сессии, поднятые до его появления, читают stdin
    из старой подоболочки, и дописать туда снаружи нельзя ничем — им нужен
    перезапуск, о чём и говорит ошибка.
    """
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, text}")
    uuid = resolve(target.get("id") or "")
    _STOPPED.pop(uuid, None)
    text = (target.get("text") or "").strip()
    if not text:
        raise ValueError("empty message")
    if len(text) > SAY_CHARS_MAX:
        raise ValueError("too long: %d characters, the limit is %d"
                         % (len(text), SAY_CHARS_MAX))
    # Живость проверяет сам FIFO: открыть его на запись можно, только если на
    # том конце кто-то читает (иначе ENXIO). pid_for здесь не годится — он ищет
    # uuid в argv, а у сессии, созданной из панели, его там нет: она поднята без
    # --resume.
    age = _tx_age(uuid)
    fifo = G.fifo_path(uuid)
    raised = False
    if _channel_ready(fifo):
        # Печатать в модалку бессмысленно: она съест текст и вернёт «not
        # confirmed» без объяснения. Снимаем её ДО того, как набирать.
        _dismiss_noise_dialog(uuid, fifo)
    if not _channel_ready(fifo):
        # Писать можно в любую сессию, включая снятую: если канала нет или его
        # некому читать, поднимаем её и ждём готовности. Единственный случай,
        # когда это не сработает, — сессия, поднятая до появления каналов: у неё
        # stdin в старой подоболочке, и перезапуск неизбежен.
        if pid_for(uuid) is not None:
            raise ValueError("%s was launched before input channels existed — press "
                             "Queue, it relaunches and starts accepting "
                             "messages" % uuid[:8])
        act_up(uuid)
        raised = True
        for _ in range(30):
            if _channel_ready(fifo):
                break
            time.sleep(1.0)
        else:
            raise ValueError("%s launched but its input channel never opened — "
                             "try again in a minute" % uuid[:8])
        time.sleep(2.0)      # дать TUI дорисовать первый экран
        age = None
    # ⛔ 10-25: a dialog on the screen takes the keystrokes meant for the input
    # field, and the closing \r answers it (see «the screen» above: messages
    # lost, `rm` commands approved for the owner). Type nothing; the chat shows the
    # prompt with its options, and the text goes back into the box.
    prompt = open_prompt(uuid)
    if prompt:
        raise ValueError(_prompt_refusal(uuid, prompt))
    # ⛔ Перевод строки в поле ввода TUI означает «отправить». Раньше текст
    # поэтому схлопывался в один абзац — и сообщение, написанное пунктами,
    # приезжало сплошняком. Отправляем его как ВСТАВКУ: между \x1b[200~ и
    # \x1b[201~ переводы строк остаются переводами строк. Claude Code 2.1.245
    # такой ввод понимает — в сборке есть и 200~, и 201~.
    # ⛔ Пока открыт вопрос, на экране меню, а не поле ввода: забои сдвигают
    # выбор, а завершающий \r выбирает подсвеченный пункт. Esc закрывает меню
    # (так и написано в подсказке TUI: «Esc to cancel»), и дальше всё идёт как
    # с обычным полем. Человек, который вместо ответа вариантом пишет своими
    # словами, ровно этого и хочет — отменить выбор и сказать текстом.
    asked = ask_open(uuid)
    if asked:
        try:
            fd0 = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd0, b"\x1b")
            finally:
                os.close(fd0)
            time.sleep(0.6)
        except OSError:
            asked = False        # не вышло — отправляем как обычно
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    # ESC внутри оборвал бы вставку раньше времени, вместе с ней и сообщение
    body = body.replace("\x1b", "")
    body = _scrub_invisible(body)
    multiline = "\n" in body
    # Для подтверждения и для ответа человеку нужна плоская форма: в переписке
    # текст лежит как есть, а сверка идёт по схлопнутому.
    line = " ".join(body.split())
    # for the frozen-or-busy verdict below: CPU the process has used so far
    pid0 = pid_for(uuid)
    cpu0 = _cpu_seconds(pid0)
    scr = log_for(uuid)
    scr0 = _log_size(scr)
    # Codex: bracketed paste and Enter a second later (see _codex_tui)
    codex = _codex_tui(scr, uuid)
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("%s is not accepting input (%s) — not running, or no "
                         "longer reading stdin" % (uuid[:8], exc.strerror or exc))
    try:
        # ⛔ Порядок и символы проверены на живой сессии 08-23, менять вслепую
        # не стоит:
        #  1. забой, а не ctrl-U: ctrl-U поле не чистит, текст просто склеился
        #     с прежним. В поле почти всегда висит «1» — ответ на диалог
        #     возобновления из launch(), который так и не отправился;
        #  2. \r, а не \n: Enter в сыром режиме — это возврат каретки. С \n
        #     текст доезжает до поля ввода и остаётся в нём несобранным.
        os.write(fd, b"\x7f" * CLEAR_BACKSPACES)
        time.sleep(0.4)
        if multiline or codex:
            _write_all(fd, b"\x1b[200~" + body.encode("utf-8") + b"\x1b[201~")
        else:
            # Однострочный путь оставлен как был: он проверен на живых сессиях,
            # и заворачивать его во вставку незачем.
            _write_all(fd, body.encode("utf-8"))
        time.sleep(1.2 if codex else 0.5 if multiline else 0.2)
        # once more right before Enter: a prompt that came up while we typed
        # must not get it
        late = open_prompt(uuid)
        if late:
            raise ValueError(_prompt_refusal(uuid, late, typed=True))
        os.write(fd, b"\r")
    except OSError as exc:
        raise ValueError("write to %s failed: %s" % (uuid[:8], exc.strerror or exc))
    finally:
        os.close(fd)
    # the delivery check below waits for the transcript; meanwhile, if the TUI
    # stopped for a «review and press Enter», this presses it
    threading.Thread(target=_confirm_review, args=(fifo, scr, scr0), daemon=True).start()
    # Занятая сессия кладёт ввод в очередь TUI и разберёт его после текущей
    # работы. Молчать об этом нельзя: 08-23 автокомпакт задержал доставку на
    # три минуты, и «✓ отправлено» выглядело как потерянное сообщение.
    # Ввод состоялся при любом из трёх исходов ниже — значит описание в табе
    # уже устарело, и пересказ надо снять заново, когда сессия отработает.
    mark_summary_stale(uuid)
    note_sent(uuid)
    wrote_at = time.time()
    who = uuid[:8]
    lead = "%s launched, " % who if raised else ""
    if asked:
        lead += "question dismissed, "
    if _delivered(uuid, line):
        return "%sdelivered to %s: %s" % (lead, who, line[:70])
    if age is not None and age < BUSY_S:
        return ("%squeued for %s — the session is busy (wrote %ds ago), "
                "it will pick this up after the current work" % (lead, who, age))
    # ⛔ Не утверждать причину. 24-08 эта строка обвиняла переполненный контекст
    # у сессии с 33% занятости, а сообщение просто доехало позже окна — машина
    # была в свопе. Сообщаем факт и оставляем вывод человеку.
    # Словарь, а не строка: панели нужно ОТЛИЧИТЬ этот случай, чтобы не дать
    # тексту пропасть. 25-08 сессия ровно в этот момент мигрировала канал и
    # выбросила набранное — поле в панели к тому времени уже очистилось, и
    # второй копии сообщения у человека не осталось.
    # Повисшую сессию отличаем ФАКТОМ, а не догадкой: её экран не выдал ни
    # байта с самой отправки. 13.09 такая сессия 22 минуты держала два зомби,
    # не тратила процессор и глотала всё, что в неё писали, а панель повторяла
    # «может ещё доедет» — человек ждал напрасно. Канал при этом исправен:
    # `cat` разгребает FIFO, просто приложение за ним не читает.
    silent = _screen_silent_for(uuid, wrote_at)
    if silent is not None and silent > SAY_CONFIRM_S:
        # ⛔ 09-25: this used to end here with "restart it by hand", and the
        # message was lost with the frozen process. The verdict is a fact (not
        # one byte on its screen since the write), so act on it: restart the
        # session (--resume keeps the history) and send the same text again.
        # Once: a session that freezes again right away is reported, not looped.
        pid = pid_for(uuid)
        # ⛔ 09-25: a silent screen log alone restarted c20be4ba mid-turn — its
        # old wrapper buffered the screen (no `script -F`), so "nothing on
        # screen" was not a fact, and a busy session lost its turn and two
        # background tasks. Act only on hard evidence of a freeze.
        frozen = _looks_frozen(pid, cpu0 if pid == pid0 else None)
        if pid is not None and not frozen:
            return {"msg": ("%s%s has not shown the message yet, but it is alive "
                            "(it kept using CPU while we waited): most likely in the "
                            "middle of a long step. The text is in its input. If "
                            "nothing happens, press ■ Stop, or restart it."
                            % (lead, who)),
                    "unconfirmed": True}
        if pid is not None and not target.get("_healed"):
            # First try to wake it without a restart: a claude blocked opening
            # a stray pipe runs on the moment the pipe is opened from the
            # other side (09-28, b28ee05e). Then the message it was sent is
            # still in its input, and it answers it by itself.
            moved, woke = G.try_unfreeze(pid)
            if moved:
                return {"msg": ("%s%s was frozen on a system file (%s) — released it, "
                                "the session runs again and has your message"
                                % (lead, who, ", ".join(os.path.basename(w) for w in woke)
                                   or "unknown")),
                        "unconfirmed": True}
            kill_tree(pid)
            # ⛔ The old wrapper keeps the channel open for a moment after claude
            # dies (script exits → bash `kill 0` → cat). A resend in that window
            # "found" a live channel and wrote into it: Broken pipe (09-25 test).
            # Wait for it to close, so the resend takes the launch path.
            for _ in range(40):
                if not _channel_ready(fifo):
                    break
                time.sleep(0.25)
            again = dict(target, _healed=True)
            note = ("%s was frozen (nothing on its screen for %d min) — restarted "
                    "it, history kept; " % (who, max(1, int(silent // 60))))
            try:
                res = act_say(again)
            except Exception as exc:
                raise ValueError(note + "but the resend failed: %s" % exc)
            if isinstance(res, dict):
                res = dict(res, msg=note + res.get("msg", ""))
                return res
            return note + res
        return {"msg": ("%s%s is not reading its input: nothing has appeared on "
                        "its screen for %d min, the write went nowhere. Restart "
                        "it — Queue, then Revive; the history is kept."
                        % (lead, who, int(silent // 60))),
                "unconfirmed": True, "wedged": True}
    return {"msg": ("%s%s accepted the write, but the message had not appeared "
                    "in the transcript after %ds — it may still land. If it "
                    "does not: check the context bar in the row, and /compact "
                    "if it is full." % (lead, who, int(SAY_CONFIRM_S))),
            "unconfirmed": True}


UPLOAD_DIR = CFG.state("rc-uploads")
# 11-17: 25 -> 200 MB. A 5-minute screen recording is 25-30 MB; the panel
# refused it before reading the body and the browser showed that as
# «connection lost». Keep UP_MAX_MB in rc_panel.py's JS equal to this.
UPLOAD_MAX = 200 * 1024 * 1024


def save_upload(pref, name, blob):
    """Положить файл рядом с сессией и вернуть путь.

    Отправить картинку «в чат» через FIFO нельзя — туда уходят нажатия клавиш.
    Зато Claude Code читает файлы с диска, так что кладём файл на мини и даём
    сессии путь: дальше она сама его откроет своим Read.
    """
    # Черновик новой сессии: uuid ещё нет, а вложить файл уже надо — иначе
    # приходится сперва отправить пустой промпт, потом прикреплять. Путь
    # возвращаем абсолютный, поэтому после создания сессии он остаётся
    # рабочим и переносить файл никуда не нужно.
    uuid = resolve(pref) if pref else ""
    base = os.path.basename(name or "").strip().replace("\x00", "")
    base = re.sub(r"[^\w.\- ]+", "_", base, flags=re.UNICODE)[:120] or "file"
    if base.startswith("."):
        base = "_" + base
    if len(blob) > UPLOAD_MAX:
        raise ValueError("file too large: %.1f MB, cap %d MB"
                         % (len(blob) / 1048576.0, UPLOAD_MAX // 1048576))
    if not blob:
        raise ValueError("empty file")
    d = os.path.join(UPLOAD_DIR, uuid[:8] if uuid else "draft")
    try:
        os.makedirs(d)
    except OSError:
        pass
    dest = os.path.join(d, base)
    stem, ext = os.path.splitext(base)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(d, "%s-%d%s" % (stem, n, ext))
        n += 1
    # ⛔ 10-16: written whole or not at all. A .part next to it until the last
    # byte is on disk, so a cut connection never leaves a half file behind
    # under the real name (10-15: «_1_.zip» saved at exactly 1 MiB of 1.9).
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, dest)
    return dest


SAY_CONFIRM_S = 25.0     # сколько ждём, пока реплика проявится в переписке
# ⛔ Компакт не роняем в «not confirmed». Во время «Compacting conversation»
# TUI держит набранное в очереди и отдаёт его в переписку ТОЛЬКО после себя, а
# компакт длиннее обычного окна (25 c). Пока он идёт — не засчитываем это время
# против окна, иначе доехавшее сообщение объявлялось неподтверждённым и человек
# вручную возвращал его в работу кнопкой. Держим суммарный потолок ниже таймаута
# прокси между машинами (remote_post=120 c), чтобы act через панель-посредник
# успел вернуть ответ, а не оборвался.
SAY_COMPACT_CAP_S = 100.0


def _screen_silent_for(uuid, since):
    """Сколько секунд экран сессии не издал НИ БАЙТА после отметки `since`.

    Отличает «занята и ответит позже» от «повисла»: занятая сессия перерисовывает
    спиннер и лог растёт, а повисшая не пишет вовсе. None — экран не читается,
    тогда судить не о чем.
    """
    log = log_for(uuid)
    if not log:
        return None
    try:
        return max(0.0, since - os.stat(log).st_mtime)
    except OSError:
        return None


def _is_compacting(uuid):
    """Идёт ли прямо сейчас /compact в этой сессии (по экрану TUI)."""
    try:
        return tui_status(log_for(uuid)).get("kind") == "compacting"
    except Exception:
        return False


def _delivered(uuid, line):
    """Дождаться, что реплика ДЕЙСТВИТЕЛЬНО попала в переписку.

    Раньше act_say докладывал об удачной записи в FIFO и называл это
    «отправлено». Запись удаётся всегда, пока канал кто-то читает, — а сессия с
    переполненным контекстом ввод не принимает. 24-08 так и вышло: панель на
    каждое сообщение отвечала бодрым «отправлено», а в сессию не доходило
    ничего. Единственное честное подтверждение — сама переписка.

    Компакт — особый случай: он держит ввод в очереди и отдаёт его позже, чем
    базовое окно. Пока идёт компакт, продлеваем ожидание (до SAY_COMPACT_CAP_S),
    чтобы дождаться доставки, а не отдавать сообщение в «not confirmed».
    """
    path = _tx_path(uuid)
    probe = " ".join(line.split())[:40]
    if os.path.isdir(path):
        # a Mistral Vibe session folder (rc_vibe). The text was typed just
        # before this call, so a user entry from the last minute with the same
        # words is this message.
        import rc_vibe
        since = time.time() - 60
        end = time.time() + SAY_CONFIRM_S
        while time.time() < end:
            for m in rc_vibe.session_rows(path):
                if (m.get("role") == "user" and (m.get("tse") or 0) >= since
                        and probe in " ".join((m.get("text") or "").split())):
                    return True
            time.sleep(1.0)
        return False
    try:
        start = os.path.getsize(path)
    except OSError:
        return False
    hard_end = time.time() + SAY_COMPACT_CAP_S
    end = time.time() + SAY_CONFIRM_S
    while time.time() < end and time.time() < hard_end:
        time.sleep(1.0)
        # Компакт держит очередь ввода нераскрытой. Не даём окну истечь, пока он
        # виден на экране: после его конца остаётся ещё SAY_CONFIRM_S на то, чтоб
        # отложенный ввод проявился записью user/queue-operation.
        if _is_compacting(uuid):
            end = min(time.time() + SAY_CONFIRM_S, hard_end)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= start:
            continue
        if "/.codex/sessions/" in path:
            # a Codex thread (rc_codex): the user message is a response_item
            import rc_codex
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    blob = fh.read().decode("utf-8", "ignore")
            except OSError:
                continue
            for ln in blob.splitlines():
                try:
                    p = (json.loads(ln) or {}).get("payload") or {}
                except ValueError:
                    continue
                if p.get("type") == "message" and p.get("role") == "user":
                    if probe in " ".join(rc_codex._texts(p.get("content")).split()):
                        return True
            continue
        for rec in _tail_records(path, size):
            # Занятая сессия кладёт ввод в очередь TUI и пишет его типом
            # queue-operation, а записи "user" не появляется вовсе. Без этой
            # ветки доставка в работающую сессию НИКОГДА не подтверждалась, и
            # панель отвечала уклончивым «принято, но в переписке не видно».
            if rec.get("type") == "queue-operation":
                if rec.get("operation") != "enqueue":
                    continue
                q = " ".join(str(rec.get("content") or "").split())
                if q and probe in q:
                    return True
                continue
            if rec.get("type") != "user" or _is_meta(rec):
                continue
            txt = _msg_text(rec)
            if txt and probe in txt:
                return True
    return False


def act_set_title(target):
    """Своё имя таба. Пустое значение — вернуть автоматическое."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {ids, value}")
    value = " ".join((target.get("value") or "").split())[:40]
    good, bad = _resolve_many(target.get("ids"))

    def edit():
        reg = G.load(G.REG_PATH, {})
        n = 0
        for uuid in good:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec:
                continue
            if value:
                rec["title_manual"] = value
            else:
                rec.pop("title_manual", None)
            n += 1
        if n:
            G.save(G.REG_PATH, reg)
        return n
    n = with_lock(edit)
    if not n:
        # «0 renamed» with ok=true read as done on a page that never re-checks
        raise KeyError("no such session: %s" % ", ".join(bad or ["?"]))
    what = ("renamed to «%s»" % value) if value else "name back to automatic"
    out = "%d %s" % (n, what)
    if bad:
        out += " (not found: %s)" % ", ".join(bad)
    return out


def act_set_project(target):
    """Переложить сессии в другую группу руками."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {ids, value}")
    prefs = target.get("ids")
    value = (target.get("value") or "").strip()[:60]
    good, bad = _resolve_many(prefs)

    def edit():
        reg = G.load(G.REG_PATH, {})
        n = 0
        for uuid in good:
            rec = reg.get("sessions", {}).get(uuid)
            if not rec:
                continue
            if value and value != NO_PROJECT:
                rec["project_manual"] = value
            else:
                rec.pop("project_manual", None)
            n += 1
        if n:
            G.save(G.REG_PATH, reg)
        return n

    n = with_lock(edit)
    where = value or NO_PROJECT
    msg = "moved to «%s»: %d" % (where, n)
    if bad:
        msg += " · not found: %d" % len(bad)
    return msg


def act_answer(target):
    """Ответить на вопрос Claude — нажать пункт его меню.

    В меню TUI цифра выбирает пункт СРАЗУ, без Enter. Подсказка внизу обещает
    только «Enter to select · ↑/↓ to navigate», про цифры молчит, поэтому
    проверено на одноразовой сессии 24-08: «2» выбрала второй вариант.
    Отрисовка при этом отстала на несколько секунд — машина была в свопе, — так
    что по свежему снимку экрана легко решить, что клавиша не сработала.

    ⛔ Забои сюда слать нельзя: это меню, а не поле ввода. `act_say` чистит поле
    четырьмя сотнями `\x7f` — здесь это сдвинуло бы выбор куда попало.
    """
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, choice}")
    uuid = resolve(target.get("id") or "")
    # Вопрос с несколькими ответами TUI подтверждает по Enter, поэтому здесь
    # принимается СПИСОК номеров. Без этого такой вопрос из панели закрыть было
    # нельзя вовсе: цифра только переключала галочку, а подтвердить нечем.
    raw = target.get("choice")
    picks = []
    for v in (raw if isinstance(raw, (list, tuple)) else [raw]):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("expected an option number")
        if not 1 <= n <= 9:
            raise ValueError("option number outside 1..9")
        picks.append(n)
    if not picks:
        raise ValueError("expected an option number")
    fifo = G.fifo_path(uuid)
    if not _channel_ready(fifo):
        raise ValueError("%s is not accepting input — no channel, press Restart"
                         % uuid[:8])
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("%s is not accepting input (%s)"
                         % (uuid[:8], exc.strerror or exc))
    try:
        for i, n in enumerate(picks):
            if i:
                time.sleep(0.25)
            os.write(fd, str(n).encode("ascii"))
        # ⛔ Enter — ТОЛЬКО для вопроса с несколькими ответами. В обычном меню
        # цифра выбирает сразу, и лишний Enter уехал бы в следующий вопрос
        # ответом наугад.
        if target.get("enter"):
            time.sleep(0.3)
            os.write(fd, b"\r")
    finally:
        os.close(fd)
    # Ответ на вопрос — тоже «я эту сессию только что трогал»: в колонке она
    # должна подняться сразу, а не когда Claude домелет следующий шаг.
    note_sent(uuid)
    mark_summary_stale(uuid)
    what = ", ".join(str(n) for n in picks)
    return ("%s — option%s %s picked, the answer lands in a couple of seconds"
            % (uuid[:8], "s" if len(picks) > 1 else "", what))


PERMIT_CONFIRM_S = 6.0


def act_permit(target):
    """Pick an option of the dialog on a session's screen: the panel's answer
    to a permission prompt (see «the screen»). A digit picks at once, as in
    act_answer. `sig` is the prompt the person was looking at: if the screen
    shows another one by now (the agent moved on, a new prompt came up),
    nothing is pressed, so a «Yes» meant for one command cannot approve the
    next one."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, choice, sig}")
    uuid = resolve(target.get("id") or "")
    try:
        n = int(target.get("choice"))
    except (TypeError, ValueError):
        raise ValueError("expected an option number")
    rows = ps_rows()
    log = log_for(uuid, rows)
    pr = screen_prompt(screen_lines(log, rows)) if log else None
    if not pr:
        raise ValueError("%s has no prompt open any more — it was answered or "
                         "went away" % uuid[:8])
    if target.get("sig") and target.get("sig") != pr["sig"]:
        raise ValueError("%s shows a different prompt now — nothing was pressed, "
                         "look at it again" % uuid[:8])
    labels = dict((o["n"], o["label"]) for o in pr["options"])
    if n not in labels:
        raise ValueError("option %d is not on that prompt" % n)
    fifo = G.fifo_path(uuid)
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("%s is not accepting input (%s)"
                         % (uuid[:8], exc.strerror or exc))
    try:
        os.write(fd, str(n).encode("ascii"))
    finally:
        os.close(fd)
    note_sent(uuid)
    mark_summary_stale(uuid)
    end = time.time() + PERMIT_CONFIRM_S
    while time.time() < end:
        time.sleep(0.4)
        now = screen_prompt(screen_lines(log))
        if not now or now["sig"] != pr["sig"]:
            return "%s — «%s» picked" % (uuid[:8], labels[n])
    return {"msg": "%s — pressed %d («%s»), but the prompt is still on its screen "
                   "after %ds" % (uuid[:8], n, labels[n], int(PERMIT_CONFIRM_S)),
            "unconfirmed": True}


# ---------------------------------------------------------------- голос
#
# Распознаём на своей машине, а не в браузере. Браузерный SpeechRecognition
# шлёт звук наружу (Chrome в Google, Safari в Apple) и на смеси русского с
# английскими терминами ошибается так, что текст проще надиктовать заново.
# Whisper с моделью покрупнее плюс правка терминов дешёвой моделью дают текст,
# который не приходится переписывать.
#
# ⚠️ Модель грузится на КАЖДЫЙ вызов: и CLI, и чужой сервер на 8787 просто
# запускают whisper заново. Отсюда потолок по размеру — большая модель
# добавляет к каждой фразе секунды только на загрузку.

def _cpu_seconds(pid):
    """CPU time the process has used, in seconds; None if it is gone."""
    if not pid:
        return None
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    days = 0
    if "-" in out:
        d, out = out.split("-", 1)
        days = int(d) if d.isdigit() else 0
    parts = out.split(":")
    try:
        sec = float(parts[-1])
        mins = int(parts[-2]) if len(parts) > 1 else 0
        hrs = int(parts[-3]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return days * 86400 + hrs * 3600 + mins * 60 + sec


def _looks_frozen(pid, cpu_before):
    """Hard evidence of a frozen claude: a child it has not reaped (its event
    loop is not running — d3e26fda, 546d9eb4), or not a hundredth of a second
    of CPU across the whole confirmation wait (a live TUI redraws and uses
    some). A silent screen log is NOT evidence: old wrappers buffer it."""
    if pid is None:
        return False
    if any(pp == str(pid) and st.startswith("Z") for _p, pp, st, _a in G.ps_tree()):
        return True
    now = _cpu_seconds(pid)
    return cpu_before is not None and now is not None and now - cpu_before < 0.05


INTERRUPT_CONFIRM_S = 8.0


def act_interrupt(target):
    """Остановить текущий шаг работающей сессии — Esc, как в терминале.

    Один Esc, и только один: второй подряд открывает в TUI меню отката к
    прошлым репликам. Проверено 25-09 на одноразовой сессии: Esc посреди
    `ping -c 90` снял команду, и в переписке появилась запись
    "[Request interrupted by user for tool use]". По ней и подтверждаем —
    экран (pty-лог) обновляется слишком лениво, чтобы по нему судить.

    Без общего лока действий (см. QUICK_ACTIONS в панели): стоп нужен сейчас,
    а не после того, как допишется отправка в эту же сессию.
    """
    pref = target.get("id") if isinstance(target, dict) else target
    uuid = resolve(pref or "")
    fifo = G.fifo_path(uuid)
    if not _channel_ready(fifo):
        raise ValueError("%s is not running — nothing to stop" % uuid[:8])
    path = _tx_path(uuid)
    try:
        start = os.path.getsize(path)
    except OSError:
        start = None
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("%s is not accepting input (%s)" % (uuid[:8], exc.strerror or exc))
    try:
        os.write(fd, b"\x1b")
    finally:
        os.close(fd)
    end = time.time() + INTERRUPT_CONFIRM_S
    while start is not None and time.time() < end:
        time.sleep(0.4)
        try:
            size = os.path.getsize(path)
        except OSError:
            break
        if size <= start:
            continue
        with open(path, "rb") as fh:
            fh.seek(start)
            if INTERRUPT_MARK.encode() in fh.read():
                _STOPPED[uuid] = time.time()
                mark_summary_stale(uuid)
                return ("%s stopped — the step was interrupted; it waits for your "
                        "next message" % uuid[:8])
    return ("%s — Esc sent, but no stop was recorded within %ds: it was probably "
            "between steps, or is compacting" % (uuid[:8], int(INTERRUPT_CONFIRM_S)))


VOICE_DIR = CFG.state("rc-voice")
def _tool(name, mac_path):
    """Путь к бинарю: на macOS — homebrew, как и было; иначе что найдётся в
    PATH. Нет нигде — остаётся homebrew-путь, и ошибка «нет whisper» читается
    в логе так же, как раньше."""
    if os.path.exists(mac_path):
        return mac_path
    return shutil.which(name) or mac_path


WHISPER_BIN = _tool("whisper", "/opt/homebrew/bin/whisper")
# ⛔ 10-01: the main Mac never had the Python `whisper`, only Homebrew's
# whisper.cpp (`whisper-cli`), so every voice note failed with "whisper is not
# installed". whisper.cpp needs a ggml model file; large-v3-turbo q5_0 (574 MB)
# is far better at Russian than `small` and fast on Apple Silicon (Metal).
WHISPER_CPP_BIN = _tool("whisper-cli", "/opt/homebrew/bin/whisper-cli")
WHISPER_CPP_MODEL = os.environ.get("RC_WHISPER_CPP_MODEL") or os.path.expanduser(
    "~/.claude/rc-voice-models/ggml-large-v3-turbo-q5_0.bin")
FFMPEG_BIN = _tool("ffmpeg", "/opt/homebrew/bin/ffmpeg")
VOICE_MODEL = os.environ.get("RC_VOICE_MODEL", "small")
VOICE_LANG = "ru"
VOICE_MAX = 25 * 1024 * 1024
# ⛔ 10-14: a long note came out as «Субтитры создавал DimaTorzok» and was gone:
# whisper answers SILENCE with subtitle credits from its training data, and the
# source audio was deleted right after. Now every recording is kept for a week,
# and silence (or the credits line) is an error, not text.
VOICE_KEEP_DIR = os.path.join(VOICE_DIR, "keep")
VOICE_KEEP_DAYS = 7
VOICE_SILENT_DB = -50.0
VOICE_CREDITS_RE = re.compile(
    u"(субтитр|корректор\\s|DimaTorzok|Семкин|Егорова|Синецкая)", re.I)

VOICE_FIX_PROMPT = (
    "Below is an automatic transcript of a developer's voice message. It has "
    "recognition errors.\n"
    "Fix ONLY those: names of tools and services (Vercel, Supabase, Claude, "
    "Telegram, tailscale, systemd and the like), technical terms, punctuation "
    "and sentence breaks. Keep the language of the transcript.\n"
    "Do not add, shorten or paraphrase anything. Keep the meaning and the "
    "order of thoughts. Return ONLY the fixed text, no preamble, no quotes."
    "\n\n")


def _voice_polish(text):
    """Правка распознанного дешёвой моделью. Не удалась — отдаём как есть:
    сырая расшифровка всё равно лучше пустоты."""
    if not text.strip():
        return text
    try:
        os.makedirs(SUM_DIR)
    except OSError:
        pass
    try:
        # --effort low тут пробовали: на замере вышло 34 с против 26 — не
        # быстрее, а медленнее. Оставлен эффорт по умолчанию.
        r = helper_run(VOICE_FIX_PROMPT + text, SUM_MODEL, 90)
    except Exception:
        return text
    # Модель иногда обрамляет ответ разделителем или кавычками — в текст
    # сообщения это попадать не должно (поймано на «--- Проверь пожалуйста…»).
    drop = ("EN:", "---", "***", "```", "___")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if not any(ln.startswith(d) for d in drop) and set(ln) - set("-*_ ")]
    out = " ".join(" ".join(lines).split()).strip().strip('"').strip()
    # Защита от переусердствовавшей модели: если она вернула что-то втрое
    # длиннее или почти пустое — это уже не правка, а сочинение.
    bad = ""
    if not out:
        bad = "пусто"
    elif len(out) > max(400, len(text) * 3):
        bad = "слишком длинно (%d при исходных %d)" % (len(out), len(text))
    elif len(out) < len(text) * 0.4:
        bad = "слишком коротко (%d при исходных %d)" % (len(out), len(text))
    sys.stderr.write("[voice] rc=%s stdout=%r -> %r %s\n"
                     % (r.returncode, (r.stdout or "")[:400], out[:200],
                        ("ОТКЛОНЕНО: " + bad) if bad else "принято"))
    sys.stderr.flush()
    if bad:
        return text
    return out


def _voice_keep(src, stamp, ext):
    """Copy of every recording for VOICE_KEEP_DAYS, so a failed or silent
    transcription never loses what was said. Older copies are pruned here."""
    try:
        os.makedirs(VOICE_KEEP_DIR, exist_ok=True)
        cutoff = time.time() - VOICE_KEEP_DAYS * 86400
        for fn in os.listdir(VOICE_KEEP_DIR):
            fp = os.path.join(VOICE_KEEP_DIR, fn)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.unlink(fp)
            except OSError:
                pass
        dst = os.path.join(VOICE_KEEP_DIR, "%s-%s%s" % (
            datetime.now().strftime("%Y%m%d-%H%M%S"), stamp[-3:], ext))
        shutil.copyfile(src, dst)
        return dst
    except Exception as exc:
        sys.stderr.write("[voice] keep failed: %s\n" % exc)
        return ""


def _voice_peak_db(wav):
    """Peak level of the decoded audio in dB, None when ffmpeg cannot tell."""
    try:
        r = subprocess.run([FFMPEG_BIN, "-hide_banner", "-nostats", "-i", wav,
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace",
                           timeout=120)
        m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr or "")
        if not m:
            return None
        return -999.0 if m.group(1) == "-inf" else float(m.group(1))
    except Exception:
        return None


def transcribe(blob, name="voice.webm"):
    if not blob:
        raise ValueError("empty recording")
    if len(blob) > VOICE_MAX:
        raise ValueError("recording too large: %.1f MB" % (len(blob) / 1048576.0))
    use_cpp = (not os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_CPP_BIN)
               and os.path.exists(WHISPER_CPP_MODEL))
    if not use_cpp and not os.path.exists(WHISPER_BIN):
        raise ValueError("voice needs whisper and ffmpeg on this computer (ask Claude Code to install openai-whisper and ffmpeg); text messages work without them")
    try:
        os.makedirs(VOICE_DIR)
    except OSError:
        pass
    stamp = "%d" % (time.time() * 1000)
    ext = os.path.splitext(name)[1][:8] or ".webm"
    src = os.path.join(VOICE_DIR, "in-%s%s" % (stamp, ext))
    # ⛔ Имя ДРУГОЕ, а не in-<stamp>.wav: если на входе уже wav, вход и выход
    # совпадут и ffmpeg откажется («same as Input»). Браузер шлёт webm, так что
    # вживую это не стреляет — ловится только прогоном готового wav.
    wav = os.path.join(VOICE_DIR, "conv-%s.wav" % stamp)
    with open(src, "wb") as fh:
        fh.write(blob)
    kept = _voice_keep(src, stamp, ext)
    # whisper хочет 16 кГц моно; браузер отдаёт webm/opus.
    r = subprocess.run([FFMPEG_BIN, "-y", "-loglevel", "error", "-i", src,
                        "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0 or not os.path.exists(wav):
        raise ValueError("could not decode the audio: %s (kept: %s)"
                         % ((r.stderr or "")[:120], kept))
    secs = max(0.0, (os.path.getsize(wav) - 44) / 32000.0)
    peak = _voice_peak_db(wav)
    sys.stderr.write("[voice] in %s: %d KB, %.1f s, peak %s dB, kept %s\n"
                     % (name, len(blob) // 1024, secs,
                        "?" if peak is None else "%.1f" % peak, kept))
    sys.stderr.flush()
    if peak is not None and peak < VOICE_SILENT_DB:
        for f in (src, wav):
            try:
                os.unlink(f)
            except OSError:
                pass
        raise ValueError("the recording is silent (%.0f s, peak %.0f dB): the "
                         "browser got no sound from the microphone. Check the "
                         "input device; over Screen Sharing the Mac mini has no mic"
                         % (secs, peak))
    out_dir = os.path.join(VOICE_DIR, "out-%s" % stamp)
    if use_cpp:
        os.makedirs(out_dir, exist_ok=True)
        r = subprocess.run([WHISPER_CPP_BIN, "-m", WHISPER_CPP_MODEL, "-l", VOICE_LANG,
                            "-f", wav, "-otxt", "-np", "-of",
                            os.path.join(out_dir, os.path.basename(wav)[:-4])],
                           capture_output=True, text=True, errors="replace", timeout=600)
    else:
        r = subprocess.run([WHISPER_BIN, wav, "--model", VOICE_MODEL,
                            "--language", VOICE_LANG, "--output_format", "txt",
                            "--output_dir", out_dir, "--fp16", "False"],
                           capture_output=True, text=True, errors="replace", timeout=600)
    txt_path = os.path.join(out_dir, os.path.basename(wav)[:-4] + ".txt")
    raw = ""
    try:
        with open(txt_path, encoding="utf-8", errors="replace") as fh:
            raw = " ".join(fh.read().split())
    except OSError:
        pass
    for f in (src, wav):
        try:
            os.unlink(f)
        except OSError:
            pass
    try:
        shutil.rmtree(out_dir)
    except Exception:
        pass
    if not raw:
        raise ValueError("nothing recognised (%s)" % (r.stderr or "")[:120])
    if len(raw) < 120 and VOICE_CREDITS_RE.search(raw):
        raise ValueError("no speech in the recording (%.0f s): whisper returned "
                         "its silence placeholder %r. Audio kept: %s"
                         % (secs, raw, kept))
    return {"raw": raw, "text": _voice_polish(raw), "kept": kept,
            "model": os.path.basename(WHISPER_CPP_MODEL) if use_cpp else VOICE_MODEL}


# ------------------------------------------------------- приём извне
#
# Одна дверь для «надиктовал на бегу — получил сессию»: часы, очки, бот. Своя
# авторизация по токену, потому что у панели её нет вообще, а этой ручкой
# когда-нибудь придётся торчать наружу.

INGEST_TOKEN_PATH = CFG.state("rc-ingest-token")

# Правка терминов для ПРОДИКТОВАННОГО ТЕКСТА выключена: она добавляет к каждому
# приёму секунд пятнадцать-тридцать, а смысл приёма в том, чтобы на ходу
# сказать и забыть. Аудио правится по-прежнему — там whisper выдаёт куда более
# сырой текст. Включается переменной окружения, без правки кода:
#     RC_INGEST_POLISH=1 в plist панели.
INGEST_POLISH = os.environ.get("RC_INGEST_POLISH", "0") == "1"


def ingest_token():
    """Токен для внешних отправителей. Создаётся сам при первом обращении."""
    try:
        with open(INGEST_TOKEN_PATH) as fh:
            t = fh.read().strip()
        if t:
            return t
    except OSError:
        pass
    import binascii
    t = binascii.hexlify(os.urandom(24)).decode("ascii")
    old_mask = os.umask(0o077)
    try:
        with open(INGEST_TOKEN_PATH, "w") as fh:
            fh.write(t)
    finally:
        os.umask(old_mask)
    return t


ROUTE_CANDIDATES = 20        # больше не влезает в короткий промпт осмысленно

ROUTE_PROMPT = (
    "You are handling a developer's dictated message.\n\n"
    "DO TWO THINGS.\n"
    "1. Fix recognition errors: names of services and tools (Vercel, "
    "Supabase, Claude, Telegram, tailscale, systemd and the like), terms, "
    "punctuation. Keep the language. Do not add or paraphrase anything; keep "
    "the meaning and the order of thoughts.\n"
    "2. Decide where it goes: start NEW work, or continue one of the "
    "sessions listed below.\n\n"
    "Count it as a continuation only on an explicit reference: the same task "
    "or project, or a request to finish what was started. When in doubt, NEW. "
    "An extra new session is a small cost; a message in the wrong session "
    "mixes up the work.\n\n"
    "Reply with ONE line of JSON, no explanation, no markdown:\n"
    '{"text":"fixed text","action":"new"|"say","session":"id or null"}\n\n')


def _route_candidates(limit=ROUTE_CANDIDATES):
    """Свежие сессии, среди которых ищем адресата."""
    reg = G.load(G.REG_PATH, {})
    sm = summaries()
    live = G.live_uuids()
    rows = []
    for uuid, rec in (reg.get("sessions") or {}).items():
        if rec.get("kind") != "rc" or rec.get("retired") or not G.has_content(rec):
            continue
        rows.append((rec.get("last_active_ts", 0), uuid, rec))
    rows.sort(reverse=True)
    out = []
    for _ts, uuid, rec in rows[:limit]:
        what = (sm.get(uuid) or {}).get("text") or rec.get("name") or ""
        out.append({"id": uuid[:8], "live": uuid in live,
                    "project": project_of(rec),
                    "what": " ".join(str(what).split())[:150]})
    return out


def _route(text):
    """Починить текст и решить, в какую сессию он идёт.

    Одним вызовом, а не двумя: правка и маршрутизация смотрят на один и тот же
    текст, а каждый лишний заход к модели — это ещё десяток секунд ожидания у
    человека, который диктовал на ходу.
    """
    cands = _route_candidates()
    listing = "\n".join(
        "%s | %s | %s | %s" % (c["id"], "live" if c["live"] else "idle",
                               c["project"], c["what"])
        for c in cands) or "(no sessions)"
    prompt = (ROUTE_PROMPT + "SESSIONS (id | state | project | what it is doing):\n"
              + listing + "\n\nMESSAGE:\n" + text)
    try:
        os.makedirs(SUM_DIR)
    except OSError:
        pass
    try:
        r = helper_run(prompt, SUM_MODEL, 120)
    except Exception:
        return None
    raw = (r.stdout or "").strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        sys.stderr.write("[route] не JSON: %r\n" % raw[:200])
        sys.stderr.flush()
        return None
    try:
        got = json.loads(raw[i:j + 1])
    except ValueError:
        sys.stderr.write("[route] JSON не разобрался: %r\n" % raw[i:j + 1][:200])
        sys.stderr.flush()
        return None
    fixed = " ".join(str(got.get("text") or "").split())
    # Та же защита, что и у правки: ответ втрое длиннее или вдвое короче — это
    # уже не починка, а сочинение.
    if not fixed or len(fixed) > max(400, len(text) * 3) or len(fixed) < len(text) * 0.4:
        fixed = text
    target = str(got.get("session") or "").strip()
    if got.get("action") != "say" or not target or target.lower() == "null":
        target = ""
    if target and not any(c["id"] == target for c in cands):
        sys.stderr.write("[route] выдуманная сессия %r — отправляю в новую\n" % target)
        sys.stderr.flush()
        target = ""
    sys.stderr.write("[route] -> %s | %r\n" % (target or "НОВАЯ", fixed[:90]))
    sys.stderr.flush()
    return {"text": fixed, "target": target}


# ---------------------------------------------------------------- continue
# 19.11 the owner: the launch bay gets a second mode, «continue the best match»: find
# the live or archived session that this request continues and send it there.
# Unlike _route (voice ingest: 20 fresh sessions, haiku, «doubt = new»), this
# one looks at EVERY session with a transcript, archived ones included, and the
# person asked for a continuation on purpose, so the bar is «the same work».
# Signals per session: title, project, last activity, the 160-char summary,
# the last thing the person said, and how many of the request's words occur in
# its conversation (rc_search index), which is what finds an old archived one
# whose summary is about its last turn, not its topic.
CONT_MODEL = "sonnet"
CONT_MAX = 400
CONT_STOP = set("""это этот эта эти того тоже также чтобы когда потом теперь
сейчас нужно надо можно давай давайте сделай сделать пожалуйста просто
только очень всех всего всем было были будет есть который которая которые
where what when this that with from have will your just please make need""".split())
CONT_PROMPT = (
    "A person is starting work in one of their Claude Code sessions and chose "
    "\"continue the matching one\": find the session where THIS work already "
    "went on (the same task, product or project, finishing it or its next "
    "step), so the request goes there.\n\n"
    "Session line: id | state (live = running, idle = stopped, archived) | "
    "project | last activity | title | what it is about | the person's last "
    "message | kw = how many words of the request occur in its conversation."
    "\n\n"
    "Rules. The topic matters more than recency, but among several sessions "
    "on the same thing take the one where this work went on last. kw is a "
    "hint, not the answer: common words occur everywhere. If no session is "
    "about this work, answer null: a new one starts.\n\n"
    "Reply with ONE line of JSON, no markdown:\n"
    '{"session":"id or null","why":"up to 12 words, in the request\'s '
    'language: why this one"}\n\n')


def _cont_terms(text):
    """Words of the request worth looking up: 4+ letters, not filler; long
    Russian words cut to a stem so «сессию» finds «сессия»."""
    out = []
    for w in re.findall(r"[\w@./-]+", (text or "").lower()):
        w = w.strip(".-/")
        if len(w) < 4 or w in CONT_STOP or w.isdigit():
            continue
        if len(w) >= 7 and re.match(r"^[а-яё]+$", w):
            w = w[:-2]
        if w not in out:
            out.append(w)
    return out[:12]


def _cont_kw(terms):
    """{uuid: how many of the terms occur in its conversation}."""
    if not terms:
        return {}
    try:
        import rc_search as S
        try:
            S.update()
        except Exception:
            pass
        c = S._conn()
    except Exception as exc:
        sys.stderr.write("[continue] search index unavailable: %r\n" % (exc,))
        return {}
    hits = {}
    try:
        for t in terms:
            for (uuid,) in c.execute(
                    "select distinct uuid from msgs where instr(low, ?) > 0", (t,)):
                hits[uuid] = hits.get(uuid, 0) + 1
    finally:
        c.close()
    return hits


def continue_candidates(text):
    reg = G.load(G.REG_PATH, {})
    sm = summaries()
    try:
        dg = digests()
    except Exception:
        dg = {}
    live = G.live_uuids()
    terms = _cont_terms(text)
    kw = _cont_kw(terms)
    rows = []
    for uuid, rec in (reg.get("sessions") or {}).items():
        if rec.get("kind") != "rc" or not G.has_content(rec):
            continue
        if not os.path.exists(os.path.join(G.PROJ, uuid + ".jsonl")):
            continue
        summ = (sm.get(uuid) or {}).get("text", "")
        proj = project_of(rec)
        title = ((rec.get("title_manual") or "").strip()
                 or auto_title(rec.get("name") or "", summ, proj, uuid[:8]))
        rows.append({
            "uuid": uuid, "id": uuid[:8],
            "state": ("live" if uuid in live else
                      "archived" if rec.get("retired") else "idle"),
            "project": proj, "ts": rec.get("last_active_ts") or 0,
            "title": title, "what": summ or rec.get("name") or "",
            "said": ((dg.get(uuid) or {}).get("last_user") or ""),
            "kw": kw.get(uuid, 0)})
    # the prompt is capped: keep the word matches first, then the freshest
    rows.sort(key=lambda r: (-r["kw"], -r["ts"]))
    rows = rows[:CONT_MAX]
    rows.sort(key=lambda r: -r["ts"])
    return rows, terms


def continue_pick(text):
    """Best session for this request: {"uuid", "id", "title", "state", "why"},
    or None when nothing fits (or the model could not be asked)."""
    rows, terms = continue_candidates(text)
    if not rows:
        return None

    def cut(s, n):
        s = " ".join(str(s or "").split())
        return s[:n]
    listing = "\n".join(
        "%s | %s | %s | %s | %s | %s | %s | kw %d/%d" % (
            r["id"], r["state"], cut(r["project"], 30),
            datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M") if r["ts"] else "?",
            cut(r["title"], 60), cut(r["what"], 160), cut(r["said"], 110),
            r["kw"], len(terms))
        for r in rows)
    prompt = (CONT_PROMPT + "SESSIONS:\n" + listing + "\n\nREQUEST:\n"
              + text[:4000])
    try:
        os.makedirs(SUM_DIR)
    except OSError:
        pass
    t0 = time.time()
    try:
        r = helper_run(prompt, CONT_MODEL, 150)
    except Exception as exc:
        raise ValueError("could not ask the model which session fits (%s)" % exc)
    raw = (r.stdout or "").strip()
    i, j = raw.find("{"), raw.rfind("}")
    try:
        got = json.loads(raw[i:j + 1]) if 0 <= i < j else None
    except ValueError:
        got = None
    if not isinstance(got, dict):
        sys.stderr.write("[continue] not JSON: %r\n" % raw[:200])
        sys.stderr.flush()
        raise ValueError("the model gave no usable answer about which session fits")
    sid = str(got.get("session") or "").strip().lower()
    why = " ".join(str(got.get("why") or "").split())[:160]
    hit = next((x for x in rows if sid and x["id"] == sid[:8]), None)
    sys.stderr.write("[continue] %d candidates, %d terms, %.1fs -> %s | %s\n"
                     % (len(rows), len(terms), time.time() - t0,
                        hit["id"] if hit else "none", why))
    sys.stderr.flush()
    if not hit:
        return {"uuid": "", "why": why}
    return {"uuid": hit["uuid"], "id": hit["id"], "title": hit["title"],
            "state": hit["state"], "why": why}


def ingest(text=None, audio=None, name="voice.m4a", model=None, effort=None,
           polish=None, route=True):
    """Завести сессию из надиктованного. Аудио распознаём, текст правим.

    Правка текста по умолчанию ВЫКЛЮЧЕНА (INGEST_POLISH): на ходу важнее, чтобы
    сказанное ушло сразу, а не через полминуты. Аудио правится всегда — там
    whisper выдаёт заметно более сырой текст. Явный `polish` в запросе бьёт
    умолчание в обе стороны.

    Возвращает то, с чего сессия началась: отправитель у нас безголовый (часы,
    бот), и увидеть результат ему больше негде.
    """
    raw = ""
    if audio:
        got = transcribe(audio, name)
        raw, text = got["raw"], got["text"]
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to start a session with")
    if polish is None:
        polish = INGEST_POLISH

    target = ""
    if route:
        got = _route(text)
        if got:
            raw = raw or text
            text, target = got["text"], got["target"]
        elif polish and not audio:
            raw = text
            text = _voice_polish(text)
    elif polish and not audio:
        raw = text
        text = _voice_polish(text)

    if target:
        msg = act_say({"id": target, "text": text})
        return {"text": text, "raw": raw, "msg": msg,
                "action": "say", "session": target}
    msg = act_new({"text": text, "model": model, "effort": effort},
                  wait_link=False)
    return {"text": text, "raw": raw, "msg": msg, "action": "new"}


# ------------------------------------------------------------ просмотр файлов

FILE_MAX = 4 * 1024 * 1024
FILE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".pdf": "application/pdf", ".json": "application/json",
    ".html": "text/html", ".htm": "text/html",
}

# Смотреть можно в домашней папке И в рабочих папках Claude Code: отчёты и
# артефакты сессии складывают именно туда, а не в HOME. Без второго корня
# ссылка на свежесделанный отчёт открывалась «outside the home directory».
FILE_ROOTS = (os.path.realpath(os.path.expanduser("~")),
              os.path.realpath("/private/tmp/claude-501"),
              os.path.realpath("/tmp/claude-501"))


def file_tag(path):
    """Метка версии файла для ETag: путь, размер и mtime.

    Считается БЕЗ чтения содержимого — на 304 панель не должна поднимать
    мегабайты с диска. Любая ошибка (нет файла, путь не разрешён) даёт None:
    тогда запрос идёт обычной дорогой и получает честный отказ от read_file.
    """
    try:
        real = os.path.realpath(os.path.expanduser(path or ""))
        st = os.stat(real)
    except OSError:
        return None
    return '"%s-%x-%x"' % (
        hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()[:12],
        st.st_size, int(st.st_mtime))


def read_file(path):
    """Файл для просмотра в панели.

    ⛔ Ходим только внутри домашней папки и только по РАЗЫМЕНОВАННОМУ пути:
    иначе `../..` или симлинк наружу открыли бы что угодно на машине.
    """
    if not path:
        raise ValueError("no path given")
    real = os.path.realpath(os.path.expanduser(path))
    home = os.path.realpath(os.path.expanduser("~"))
    if not any(real == r or real.startswith(r + os.sep) for r in FILE_ROOTS):
        raise ValueError("outside the allowed folders")
    if os.path.isdir(real):
        raise ValueError("that is a directory")
    # Секреты не показываем даже своему: токен приёма охраняет публичный порт,
    # а ключи — вообще всё. Панель когда-то уже утекала наружу на минуту.
    rel = real[len(home):].lstrip(os.sep) if real.startswith(home) else real
    if rel.startswith(".ssh" + os.sep) or "rc-ingest-token" in rel \
            or rel.endswith((".pem", ".key")):
        raise ValueError("this file is not viewable")
    # Ссылка в переписке переживает сам файл: скретчпады лежат в /private/tmp,
    # и система их подчищает по своему расписанию. Это не сбой панели, и
    # прятать «файла нет» за идентификатором в логе — гонять оператора зря.
    # Проверка стоит ПОСЛЕ отказов выше, чтобы не подтверждать существование
    # того, что панель и так отказывается показывать.
    if not os.path.exists(real):
        gone = "this file is no longer there"
        if real.startswith("/private/tmp/") or real.startswith("/tmp/"):
            gone += " (scratchpad files are reaped by the system)"
        raise ValueError(gone)
    st = os.stat(real)
    if st.st_size > FILE_MAX:
        raise ValueError("too big to show: %.1f MB" % (st.st_size / 1048576.0))
    ext = os.path.splitext(real)[1].lower()
    ctype = FILE_TYPES.get(ext, "text/plain")
    with open(real, "rb") as fh:
        blob = fh.read()
    return blob, ctype, real, st.st_size


def _channel_ready(fifo):
    """Готов ли канал принять ввод. Открыть FIFO на запись удаётся, только если
    на том конце кто-то читает, — это и есть проверка живости, точнее любого
    разбора ps."""
    if not os.path.exists(fifo):
        return False
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


# ⛔ 10-16: Claude Code 2.1.301 strips invisible characters from a paste and then
# waits for a SECOND Enter: «Removed 24 invisible characters · review and press
# Enter to send». The first prompt of 4473a035 (a WhatsApp chat full of U+200E
# marks) sat in the box unsent while the panel said «new session launched».
# Two guards: the text goes in without those characters, and if the TUI still
# stops for a review, the panel presses Enter for it.
_INVISIBLE_KEEP = {"\u200d"}        # the zero-width joiner glues emoji together
REVIEW_RE = re.compile(r"invisible\s*char|review\s*and\s*press|Enter\s*to\s*send", re.I)


def _scrub_invisible(text):
    out = []
    for ch in text:
        if ch in "\u2028\u2029":
            out.append("\n")
        elif ch not in _INVISIBLE_KEEP and unicodedata.category(ch) == "Cf":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _log_size(log):
    try:
        return os.path.getsize(log) if log else None
    except OSError:
        return None


def _confirm_review(fifo, log, size0, wait=1.6):
    """If the screen asked to «review and press Enter to send» after our write,
    press Enter once more. Reads only what the screen printed since `size0`."""
    if size0 is None:
        return False
    time.sleep(wait)
    try:
        with open(log, "rb") as fh:
            fh.seek(size0)
            new = fh.read(400000).decode("utf-8", "replace")
    except OSError:
        return False
    flat = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x00-\x1f\x7f]", " ", new)
    if not REVIEW_RE.search(flat):
        return False
    # an Enter into a dialog answers it (10-25, see «the screen»)
    if screen_prompt(screen_lines(log)):
        return False
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"\r")
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def _codex_tui(log, uuid=None):
    """True when the session behind this log is a Codex TUI.

    ⛔ 26.11: the OSC 7501 state marks show up only when the state changes;
    an idle session's spinner title redraws push them out of the log tail, so
    the tail alone took a Codex session for Claude Code and typed into it key
    by key. Codex read that as a paste burst, the Enter became a new line and
    the message sat unsent while the panel waited for it.
    """
    try:
        if uuid and "/.codex/sessions/" in (_tx_path(uuid) or ""):
            return True
        if not log:
            return False
        import rc_codex
        tail = rc_codex._tail(log)
        with open(log, "rb") as fh:
            head = fh.read(262144)
        if b"app=vibe" in tail or b"app=vibe" in head:
            return False
        return bool(rc_codex.tui_state(log)) or b"\x1b]7501;" in head
    except Exception:
        return False


def _type_into(fifo, text, log=None):
    """Набрать текст в канал ввода: очистить поле, вписать, отправить.

    Символы и порядок объяснены в act_say — вслепую не менять.

    ⛔ Здесь дважды терялся ПЕРВЫЙ промпт новой сессии. `os.write` на
    неблокирующем канале берёт столько, сколько влезло, и возвращает это
    число: остаток пропадал молча, поэтому длинный промпт доезжал куском из
    середины (08-09, сессия 3ae410fa: «n my report ships it … went out befor»).
    А `" ".join(text.split())` заодно схлопывал переносы, превращая вставленную
    переписку в одну строку. act_say это давно умеет — идём той же дорогой:
    _write_all дописывает всё, многострочное уходит вставкой.
    """
    body = _scrub_invisible(text.strip())
    multiline = "\n" in body
    # Codex takes fast keystrokes for a paste burst and an Enter inside the
    # burst for a new line, so the first prompt sat unsent in its input box.
    # To Codex the text goes as a bracketed paste and Enter a second later.
    codex = False
    try:
        import rc_codex
        codex = _codex_tui(log)
    except Exception:
        pass
    size0 = _log_size(log)
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        _write_all(fd, b"\x7f" * CLEAR_BACKSPACES)
        time.sleep(0.4)
        if multiline or codex:
            _write_all(fd, b"\x1b[200~" + body.encode("utf-8") + b"\x1b[201~")
        else:
            _write_all(fd, body.encode("utf-8"))
        time.sleep(1.2 if codex else 0.5 if multiline else 0.2)
        _write_all(fd, b"\r")
    finally:
        os.close(fd)
    _confirm_review(fifo, log, size0)


_NEW = {"uuid": ""}   # the uuid the last act_new registered (act_new_pair reads it)

PAIR_EXEC_INTRO = (
    "You are the EXECUTOR in a pair of agents. An orchestrator agent will send you tasks "
    "in this chat, each starting with [task from the orchestrator]. For each task: do the "
    "work in this folder, then answer with a short report: what you did, files changed, "
    "commands run and their results, and anything left open. Do nothing until a task "
    "arrives. Reply now with one word: ready.")

PAIR_ORCH_NOTE = """

---
You are the ORCHESTRATOR of a pair of agents. Your executor is a second agent ({label}, session {eid}) working in the same folder ({cwd}). Plan the mission, hand the implementation steps to the executor one at a time, check what it did yourself (read the files, run the checks), send it fixes until the mission is done, then report to the person: what the executor did and what you verified.
To give the executor a task, run in your shell (a long task goes on stdin):
  python3 {bin} ask {eid} - <<'TASK'
  <the task, self-contained: the executor does not see this conversation>
  TASK
It sends the task, waits up to 100 seconds and prints the executor's answer. If it prints STILL WORKING, run `python3 {bin} wait {eid}` until it answers (add --max 540 if your shell tool allows a 10-minute command)."""


def act_new_pair(target):
    """26.11: RECRUIT launches an orchestrator and an executor together.
    The executor starts first and waits; the orchestrator gets the mission plus
    how to reach it (rc_pair.py). Both records keep the link to each other."""
    ex = target.get("exec") or {}
    et = {"text": PAIR_EXEC_INTRO, "provider": ex.get("provider") or "",
          "model": ex.get("model") or "", "effort": ex.get("effort") or "", "nolink": True}
    _NEW["uuid"] = ""
    msg1 = act_new(et, wait_link=False)
    eid = _NEW["uuid"]
    if not eid:
        raise ValueError("the executor did not start: %s" % msg1)
    label = _agent_label(ex.get("provider"), ex.get("model"))
    ot = dict(target)
    ot.pop("exec", None)
    ot["text"] = (target.get("text") or "").strip() + PAIR_ORCH_NOTE.format(
        label=label, eid=eid[:8], cwd=G.WORKDIR,
        bin=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rc_pair.py"))
    _NEW["uuid"] = ""
    msg2 = act_new(ot, wait_link=False)
    oid = _NEW["uuid"]
    title = " ".join((target.get("text") or "").split())[:60]

    def _link():
        reg = G.load(G.REG_PATH, {})
        sess = reg.get("sessions", {})
        for u, role, peer in ((eid, "executor", oid), (oid, "orchestrator", eid)):
            if u and u in sess:
                sess[u]["pair"] = {"role": role, "peer": peer}
                sess[u]["title_manual"] = ("%s: %s" % ("Executor" if role == "executor" else "Orchestrator", title))[:40]
        G.save(G.REG_PATH, reg)
    try:
        with_lock(_link)
    except Exception:
        pass
    if not oid:
        return "executor %s is up, but the orchestrator did not start: %s" % (eid[:8], msg2)
    last = bay_last()
    _bay_remember(last.get("provider"), last.get("model"), last.get("effort"),
                  {"provider": ex.get("provider") or "claude", "model": ex.get("model") or "",
                   "effort": ex.get("effort") or ""})
    # «new session <id>» first: the launch screen lands on that id (the orchestrator)
    return "new session %s launched as the orchestrator (%s), its executor is %s (%s)" % (
        oid[:8], _agent_label(target.get("provider"), target.get("model")), eid[:8], label)


def _handoff_brief(uuid, old_label):
    """The first prompt of the session that takes over: the task the old one
    started with, the latest part of its conversation, and where the full
    history lies."""
    msgs = [m for m in (transcript(uuid, limit=400).get("shown") or [])
            if m.get("role") in ("user", "assistant") and (m.get("text") or "").strip()]
    first = next(((m.get("text") or "").strip() for m in msgs if m.get("role") == "user"), "")
    tail, size = [], 0
    for m in reversed(msgs[1:] if msgs and first and msgs[0].get("role") == "user" else msgs):
        t = " ".join((m.get("text") or "").split())
        if len(t) > 2500:
            t = t[:2500] + " [...]"
        line = "%s: %s" % ("PERSON" if m["role"] == "user" else "AGENT", t)
        if size + len(line) > 14000:
            break
        tail.append(line)
        size += len(line)
    tail.reverse()
    path = _tx_path(uuid)
    return ("You are taking over a session that ran on %s (session %s) in this folder (%s). "
            "Continue the work from where it stopped.\n\n"
            "The task it started with:\n%s\n\n"
            "The latest part of its conversation, oldest first:\n%s\n\n"
            "The full history is in %s if you need more detail. First check the current state "
            "of the files, then go on. Tell the person in one or two lines where you picked up."
            % (old_label, uuid[:8], G.WORKDIR, first[:4000] or "(not found)",
               "\n".join(tail) or "(empty)", path))


def act_handoff(target):
    """26.11 «Continue in...»: the session goes on with another agent. A new
    session starts there with a brief of this one (_handoff_brief); this one
    stays as it is, and the two records point at each other."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, provider}")
    uuid = resolve(target.get("id") or "")
    rec = G.load(G.REG_PATH, {}).get("sessions", {}).get(uuid) or {}
    old = rec.get("provider") or "claude"
    new = (target.get("provider") or "claude").strip()
    old_label = _agent_label(old, rec.get("model"))
    brief = _handoff_brief(uuid, old_label)
    _NEW["uuid"] = ""
    msg = act_new({"text": brief, "provider": new, "model": target.get("model") or "",
                   "effort": target.get("effort") or "", "nolink": True, "quiet_last": True}, wait_link=False)
    nid = _NEW["uuid"]
    if not nid:
        raise ValueError("the new session did not start: %s" % msg)
    title = (rec.get("title_manual") or rec.get("title") or rec.get("name") or uuid[:8])

    def _link():
        reg = G.load(G.REG_PATH, {})
        sess = reg.get("sessions", {})
        if nid in sess:
            sess[nid]["handoff_from"] = uuid
            short = " ".join(title.split())
            lab = " → " + _agent_label(new, None)
            sess[nid]["title_manual"] = (short[:max(8, 40 - len(lab) - 1)].rstrip() + ("…" if len(short) > 40 - len(lab) - 1 else "") + lab)[:40]
        if uuid in sess:
            sess[uuid]["handoff_to"] = nid
        G.save(G.REG_PATH, reg)
    try:
        with_lock(_link)
    except Exception:
        pass
    return {"msg": "new session %s continues %s on %s" % (nid[:8], uuid[:8], _agent_label(new, None)),
            "sid": nid[:8]}


def act_attach_executor(target):
    """26.11: a running session becomes an orchestrator: an executor starts on
    the picked agent, and the session is told how to give it tasks."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {id, provider}")
    uuid = resolve(target.get("id") or "")
    _NEW["uuid"] = ""
    prov = (target.get("provider") or "codex").strip()
    msg = act_new({"text": PAIR_EXEC_INTRO, "provider": prov, "model": target.get("model") or "",
                   "effort": target.get("effort") or "", "nolink": True, "quiet_last": True}, wait_link=False)
    eid = _NEW["uuid"]
    if not eid:
        raise ValueError("the executor did not start: %s" % msg)
    label = _agent_label(prov, target.get("model"))
    note = PAIR_ORCH_NOTE.format(label=label, eid=eid[:8], cwd=G.WORKDIR,
                                 bin=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rc_pair.py"))
    note = note.replace("\n---\nYou are the ORCHESTRATOR", "From now on you are the ORCHESTRATOR").strip()
    rec = G.load(G.REG_PATH, {}).get("sessions", {}).get(uuid) or {}
    title = " ".join((rec.get("title_manual") or rec.get("title") or rec.get("name") or "").split())[:30]

    def _link():
        reg = G.load(G.REG_PATH, {})
        sess = reg.get("sessions", {})
        if eid in sess:
            sess[eid]["pair"] = {"role": "executor", "peer": uuid}
            sess[eid]["title_manual"] = ("Executor: " + title)[:40]
        if uuid in sess:
            sess[uuid]["pair"] = {"role": "orchestrator", "peer": eid}
        G.save(G.REG_PATH, reg)
    try:
        with_lock(_link)
    except Exception:
        pass
    res = act_say({"id": uuid, "text": note})
    told = res.get("msg") if isinstance(res, dict) else res
    return "executor %s (%s) is up; %s: %s" % (eid[:8], label, uuid[:8], (told or "")[:80])


_CAT_CACHE = {"t": 0.0, "v": None}


def bay_catalog_cached():
    if time.time() - _CAT_CACHE["t"] > 20 or _CAT_CACHE["v"] is None:
        _CAT_CACHE.update(t=time.time(), v=bay_catalog())
    return _CAT_CACHE["v"]


def _agent_label(provider, model):
    names = {"": "Claude Code", "claude": "Claude Code", "codex": "Codex", "vibe": "Mistral Vibe"}
    try:
        import rc_providers
        base = names.get(provider or "") or rc_providers.label(provider) or provider
    except Exception:
        base = names.get(provider or "", provider or "Claude Code")
    return base + (" " + model if model else "")


def act_new(target, wait_link=True):
    if isinstance(target, dict) and target.get("exec"):
        return act_new_pair(target)
    return _act_new_one(target, wait_link)


def _act_new_one(target, wait_link=True):
    """Завести новую сессию прямо из панели.

    Чем это лучше чата, начатого в приложении: процесс наш, у него есть канал
    ввода, гардиан знает о нём с первого census и поднимет после падения. Чат из
    приложения — ребёнок демона: он умирает вместе с ним и заводится заново
    только руками.
    """
    if not isinstance(target, dict):
        raise ValueError("expected an object {text}")
    text = (target.get("text") or "").strip()
    if not text:
        raise ValueError("a first prompt is required")
    if len(text) > SAY_CHARS_MAX:
        raise ValueError("too long: %d characters, the limit is %d"
                         % (len(text), SAY_CHARS_MAX))
    model = (target.get("model") or "").strip() or None
    effort = (target.get("effort") or "").strip() or None
    # HQ shows no link: waiting up to 16 s for it only kept the launch screen
    # spinning after the session was already working (10-16)
    if target.get("nolink"):
        wait_link = False
    provider = (target.get("provider") or "").strip() or None
    if not provider and not model and CFG.DEFAULT_PROVIDER:
        provider = CFG.DEFAULT_PROVIDER
    vmodel = None
    if model and model.startswith("provider:"):   # HQ's model list carries providers too
        provider, model = model[len("provider:"):], None
        if provider.startswith("vibe:"):          # provider:vibe:<model alias>
            provider, vmodel = "vibe", provider[len("vibe:"):]
    if provider == "claude":
        provider = None
    if provider == "codex":
        # the Codex CLI on its ChatGPT login (rc_codex), its own model and effort
        import rc_codex
        if not rc_codex.codex_bin():
            raise ValueError("Codex is not installed on this computer")
        # RECRUIT picks Codex's own model and effort (26.11); anything else is
        # dropped and Codex uses its default
        if model not in [m for m, _ in rc_codex.models()]:
            model = None
        if effort not in rc_codex.EFFORT_CHOICES:
            effort = None
        wait_link = False
    elif provider == "vibe":
        # the Mistral Vibe CLI on a Vibe plan key (rc_vibe)
        import rc_vibe
        import rc_providers
        if not rc_vibe.vibe_bin():
            raise ValueError("Mistral Vibe is not installed on this computer")
        if not rc_providers.vibe_key():
            raise ValueError("no Mistral Vibe key: see INSTALL.md, \"Other models\"")
        if not vmodel and model in rc_vibe.MODELS:
            vmodel = model
        model = effort = None
        wait_link = False
    elif provider:
        # another model behind the same claude (rc_providers): its env picks the
        # model; it has no Remote Control, so there is no link to wait for
        import rc_providers
        if provider not in rc_providers.PROVIDERS:
            raise ValueError("unknown provider %r" % provider[:20])
        ok, bal = rc_providers.balance(provider)
        if ok is False:
            raise ValueError("%s balance is empty (%s): top it up first"
                             % (rc_providers.label(provider), bal))
        model = effort = None
        wait_link = False
    if model and model not in MODELS and provider != "codex":
        raise ValueError("invalid model %r" % model[:20])
    if effort and effort not in EFFORTS and provider != "codex":
        raise ValueError("invalid effort %r" % effort[:20])

    name = G.make_name(" ".join(text.split()), "new")
    key = "new-%d" % int(time.time())
    if not os.path.isdir(G.WORKDIR):
        raise ValueError("the work folder %s does not exist: run "
                         "mkdir -p %s" % (G.WORKDIR, G.WORKDIR))
    if provider not in ("codex", "vibe") and not CFG.workdir_trusted():
        raise ValueError("Claude Code does not trust the work folder yet. In a "
                         "terminal run: cd %s && claude, answer \"Yes, I trust "
                         "this folder\", then /exit and try again" % G.WORKDIR)
    # a fresh install: Claude creates the project folder only on the first session
    os.makedirs(G.PROJ, exist_ok=True)
    before = set(os.listdir(G.PROJ))
    logf, fifo = G.launch_new(name, key, vmodel or model, effort, provider)
    # RECRUIT opens on this choice next time, on every device (26.11); not for
    # the sessions a handoff or an executor attach starts on their own
    if not target.get("quiet_last"):
        _bay_remember(provider, vmodel or model, effort)

    # uuid узнаём по появившемуся транскрипту: до первого промпта Claude его не
    # создаёт, поэтому ждём, а не спрашиваем.
    # ⛔ Печатать промпт сразу нельзя: Claude поднимается несколько секунд, и
    # набранное до готовности TUI уходит в никуда (проверено 08-24 — сессия
    # завелась с нулём сообщений). Транскрипт же создаётся при старте, ещё до
    # любого промпта, — вот он и служит сигналом «загрузился».
    uuid = None
    typed = False
    rollout = ""
    t0 = time.time()
    if provider in ("codex", "vibe"):
        # Codex and Vibe say idle at start, work through their startup, then
        # idle again: type only into that second idle, then find the thread
        import rc_codex
        vhome = None
        if provider == "vibe":
            import rc_providers
            vhome = rc_providers.vibe_home(vmodel)
        seen_work = False
        for _ in range(60):
            time.sleep(1.0)
            st = rc_codex.tui_state(logf)
            seen_work = seen_work or st == "working"
            if st == "idle" and (seen_work or time.time() - t0 > 10):
                break
        time.sleep(1.0)
        _type_into(fifo, text, logf)
        typed = True
        for _ in range(30):
            time.sleep(1.0)
            if provider == "vibe":
                import rc_vibe
                rollout, cid = rc_vibe.find_session(t0, G.WORKDIR, vhome)
            else:
                rollout, cid = rc_codex.find_rollout(t0, G.WORKDIR)
            if cid:
                # keyed so that its first 8 characters are not the clock (rc_codex.key_for)
                uuid = rc_codex.key_for(cid)
                break
    elif provider:
        # On another model (an API token in the env) Claude Code writes the
        # transcript at the first prompt, not at start: wait for the input box
        # on its screen, type, then look for the transcript.
        for _ in range(40):
            time.sleep(1.0)
            try:
                with open(logf, "rb") as fh:
                    if "\u276f".encode() in fh.read():
                        break
            except OSError:
                pass
        time.sleep(2.0)
        _type_into(fifo, text, logf)
        typed = True
    for _ in range(0 if provider in ("codex", "vibe") else 30):
        time.sleep(1.0)
        try:
            fresh = [f for f in os.listdir(G.PROJ)
                     if f.endswith(".jsonl") and f not in before]
        except OSError:
            fresh = []
        if fresh:
            fresh.sort(key=lambda f: os.path.getmtime(os.path.join(G.PROJ, f)))
            uuid = fresh[-1][:-6]
            break

    if uuid:
        try:      # канал переезжает под настоящий uuid — fd у процесса тот же
            os.rename(fifo, G.fifo_path(uuid))
            fifo = G.fifo_path(uuid)
        except OSError:
            pass
        # Единственная связь процесса с сессией: argv тут без --resume, потому
        # что в момент запуска uuid ещё не существовало. Без этого файла
        # live_uuids() читает живую сессию как мёртвую, а restore_batch
        # поднимает на ней второй claude. См. G.new_session_uuid().
        G.record_new_uuid(key, uuid)

        # Заносим сессию в реестр сами. snapshot() его только читает, census не
        # запускает, так что иначе строка появилась бы лишь после ближайшего
        # прохода гардиана — до минуты «создал, а его нет». На census тут
        # полагаться нельзя ещё и потому, что entrypoint в транскрипт попадает
        # не мгновенно: осмотри его census сейчас, он записал бы kind "other".
        def _register():
            reg = G.load(G.REG_PATH, {})
            sess = reg.setdefault("sessions", {})
            rec = sess.get(uuid)
            if not rec or rec.get("kind") != "rc":
                rec = sess[uuid] = {"kind": "rc", "restores": 0,
                                    "first_seen": G.now()}
            rec["state"] = "restored"
            rec["retired"] = False
            # ⛔ The model chosen here was only ever a launch flag: the record
            # never got it, so the first restore (a wipe, an account switch)
            # brought the session back on the CLI default. 09-24: 9 of 11 live
            # sessions had no model in the registry.
            if model:
                rec["model"] = model
            if effort:
                rec["effort"] = effort
            if provider:
                rec["provider"] = provider
            if vmodel:
                rec["model"] = vmodel
            if provider in ("codex", "vibe") and rollout:
                rec["rollout"] = rollout
            if not rec.get("prompt"):
                # имя у нас уже есть — census вывел бы «session xxxxxxxx»,
                # пока первая реплика не долетит до диска
                rec["prompt"] = text[:200]
                rec["name"] = name
            try:
                st = os.stat(rollout if provider == "codex" and rollout
                             else os.path.join(rollout, "meta.json") if provider == "vibe" and rollout
                             else os.path.join(G.PROJ, uuid + ".jsonl"))
                rec["last_active"] = datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds")
                rec["last_active_ts"] = st.st_mtime
                rec["size"] = st.st_size
            except OSError:
                pass
            G.save(G.REG_PATH, reg)

        try:
            with_lock(_register)
        except Exception:
            pass        # не дались — гардиан подберёт её своим проходом

    try:
        if not typed:
            time.sleep(2.0)      # дать TUI дорисовать первый экран
            _type_into(fifo, text, logf)
    except OSError as exc:
        return ("session %s launched, but the first prompt did not land (%s) — "
                "write to it from the chat" % (uuid[:8] if uuid else key, exc.strerror or exc))

    # Ссылку на карточку ждём только когда её есть кому показать. Приём с
    # часов её не читает — там важно, чтобы ответ пришёл до того, как человек
    # уберёт телефон в карман, а ссылка всё равно подтянется в панель сама.
    link = ""
    if wait_link:
        for _ in range(8):
            link = G.read_link(logf)
            if link:
                break
            time.sleep(2)

    _NEW["uuid"] = uuid or ""
    who = uuid[:8] if uuid else key
    if link:
        return "new session %s → https://claude.ai/code/%s" % (who, link)
    if provider:
        import rc_providers
        return "new session %s launched on %s" % (
            who, {"codex": "Codex", "vibe": "Mistral Vibe"}.get(provider) or rc_providers.label(provider))
    return "new session %s launched, the link appears within a minute" % who


def act_bulk_restart(prefs):
    """Перезапустить выделенные живые сессии: остановить, оставив в очереди.

    Закрывает сразу две вещи, которые иначе делаются вручную и по одной:
    применить выставленные model/effort (они аргументы запуска `claude`, на лету
    не меняются) и выдать сессии канал ввода, если её подняли до его появления.
    """
    good, bad = _resolve_many(prefs)
    rows = ps_rows()
    hit = []
    for uuid in good:
        pid = pid_for(uuid, rows)
        if pid is not None:
            kill_tree(pid)
            hit.append(uuid)

    if hit:
        def edit():
            reg = G.load(G.REG_PATH, {})
            for uuid in hit:
                rec = reg.get("sessions", {}).get(uuid)
                if rec:
                    rec["state"] = "dead"
                    rec["retired"] = False
            G.save(G.REG_PATH, reg)
        with_lock(edit)

    msg = "restarting: %d · the guardian relaunches 3 per minute" % len(hit)
    skipped = len(good) - len(hit)
    if skipped:
        msg += " · %d were not running" % skipped
    return msg + _tail(bad)


def act_kill_empty(_=None):
    """Kill every live session that has no user message in it.

    These are the `claude -p "/usage"` artefacts described in G.has_content():
    real processes, real MAX_LIVE slots, nothing inside to come back to. Freeing
    the slot is the whole fix — the next guardian pass restores an actual chat
    into it within a minute.
    """
    reg = G.load(G.REG_PATH, {})
    rows = ps_rows()
    victims = []
    for uuid in G.live_uuids():
        if G.has_content(reg.get("sessions", {}).get(uuid) or {}):
            continue
        pid = pid_for(uuid, rows)
        if pid:
            victims.append((uuid, pid))
    if not victims:
        return "no empty live sessions"

    for _uuid, pid in victims:
        kill_tree(pid)

    def edit():
        r = G.load(G.REG_PATH, {})
        for uuid, _pid in victims:
            if uuid in r.get("sessions", {}):
                r["sessions"][uuid]["state"] = "dead"
        G.save(G.REG_PATH, r)
    with_lock(edit)
    return ("slots freed: %d (%s)"
            % (len(victims), ", ".join(u[:8] for u, _p in victims)))


def act_retire(pref):
    uuid = resolve(pref)

    def edit():
        reg = G.load(G.REG_PATH, {})
        reg["sessions"][uuid]["retired"] = True
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    return "%s retired" % uuid[:8]


def act_unretire(pref):
    uuid = resolve(pref)

    def edit():
        reg = G.load(G.REG_PATH, {})
        reg["sessions"][uuid]["retired"] = False
        G.save(G.REG_PATH, reg)
    with_lock(edit)
    return "%s back in the queue" % uuid[:8]


# ---------------------------------------------------------------- delete

# «Удалить», а не «снять» (the owner, 11-16: retire only archives, there was no way
# to make a session go away). Removing the registry row alone does nothing: the
# guardian's census re-registers every transcript in PROJ within a minute. So
# the transcript (and its <uuid>/ folder of subagents and tool results) leaves
# PROJ for RC_TRASH, where it sits TRASH_KEEP_S and is then purged for good.
RC_TRASH = CFG.state("rc-trash")
TRASH_KEEP_S = 7 * 86400


def _purge_trash():
    try:
        names = os.listdir(RC_TRASH)
    except OSError:
        return
    cut = time.time() - TRASH_KEEP_S
    for n in names:
        path = os.path.join(RC_TRASH, n)
        try:
            if os.lstat(path).st_mtime < cut:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _trash_session(uuid):
    """Move the transcript and its folder out of PROJ; drop /tmp runtime files."""
    import glob
    dest = os.path.join(RC_TRASH, uuid)
    os.makedirs(dest, exist_ok=True)
    os.utime(dest, None)          # the purge clock starts at deletion
    moved = 0
    for src in (os.path.join(G.PROJ, uuid + ".jsonl"), os.path.join(G.PROJ, uuid)):
        if os.path.lexists(src):
            tgt = os.path.join(dest, os.path.basename(src))
            if os.path.lexists(tgt):
                shutil.rmtree(tgt, ignore_errors=True) if os.path.isdir(tgt) else os.unlink(tgt)
            shutil.move(src, tgt)
            moved += 1
    u8 = uuid[:8]
    for f in (glob.glob("/tmp/rc-in-%s.fifo" % u8) + glob.glob("/tmp/rc-launch-%s.sh" % u8)
              + glob.glob("/tmp/rc-restore-%s-*.log" % u8)):
        try:
            os.unlink(f)
        except OSError:
            pass
    return moved


def _delete_many(uuids):
    rows = ps_rows()
    killed = 0
    for uuid in uuids:
        pid = pid_for(uuid, rows)
        if pid is not None:
            kill_tree(pid)
            killed += 1
    gone = set(uuids)

    def edit():
        reg = G.load(G.REG_PATH, {})
        sess = reg.get("sessions", {})
        n = 0
        for uuid in uuids:
            # moved under the lock: a census pass cannot run between the move
            # and the registry write and re-add the row
            _trash_session(uuid)
            if sess.pop(uuid, None) is not None:
                n += 1
        G.save(G.REG_PATH, reg)
        nu = G.load(G.NEW_UUIDS_PATH, {})
        keep = {k: v for k, v in nu.items() if v not in gone}
        if len(keep) != len(nu):
            G.save(G.NEW_UUIDS_PATH, keep)
        return n
    n = with_lock(edit)
    for path in (DIGEST_CACHE, SUM_CACHE):
        try:
            c = G.load(path, {})
            if isinstance(c, dict) and any(u in c for u in gone):
                for u in gone:
                    c.pop(u, None)
                G.save(path, c)
        except Exception:
            pass
    _purge_trash()
    return killed, n


def act_delete(pref):
    """Delete a session: stop it if running, drop it from the registry, move
    its transcript to ~/.claude/rc-trash (purged after 7 days)."""
    uuid = resolve(pref)
    killed, _n = _delete_many([uuid])
    return "%s deleted%s" % (uuid[:8], " (stopped first)" if killed else "")


def act_bulk_delete(prefs):
    good, bad = _resolve_many(prefs)
    killed, n = _delete_many(good)
    return "deleted: %d · stopped first: %d%s" % (n, killed, _tail(bad))


def act_retire_older(hours):
    """Bulk 'Снять' over the queue — everything not touched for N hours.

    Scoped to the panel's own queue definition rather than the guardian's 48h
    RESTORE_FRESH_H window: the steady-state top-up draws candidates from the
    full 168h working set, so a four-day-old chat still competes for a slot and
    still has to be clearable from here. `--preflight --retire-older-than`
    cannot reach those.

    ONE locked pass, not N sequential act_retire() calls: each takes and frees
    the lock separately, so a launchd cycle can slot in between two of them and
    write its stale registry copy back over the edits already made.
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        raise ValueError("expected a number of hours, got %r" % (hours,))
    if h < 1:
        raise ValueError("minimum 1 hour — otherwise it retires what closed a minute ago")
    cut = time.time() - h * 3600
    live = G.live_uuids()

    def edit():
        reg = G.load(G.REG_PATH, {})
        hit = []
        for uuid, rec in reg.get("sessions", {}).items():
            # same predicate the panel groups under «В очереди», so the button
            # clears exactly the rows it sits under
            if rec.get("kind") != "rc" or rec.get("retired") or uuid in live:
                continue
            if rec.get("state") != "dead":
                continue
            if (rec.get("last_active") or "")[:16] < WINDOW_FROM:
                continue
            if rec.get("last_active_ts", 0) >= cut:
                continue
            rec["retired"] = True
            hit.append(uuid)
        if hit:
            G.save(G.REG_PATH, reg)
        return hit
    hit = with_lock(edit)

    if not hit:
        return "nothing in the queue older than %gh" % h
    head = ", ".join(u[:8] for u in hit[:6])
    tail = " and %d more" % (len(hit) - 6) if len(hit) > 6 else ""
    return "retired %d from the queue (older than %gh): %s%s" % (len(hit), h, head, tail)


def act_up(pref):
    """Restore a session into the app right now, bypassing the guardian's cap.

    The cap protects the box from the guardian filling it automatically; a
    deliberate hand-restore is exactly the case it should not veto. The caller
    is told when this pushes past the cap so the choice stays visible.
    """
    uuid = resolve(pref)
    if pid_for(uuid) is not None:
        return "%s is already live" % uuid[:8]

    def edit():
        reg = G.load(G.REG_PATH, {})
        rec = reg["sessions"][uuid]
        rec["retired"] = False
        name = rec.get("name") or uuid[:8]
        logf = G.launch(uuid, name, *G.launch_opts(uuid, rec))
        rec["state"] = "restored"
        rec["restores"] = rec.get("restores", 0) + 1
        rec["last_restore"] = G.now()
        G.save(G.REG_PATH, reg)
        return logf
    logf = with_lock(edit)

    link = ""
    for _ in range(10):
        time.sleep(3)
        link = G.read_link(logf)
        if link:
            break
    if link:
        def stamp():
            reg = G.load(G.REG_PATH, {})
            reg["sessions"][uuid]["app_session"] = link
            G.save(G.REG_PATH, reg)
        with_lock(stamp)
        return "%s revived → %s" % (uuid[:8], link)
    return "%s started, the link appears within a minute" % uuid[:8]


if __name__ == "__main__":
    print(json.dumps(snapshot()["meta"], ensure_ascii=False, indent=2))
