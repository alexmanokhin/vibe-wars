#!/usr/bin/env python3
"""Search across every RC session: titles, summaries and the conversation text.

12.11 the owner: «хочу поиск по всем сессиям в дашборде, чтобы легко находил любые
сессии». The left column had 535 sessions in folds and no way to find «the one
where we did the payment page» other than scrolling.

Index: ~/.claude/rc-search.db (SQLite, WAL). One row per user/assistant TEXT
message of every transcript in the RC project folder; tool calls, tool results,
meta records and images are skipped (they are 99 % of the 10 GB and noise for
finding a session). `low` is Python's lower() of the text, because SQLite's
lower() folds ASCII only and the conversations are mostly Russian.

Update is incremental: transcripts are append-only, so each file keeps the byte
offset of its last complete line; a shrunk or replaced file (other inode) is
re-read from zero. A full first build is ~6 s for 480k lines; after that a pass
is one stat per file. update() takes a non-blocking flock, so two searches at
once never build twice; the loser just queries what is there.

Python 3.9 (the plists run /usr/bin/python3): no match, no X | Y.
"""
import fcntl
import glob
import html
import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import sqlite3
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
WORKDIR = os.path.realpath(os.environ.get("RC_WORKDIR") or HOME)
PROJ = CFG.PROJ
DB = CFG.state("rc-search.db")
LOCK = DB + ".lock"
MAX_TEXT = 20000          # one message; a pasted 200 KB log is not worth more
MAX_TERMS = 6
SCHEMA_V = "2"            # 2: task notifications out (16.11, they showed as «YOU» hits)


def _conn():
    c = sqlite3.connect(DB, timeout=15)
    c.execute("pragma journal_mode=wal")
    c.execute("pragma synchronous=normal")
    c.executescript("""
    create table if not exists meta(k text primary key, v text);
    create table if not exists files(name text primary key, off integer, ino integer);
    create table if not exists msgs(id integer primary key, uuid text, ts real,
                                    role text, rec text, body text, low text);
    create index if not exists msgs_uuid on msgs(uuid);
    """)
    v = c.execute("select v from meta where k='schema'").fetchone()
    if not v or v[0] != SCHEMA_V:
        c.executescript("delete from msgs; delete from files;")
        c.execute("insert or replace into meta values('schema', ?)", (SCHEMA_V,))
        c.commit()
    return c


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _text(r):
    """(role, text) of a record worth indexing, else None."""
    ty = r.get("type")
    if ty not in ("user", "assistant") or r.get("isMeta") or r.get("isSidechain"):
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        txt = c
    elif isinstance(c, list):
        txt = "\n".join(b.get("text") or "" for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    txt = txt.strip()
    if (not txt or txt.startswith("<local-command") or txt.startswith("Caveat: The messages below")
            or txt.startswith("<task-notification>")):
        return None
    return ty, txt[:MAX_TEXT]


def _index_file(c, path, name, st):
    uuid = name[:-6]
    row = c.execute("select off, ino from files where name=?", (name,)).fetchone()
    off = 0
    if row:
        off, ino = row
        if ino == st.st_ino and off == st.st_size:
            return 0
        if ino != st.st_ino or st.st_size < off:
            c.execute("delete from msgs where uuid=?", (uuid,))
            off = 0
    added = []
    with open(path, "rb") as fh:
        fh.seek(off)
        for ln in fh:
            if not ln.endswith(b"\n"):
                break                      # half-written line: next pass
            off += len(ln)
            # cheap pre-filter: most lines are tool traffic and never parsed
            if b'"tool_use_id"' in ln or (b'"type":"user"' not in ln
                                          and b'"type":"assistant"' not in ln):
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = _text(r)
            if not t:
                continue
            added.append((uuid, _ts(r.get("timestamp")), t[0], str(r.get("uuid") or ""),
                          t[1], t[1].lower()))
    if added:
        c.executemany("insert into msgs(uuid, ts, role, rec, body, low) values(?,?,?,?,?,?)",
                      added)
    c.execute("insert or replace into files values(?,?,?)", (name, off, st.st_ino))
    c.commit()
    return len(added)


def update():
    """Bring the index up to date. Returns messages added, or -1 when another
    process holds the lock (it is doing the same work)."""
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lf.close()
        return -1
    try:
        c = _conn()
        try:
            seen, n = set(), 0
            for path in glob.glob(os.path.join(PROJ, "*.jsonl")):
                name = os.path.basename(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                seen.add(name)
                try:
                    n += _index_file(c, path, name, st)
                except Exception as exc:
                    sys.stderr.write("[rc_search] %s: %r\n" % (name, exc))
            gone = [x for (x,) in c.execute("select name from files") if x not in seen]
            for name in gone:
                c.execute("delete from msgs where uuid=?", (name[:-6],))
                c.execute("delete from files where name=?", (name,))
            c.execute("insert or replace into meta values('updated', ?)", (str(time.time()),))
            c.commit()
            return n
        finally:
            c.close()
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def terms_of(q):
    """Words AND-ed; "a quoted phrase" stays one term."""
    q = (q or "").strip().lower()
    out, i = [], 0
    while i < len(q) and len(out) < MAX_TERMS:
        if q[i] == '"':
            j = q.find('"', i + 1)
            j = len(q) if j < 0 else j
            t = q[i + 1:j].strip()
            i = j + 1
        else:
            j = q.find(" ", i)
            j = len(q) if j < 0 else j
            t = q[i:j].strip()
            i = j + 1
        if t and t not in out:
            out.append(t)
    return out


def _snip(body, terms, width=90):
    low = body.lower()
    pos = min([p for p in (low.find(t) for t in terms) if p >= 0] or [0])
    a = max(0, pos - width // 2)
    b = min(len(body), a + width * 2)
    # tool output in transcripts is stored HTML-escaped (&amp;&amp;); the UI escapes once itself
    s = html.unescape(" ".join(body[a:b].split()))
    return ("…" if a > 0 else "") + s + ("…" if b < len(body) else "")


def _sub(text, terms):
    """The summary line, cut around the match when the match is past what fits."""
    low = text.lower()
    pos = [p for p in (low.find(t) for t in terms) if p >= 0]
    if not pos or min(pos) < 30:
        return text
    a = max(0, min(pos) - 12)            # the column shows ~45 chars: match near the start
    return "…" + " ".join(text[a:a + 160].split()) + ("…" if a + 160 < len(text) else "")


def search_text(q, per_session=3):
    """{uuid: {"n": matching messages, "last": ts, "hits": [{ts, role, snip}]}}
    for sessions where EVERY term occurs somewhere in the conversation (not
    necessarily in one message). Snippets prefer messages carrying most terms."""
    terms = terms_of(q)
    if not terms:
        return {}, terms
    c = _conn()
    try:
        per = []
        for t in terms:
            d = {}
            for mid, uuid in c.execute("select id, uuid from msgs where instr(low, ?) > 0", (t,)):
                d.setdefault(uuid, []).append(mid)
            per.append(d)
        uuids = set(per[0])
        for d in per[1:]:
            uuids &= set(d)
        out = {}
        for uuid in uuids:
            score = {}
            for d in per:
                for mid in d[uuid]:
                    score[mid] = score.get(mid, 0) + 1
            ids = sorted(score, key=lambda m: (-score[m], -m))[:per_session]
            rows = c.execute("select id, ts, role, body from msgs where id in (%s)"
                             % ",".join("?" * len(ids)), ids).fetchall()
            rows.sort(key=lambda x: (-score[x[0]], -x[1]))
            out[uuid] = {"n": len(score), "last": max(x[1] for x in rows) if rows else 0,
                         "hits": [{"ts": x[1], "role": x[2], "snip": _snip(x[3], terms)}
                                  for x in rows]}
        return out, terms
    finally:
        c.close()


def search(q, rows, limit=80):
    """Sessions matching q, ranked: name/summary hits first, then by last activity.

    rows: the panel snapshot rows (title, name, summary, project, short, uuid,
    la_ts, live, retired, state, busy). Only sessions the panel knows can be
    opened, so transcript-only hits (sdk-cli junk, deleted rows) are dropped.
    """
    t0 = time.time()
    try:
        update()
    except Exception as exc:
        sys.stderr.write("[rc_search] update failed: %r\n" % (exc,))
    t1 = time.time()
    text, terms = search_text(q)
    if not terms:
        return {"ok": True, "q": q, "n": 0, "results": []}
    res = []
    for r in rows:
        if r.get("empty"):
            continue
        # a row whose transcript is gone cannot be opened (GET /transcript
        # fails on every box): 180 such rows on the main Mac, more on machine B
        if not os.path.exists(os.path.join(PROJ, (r.get("uuid") or "") + ".jsonl")):
            continue
        hay = " ".join(str(r.get(k) or "") for k in
                       ("title", "name", "summary", "project", "short", "uuid")).lower()
        meta_hit = all(t in hay for t in terms)
        th = text.get(r.get("uuid") or "")
        if not meta_hit and not th:
            continue
        cls = ("live" if r.get("live") else "ret" if r.get("retired")
               else "queue" if r.get("state") == "dead" else "idle")
        res.append({
            "short": r.get("short"), "title": r.get("title") or r.get("short"),
            "sub": _sub(r.get("summary") or r.get("name") or "", terms),
            "project": r.get("project") or "", "la_ts": r.get("la_ts") or 0,
            "cls": cls, "busy": bool(r.get("busy")),
            "meta": meta_hit, "n": th["n"] if th else 0,
            "hits": th["hits"] if th else []})
    res.sort(key=lambda x: (0 if x["meta"] else 1, -(x["la_ts"] or 0)))
    return {"ok": True, "q": q, "terms": terms, "n": len(res), "results": res[:limit],
            "ms": int((time.time() - t0) * 1000), "upd_ms": int((t1 - t0) * 1000)}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        t = time.time()
        print("added", update(), "in %.1fs" % (time.time() - t))
    elif len(sys.argv) > 1:
        t = time.time()
        found, terms = search_text(" ".join(sys.argv[1:]))
        for u, v in sorted(found.items(), key=lambda kv: -kv[1]["last"])[:20]:
            print(u[:8], v["n"], v["hits"][0]["snip"][:120] if v["hits"] else "")
        print(len(found), "sessions, %.0f ms" % ((time.time() - t) * 1000))
    else:
        print("usage: rc_search.py --update | <query>")
