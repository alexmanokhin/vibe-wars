#!/usr/bin/env python3
"""Generate the launchd agents (macOS) for this machine and this user.

launchd neither expands `~` nor inherits the login environment, so the plists
are written with absolute paths for the current $HOME. The scripts run from
this repo (the directory of this file) with /usr/bin/python3.

The generator only writes files (or prints them with --dry-run). It does not
load anything: copy the plists to ~/Library/LaunchAgents and bootstrap them
yourself once `claude` is signed in on this machine and the work folder is
trusted (run `claude` there once and accept the dialog). A plist installed too
early would start on the next login against an unprepared machine.

    /usr/bin/python3 make_plists.py --dry-run
    /usr/bin/python3 make_plists.py --workdir ~/rc-work --max-live 8 --with-bot
    /usr/bin/python3 make_plists.py --uninstall      # prints the removal commands

The panel listens on 127.0.0.1 by default. To reach it from a phone use
`tailscale serve` (see INSTALL.md) rather than binding a public address.
"""

import argparse
import os
import plistlib
import sys

# Rename the whole install in one place: labels are com.<NAME>.<unit>.
NAME = "vibe-wars"
LABEL_PREFIX = "com." + NAME

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
USER = os.path.basename(HOME)
PY = "/usr/bin/python3"
# A missing PATH entry is harmless, so /opt/homebrew/bin stays even without
# Homebrew installed.
PATH = "%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" % HOME
# The daemon log: rc_guardian and claude-rc-health.sh read it from here.
DAEMON_LOGDIR = os.path.join(HOME, "Library/Logs")

UNITS = ("rc-guardian", "rc-panel", "rc-load-guard", "claude-rc-health",
         "rc-bot", "remote-control")


def label(unit):
    return "%s.%s" % (LABEL_PREFIX, unit)


def jobs(args, cfg):
    logdir = os.path.join(cfg.STATE_DIR, "logs")
    env = {"PATH": PATH, "HOME": HOME, "USER": USER,
           "RC_WORKDIR": args.workdir,
           "RC_PANEL_BIND": args.bind,
           "RC_PANEL_PORT": str(args.port),
           "RC_NAME_PREFIX": args.prefix,
           "RC_LABEL_PREFIX": LABEL_PREFIX,
           "RC_UNIT_PREFIX": NAME}
    if args.max_live:
        # Every unit gets the cap, not only the guardian: the panel, the bot and
        # the load guard import rc_guardian and read the same variable.
        env["RC_MAX_LIVE"] = str(args.max_live)
    if args.state_dir:
        env["RC_STATE_DIR"] = cfg.STATE_DIR
    if args.default_provider:
        env["RC_DEFAULT_PROVIDER"] = args.default_provider

    def py(unit, script, **extra):
        body = {
            "Label": label(unit),
            "ProgramArguments": [PY, os.path.join(HERE, script)],
            "RunAtLoad": True,
            "WorkingDirectory": HERE,
            "StandardOutPath": os.path.join(logdir, unit + ".out.log"),
            "StandardErrorPath": os.path.join(logdir, unit + ".err.log"),
            "EnvironmentVariables": dict(env),
        }
        body.update(extra)
        return body

    out = {}
    out[label("rc-guardian")] = py("rc-guardian", "rc_guardian.py", StartInterval=60)
    out[label("rc-panel")] = py("rc-panel", "rc_panel.py", KeepAlive=True, ThrottleInterval=10)
    out[label("rc-load-guard")] = py("rc-load-guard", "rc_load_guard.py", StartInterval=60)
    out[label("claude-rc-health")] = {
        "Label": label("claude-rc-health"),
        "ProgramArguments": ["/bin/bash", os.path.join(HERE, "claude-rc-health.sh")],
        "StartInterval": 120,
        "RunAtLoad": True,
        "WorkingDirectory": HERE,
        "EnvironmentVariables": dict(env),
        "StandardOutPath": os.path.join(logdir, "claude-rc-health.log"),
        "StandardErrorPath": os.path.join(logdir, "claude-rc-health.log"),
    }
    if args.with_daemon:
        out[label("remote-control")] = {
            "Label": label("remote-control"),
            "ProgramArguments": ["/bin/bash", os.path.join(HERE, "claude-rc-launcher.sh")],
            "RunAtLoad": True,
            # Not KeepAlive: true. A daemon stopped on purpose (an account
            # switch, for example) must not come back in the middle of it.
            "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
            "ThrottleInterval": 30,
            # Never the home folder: Claude Code refuses to start there.
            "WorkingDirectory": args.workdir,
            "StandardOutPath": os.path.join(DAEMON_LOGDIR, "claude-rc.log"),
            "StandardErrorPath": os.path.join(DAEMON_LOGDIR, "claude-rc.err"),
            "EnvironmentVariables": dict(env),
            "ProcessType": "Interactive",
        }
    if args.with_bot:
        # Telegram allows one getUpdates consumer per token: a second bot on the
        # same token steals updates from the first. Hence opt-in.
        token, _chat = cfg.telegram()
        if token:
            out[label("rc-bot")] = py("rc-bot", "rc_bot.py", KeepAlive=True, ThrottleInterval=10)
        else:
            sys.stderr.write("--with-bot skipped: Telegram is not configured (put "
                             "TELEGRAM_BOT_TOKEN and RC_BOT_CHAT in %s)\n" % cfg.TELEGRAM_ENV)
    return out


def print_uninstall():
    print("# stop and remove every %s agent:" % LABEL_PREFIX)
    for u in UNITS:
        print("launchctl bootout gui/$(id -u)/%s 2>/dev/null" % label(u))
    print("rm -f ~/Library/LaunchAgents/%s.*.plist" % LABEL_PREFIX)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="panel listen address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--default-provider", default="",
                    help="no Claude subscription here: new sessions run on this provider (codex, vibe or minimax)")
    ap.add_argument("--max-live", type=int, default=0,
                    help="cap on live sessions; without it the code default applies")
    ap.add_argument("--workdir", default="",
                    help="session work folder, not the home folder (default ~/rc-work)")
    ap.add_argument("--prefix", default=NAME,
                    help="session name prefix shown in the Claude app (default %s)" % NAME)
    ap.add_argument("--state-dir", default="",
                    help="state folder (sets RC_STATE_DIR in every unit)")
    ap.add_argument("--out", default="",
                    help="where to write the plists (default <state dir>/launchd; "
                         "not ~/Library/LaunchAgents, installing is a separate step)")
    ap.add_argument("--with-bot", action="store_true",
                    help="add the Telegram bot (only when Telegram is configured)")
    ap.add_argument("--with-daemon", action="store_true",
                    help="add the `claude remote-control` daemon")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plists instead of writing them")
    ap.add_argument("--uninstall", action="store_true",
                    help="print the commands that remove the agents, then exit")
    args = ap.parse_args()

    if args.uninstall:
        print_uninstall()
        return 0
    if args.workdir:
        os.environ["RC_WORKDIR"] = os.path.expanduser(args.workdir)
    if args.state_dir:
        os.environ["RC_STATE_DIR"] = os.path.expanduser(args.state_dir)
    sys.path.insert(0, HERE)
    import rc_config as cfg  # noqa: E402  (after the env overrides above)

    args.workdir = cfg.WORKDIR
    if args.workdir == os.path.realpath(HOME):
        ap.error("the work folder cannot be the home folder: claude does not start there")
    out_dir = os.path.expanduser(args.out) if args.out else os.path.join(cfg.STATE_DIR, "launchd")

    made = jobs(args, cfg)
    if args.dry_run:
        for name, body in sorted(made.items()):
            print("==> %s.plist" % os.path.join(out_dir, name))
            sys.stdout.write(plistlib.dumps(body).decode())
            print()
        return 0

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(cfg.STATE_DIR, "logs"), exist_ok=True)
    for name, body in sorted(made.items()):
        path = os.path.join(out_dir, name + ".plist")
        with open(path, "wb") as fh:
            plistlib.dump(body, fh)
        print(path)
    print("\nuser     : %s\nrepo     : %s\nworkdir  : %s\nplists   : %d"
          % (USER, HERE, args.workdir, len(made)))
    print("\ninstall (once claude is signed in here):")
    print("  cp %s/%s.*.plist ~/Library/LaunchAgents/" % (out_dir, LABEL_PREFIX))
    print("  for p in ~/Library/LaunchAgents/%s.*.plist; do "
          "launchctl bootstrap gui/$(id -u) \"$p\"; done" % LABEL_PREFIX)
    print("\nremove: /usr/bin/python3 %s --uninstall" % os.path.basename(__file__))
    return 0


if __name__ == "__main__":
    sys.exit(main())
