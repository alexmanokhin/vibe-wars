#!/usr/bin/env python3
"""An orchestrator agent hands tasks to its executor agent (26.11).

RECRUIT can launch two sessions as a pair: the orchestrator plans and checks,
the executor does the work. Each can run on any agent the panel knows (Claude
Code, Codex, Mistral Vibe, MiniMax). The orchestrator talks to its executor
through this script, from its own shell:

    rc_pair.py ask  <executor id> "task"     send a task, wait, print the answer
    rc_pair.py ask  <executor id> - <<'TASK'  the same, the task on stdin
    rc_pair.py wait <executor id>            keep waiting for the current task
    rc_pair.py read <executor id>            print the executor's last answer

A wait lasts --max seconds (100 by default, under the 2-minute default limit of
an agent's shell tool). When the executor is not done by then the script says
STILL WORKING and the orchestrator runs `wait` again: waiting is idempotent,
nothing is sent twice.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_control as C  # noqa: E402

BUSY = ("working", "compacting")


def _view(eid):
    t = C.transcript(eid, limit=400)
    return t, t.get("shown") or []


def _after_last_task(msgs):
    """Assistant texts after the newest user message, and whether one exists."""
    last_user = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            last_user = i
    if last_user is None:
        return [], False
    return [m.get("text") or "" for m in msgs[last_user + 1:]
            if m.get("role") == "assistant" and (m.get("text") or "").strip()], True


def wait(eid, max_s, started=False):
    """Wait until the executor finished its current task; print the answer.
    Returns 0 when done, 3 when still working, 4 when it waits on a dialog."""
    end = time.time() + max_s
    calm = 0
    seen_busy = started
    t0 = time.time()
    while True:
        t, msgs = _view(eid)
        kind = ((t.get("tui") or {}).get("kind") or "")
        if t.get("perm"):
            p = t["perm"] or {}
            print("WAITING ON A DIALOG: the executor shows a question it cannot pass by itself "
                  "(%s). The person answers it in the dashboard; run wait again after."
                  % ((p.get("q") or p.get("title") or "a permission prompt")[:200]))
            return 4
        busy = kind in BUSY
        seen_busy = seen_busy or busy
        texts, has_task = _after_last_task(msgs)
        last_role = (msgs[-1].get("role") if msgs else "")
        if not busy and texts and last_role == "assistant":
            calm += 1
        else:
            calm = 0
        # two calm reads in a row (6 s), so a pause between two steps is not taken for the end
        if calm >= 2 or (calm >= 1 and not seen_busy and time.time() - t0 > 20):
            print("EXECUTOR %s ANSWERED:\n" % eid[:8])
            print("\n\n".join(texts).strip())
            return 0
        if time.time() >= end:
            print("STILL WORKING: the executor %s has not finished yet%s. Run: python3 %s wait %s"
                  % (eid[:8], (" (it wrote: %s)" % texts[-1][:300]) if texts else "",
                     os.path.abspath(__file__), eid[:8]))
            return 3
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("ask", "wait", "read"))
    ap.add_argument("executor")
    ap.add_argument("task", nargs="?", default="")
    ap.add_argument("--max", type=int, default=100, help="seconds to wait (default 100)")
    a = ap.parse_args()
    eid = a.executor.strip()
    if a.cmd == "read":
        _, msgs = _view(eid)
        texts, _ = _after_last_task(msgs)
        print("\n\n".join(texts).strip() or "(no answer yet)")
        return 0
    if a.cmd == "wait":
        return wait(eid, a.max)
    task = sys.stdin.read() if a.task == "-" else a.task
    task = (task or "").strip()
    if not task:
        print("no task given", file=sys.stderr)
        return 2
    # a busy executor (still starting, or on the previous task) is waited for
    # first, within the same --max; the task goes out once it is free
    end = time.time() + a.max
    while True:
        t, _ = _view(eid)
        if ((t.get("tui") or {}).get("kind") or "") not in BUSY:
            break
        if time.time() >= end:
            print("BUSY: the executor %s is still on its previous work, the task was NOT sent. "
                  "Run the same ask command again in a minute." % eid[:8])
            return 3
        time.sleep(3)
    a.max = max(20, int(end - time.time()))
    try:
        res = C.act_say({"id": eid, "text": "[task from the orchestrator]\n" + task})
    except ValueError as exc:
        print("NOT SENT: %s" % exc)
        return 2
    msg = res.get("msg") if isinstance(res, dict) else res
    if "delivered" not in (msg or "") and "queued" not in (msg or ""):
        print("SENT, NOT CONFIRMED: %s" % (msg or "")[:300])
    return wait(eid, a.max, started=True)


if __name__ == "__main__":
    sys.exit(main())
