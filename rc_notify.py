#!/usr/bin/env python3
"""rc_notify - one alert line to the owner.

Sends to Telegram when <state>/telegram.env (or the environment) has
TELEGRAM_BOT_TOKEN and RC_BOT_CHAT; always appends to <state>/alerts.log, so
an install without Telegram still keeps a record. Never raises.

    python3 rc_notify.py "text"      # from a shell
    rc_notify.send("text")           # from Python
"""
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402

LOG = CFG.state("alerts.log")
MAX_LOG = 1024 * 1024


def _log(line):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def send(text, timeout=15):
    """Deliver `text`; returns True when Telegram accepted it."""
    text = (text or "").strip()
    if not text:
        return False
    _log(text.replace("\n", " | ")[:1000])
    token, chat = CFG.telegram()
    if not token:
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = r.status == 200
    except Exception as exc:  # network down, bad token: the log line stays
        _log("telegram send failed: %s" % type(exc).__name__)
        return False
    return ok


if __name__ == "__main__":
    sys.exit(0 if send(" ".join(sys.argv[1:])) else 1)
