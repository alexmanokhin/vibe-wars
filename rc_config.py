"""rc_config - every path and setting the dashboard shares, in one place.

All modules import this instead of hard-coding locations. Everything can be
overridden with environment variables (the launchd / systemd units set them);
the defaults work for a single-user install.

Python 3.9 compatible (macOS /usr/bin/python3): no match, no X | Y.
"""
import os
import re
import shutil
import sys

# Working project name: rename here (and in make_plists.py / make_units.py).
NAME = "vibe-wars"
# Set when this computer has no Claude subscription: new sessions, titles and
# voice fixes run on that provider (rc_providers), and the Claude limit and
# account checks stay off. Empty = Claude, with providers as an option.
DEFAULT_PROVIDER = (os.environ.get("RC_DEFAULT_PROVIDER") or "").strip()

HOME = os.path.expanduser("~")
# Where this code lives (the cloned repo). Units run the scripts from here.
HERE = os.path.dirname(os.path.abspath(__file__))
IS_LINUX = sys.platform.startswith("linux")

# Our own state: registry, caches, logs, the panel token. Never inside the
# Claude Code config folder, so removing the dashboard is one `rm -r`.
STATE_DIR = os.path.realpath(os.path.expanduser(
    os.environ.get("RC_STATE_DIR") or "~/." + NAME))
# Per-session runtime files (FIFOs, pty logs, launch wrappers). Private to the
# user (0700): a FIFO in a shared /tmp would let anyone type into a session.
RUN_DIR = os.path.realpath(os.path.expanduser(
    os.environ.get("RC_RUN_DIR") or os.path.join(STATE_DIR, "run")))

# Claude Code's own config folder (transcripts live in <CLAUDE_HOME>/projects).
CLAUDE_HOME = os.path.realpath(os.path.expanduser(
    os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
CLAUDE_JSON = os.path.join(HOME, ".claude.json")


def _find_claude():
    p = os.environ.get("RC_CLAUDE")
    if p:
        return os.path.expanduser(p)
    native = os.path.join(HOME, ".local/bin/claude")
    if os.path.exists(native):
        return native
    return shutil.which("claude") or native


# The claude CLI the sessions run.
CLAUDE = _find_claude()

# Working folder of the sessions. Claude Code refuses to work in the home
# folder itself, so the default is a dedicated folder.
WORKDIR = os.path.realpath(os.path.expanduser(
    os.environ.get("RC_WORKDIR") or "~/rc-work"))
# Claude Code names a transcript folder after the cwd, every character that is
# not a letter or digit replaced by "-" (/Users/me/rc-work -> -Users-me-rc-work).
PROJ = os.path.join(CLAUDE_HOME, "projects", re.sub(r"[^A-Za-z0-9]", "-", WORKDIR))


def workdir_trusted(path=None):
    """True when Claude Code has the trust question answered for the work folder
    (or for a folder above it). An untrusted folder makes every new session hang
    on that question in the background, so the panel checks this first."""
    import json
    path = os.path.realpath(path or WORKDIR)
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    cands = [os.path.join(cfg_dir, ".claude.json")] if cfg_dir else []
    cands.append(os.path.join(HOME, ".claude.json"))
    for f in cands:
        try:
            with open(f) as fh:
                projects = json.load(fh).get("projects") or {}
        except (OSError, ValueError):
            continue
        p = path
        while True:
            if (projects.get(p) or {}).get("hasTrustDialogAccepted"):
                return True
            up = os.path.dirname(p)
            if up == p:
                break
            p = up
        return False
    return False


# Prefix of session names in the Claude app ("<prefix>-<name>").
NAME_PREFIX = os.environ.get("RC_NAME_PREFIX") or NAME

# Unit names: launchd labels on macOS, systemd user units on Linux.
LABEL_PREFIX = os.environ.get("RC_LABEL_PREFIX") or ("com." + NAME)
UNIT_PREFIX = os.environ.get("RC_UNIT_PREFIX") or NAME

# Panel listener. 127.0.0.1 by default; reach it from a phone with
# `tailscale serve` (see INSTALL.md), never by binding a public address.
PANEL_BIND = os.environ.get("RC_PANEL_BIND") or "127.0.0.1"
PANEL_PORT = int(os.environ.get("RC_PANEL_PORT") or "8788")
PANEL_TOKEN_FILE = os.path.join(STATE_DIR, "panel-token")

# Optional Telegram (bot + alerts). KEY=VALUE lines:
#   TELEGRAM_BOT_TOKEN=123:abc
#   RC_BOT_CHAT=<your numeric chat id>
TELEGRAM_ENV = os.path.join(STATE_DIR, "telegram.env")

# Extra folders /file may serve, besides WORKDIR and the sessions' own folders
# (colon-separated).
FILE_ROOTS = [os.path.realpath(os.path.expanduser(p))
              for p in (os.environ.get("RC_FILE_ROOTS") or "").split(":") if p]


def state(name):
    """Path of a state file, e.g. state("rc-registry.json")."""
    return os.path.join(STATE_DIR, name)


def run(name):
    """Path of a per-session runtime file, e.g. run("in-1234abcd.fifo")."""
    return os.path.join(RUN_DIR, name)


def ensure_dirs():
    for d in (STATE_DIR, RUN_DIR):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError:
            pass


def read_env_file(path):
    """KEY=VALUE lines (quotes and `export ` allowed) -> dict."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def telegram():
    """(token, chat_id) or (None, None) when Telegram is not configured."""
    env = read_env_file(TELEGRAM_ENV)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("RC_BOT_CHAT") or env.get("RC_BOT_CHAT")
    if not token or not chat:
        return None, None
    try:
        return token, int(chat)
    except ValueError:
        return None, None


ensure_dirs()
