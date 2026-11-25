#!/usr/bin/env python3
"""Generate the systemd user units (Linux) for this machine and this user.

The Linux twin of make_plists.py. Units do not inherit the login environment,
so they are written with absolute paths for the current $HOME; the scripts run
from this repo (the directory of this file).

The generator only writes files (or prints them with --dry-run). Installing is
a separate step, once `claude` is signed in and the work folder is trusted:
copy the files to ~/.config/systemd/user and `systemctl --user enable --now`.

    python3 make_units.py --dry-run
    python3 make_units.py --workdir ~/rc-work --max-live 8 --with-bot
    python3 make_units.py --uninstall      # prints the removal commands

Linux specifics:
- Periodic jobs (guardian, load guard, health) are a oneshot service + timer.
- Sessions the guardian starts must not live in the cgroup of its oneshot unit
  (systemd would kill them when the pass ends), so rc_guardian moves each one
  into its own scope with `systemd-run --user --scope`. That needs the user
  manager to outlive the ssh login: `loginctl enable-linger $USER`.
- The daemon logs to ~/.claude/logs (rc_guardian and claude-rc-health.sh read
  it there).
- The panel listens on 127.0.0.1 by default; reach it with `tailscale serve`.
- --memory-high prints a MemoryHigh command for the user slice, so sessions
  are throttled before they squeeze other services on a shared machine.
"""

import argparse
import os
import sys

# Rename the whole install in one place: units are <NAME>-<unit>.service.
NAME = "vibe-wars"

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
USER = os.path.basename(HOME)
PY = "/usr/bin/python3"
PATH = "%s/.local/bin:/usr/local/bin:/usr/bin:/bin" % HOME
DAEMON_LOGDIR = os.path.join(HOME, ".claude/logs")

TIMED = ("rc-guardian", "rc-load-guard", "claude-rc-health")
SERVICES = ("rc-panel", "rc-bot", "remote-control")


def unit(name, kind="service"):
    return "%s-%s.%s" % (NAME, name, kind)


def env_lines(d):
    return "".join('Environment="%s=%s"\n' % (k, v) for k, v in d.items())


def oneshot(desc, exec_start, log, every, env):
    service = (
        "[Unit]\nDescription=%s\n\n[Service]\nType=oneshot\n"
        "ExecStart=%s\nWorkingDirectory=%s\n%s"
        "StandardOutput=append:%s\nStandardError=append:%s\n"
        # One pass must not hang forever (ssh/curl inside can stall on a
        # network drop).
        "TimeoutStartSec=300\n"
        % (desc, exec_start, HERE, env, log, log))
    timer = (
        "[Unit]\nDescription=%s (timer)\n\n[Timer]\nOnBootSec=30\n"
        "OnUnitActiveSec=%ds\nAccuracySec=5s\n\n[Install]\nWantedBy=timers.target\n"
        % (desc, every))
    return service, timer


def daemon(desc, exec_start, workdir, restart, env, out_log, err_log):
    return (
        "[Unit]\nDescription=%s\nAfter=network-online.target\n\n"
        "[Service]\nExecStart=%s\nWorkingDirectory=%s\n"
        "Restart=%s\nRestartSec=10\n%s"
        "StandardOutput=append:%s\nStandardError=append:%s\n\n"
        "[Install]\nWantedBy=default.target\n"
        % (desc, exec_start, workdir, restart, env, out_log, err_log))


def units(args, cfg):
    logdir = os.path.join(cfg.STATE_DIR, "logs")
    d = {"PATH": PATH, "HOME": HOME, "USER": USER,
         "RC_WORKDIR": args.workdir,
         "RC_PANEL_BIND": args.bind,
         "RC_PANEL_PORT": str(args.port),
         "RC_NAME_PREFIX": args.prefix,
         "RC_LABEL_PREFIX": "com." + NAME,
         "RC_UNIT_PREFIX": NAME}
    if args.max_live:
        # Every unit gets the cap: the panel, the bot and the load guard import
        # rc_guardian and read the same variable.
        d["RC_MAX_LIVE"] = str(args.max_live)
    if args.state_dir:
        d["RC_STATE_DIR"] = cfg.STATE_DIR
    env = env_lines(d)

    out = {}
    for name, desc, cmd, every in (
            ("rc-guardian", "rc-guardian: session registry and restore",
             "%s %s/rc_guardian.py" % (PY, HERE), 60),
            ("rc-load-guard", "rc-load-guard: shed sessions under overload",
             "%s %s/rc_load_guard.py" % (PY, HERE), 60),
            ("claude-rc-health", "claude-rc-health: remote-control daemon watchdog",
             "/bin/bash %s/claude-rc-health.sh" % HERE, 120)):
        s, t = oneshot(desc, cmd, os.path.join(logdir, name + ".log"), every, env)
        out[unit(name)], out[unit(name, "timer")] = s, t

    out[unit("rc-panel")] = daemon(
        "rc-panel: session dashboard", "%s %s/rc_panel.py" % (PY, HERE), HERE,
        "always", env, os.path.join(logdir, "rc-panel.out.log"),
        os.path.join(logdir, "rc-panel.err.log"))

    if args.with_daemon:
        # Restart=on-failure, not always: a daemon stopped on purpose (an
        # account switch) must not come back in the middle of it. Never the
        # home folder as WorkingDirectory: Claude Code refuses to start there.
        out[unit("remote-control")] = daemon(
            "claude remote-control daemon", "/bin/bash %s/claude-rc-launcher.sh" % HERE,
            args.workdir, "on-failure", env,
            os.path.join(DAEMON_LOGDIR, "claude-rc.log"),
            os.path.join(DAEMON_LOGDIR, "claude-rc.err"))

    if args.with_bot:
        # Telegram allows one getUpdates consumer per token. Hence opt-in.
        token, _chat = cfg.telegram()
        if token:
            out[unit("rc-bot")] = daemon(
                "rc-bot: Telegram control", "%s %s/rc_bot.py" % (PY, HERE), HERE,
                "always", env, os.path.join(logdir, "rc-bot.out.log"),
                os.path.join(logdir, "rc-bot.err.log"))
        else:
            sys.stderr.write("--with-bot skipped: Telegram is not configured (put "
                             "TELEGRAM_BOT_TOKEN and RC_BOT_CHAT in %s)\n" % cfg.TELEGRAM_ENV)
    return out


def print_uninstall():
    names = [unit(n, "timer") for n in TIMED] + [unit(n) for n in SERVICES]
    udir = os.path.join(HOME, ".config/systemd/user")
    names = [n for n in names if os.path.exists(os.path.join(udir, n))]
    print("# stop every installed %s unit, then the sessions the panel started:" % NAME)
    for n in names:
        print("systemctl --user disable --now %s" % n)
    print("%s %s --stop-sessions" % (PY, os.path.join(HERE, "rc_guardian.py")))
    print("rm -f ~/.config/systemd/user/%s-*.service ~/.config/systemd/user/%s-*.timer"
          % (NAME, NAME))
    print("systemctl --user daemon-reload")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="panel listen address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--max-live", type=int, default=0,
                    help="cap on live sessions; without it the code default applies")
    ap.add_argument("--workdir", default="",
                    help="session work folder, not the home folder (default ~/rc-work)")
    ap.add_argument("--prefix", default=NAME,
                    help="session name prefix shown in the Claude app (default %s)" % NAME)
    ap.add_argument("--state-dir", default="",
                    help="state folder (sets RC_STATE_DIR in every unit)")
    ap.add_argument("--out", default="",
                    help="where to write the units (default <state dir>/systemd; "
                         "not ~/.config/systemd/user, installing is a separate step)")
    ap.add_argument("--with-bot", action="store_true",
                    help="add the Telegram bot (only when Telegram is configured)")
    ap.add_argument("--with-daemon", action="store_true",
                    help="add the `claude remote-control` daemon")
    ap.add_argument("--memory-high", default="",
                    help="memory ceiling for the whole user slice, e.g. 10G; "
                         "prints the root command, does not apply it")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the units instead of writing them")
    ap.add_argument("--uninstall", action="store_true",
                    help="print the commands that remove the units, then exit")
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
    out_dir = os.path.expanduser(args.out) if args.out else os.path.join(cfg.STATE_DIR, "systemd")

    made = units(args, cfg)
    if args.dry_run:
        for name, body in sorted(made.items()):
            print("==> %s" % os.path.join(out_dir, name))
            sys.stdout.write(body)
            print()
        return 0

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(cfg.STATE_DIR, "logs"), exist_ok=True)
    for name, body in sorted(made.items()):
        path = os.path.join(out_dir, name)
        with open(path, "w") as fh:
            fh.write(body)
        print(path)
    timers = " ".join(unit(n, "timer") for n in TIMED)
    extra = " ".join(unit(n) for n in ("remote-control", "rc-bot") if unit(n) in made)
    print("\nuser     : %s\nrepo     : %s\nworkdir  : %s\nunits    : %d"
          % (USER, HERE, args.workdir, len(made)))
    print("\ninstall (once claude is signed in here):")
    print("  mkdir -p ~/.config/systemd/user %s" % DAEMON_LOGDIR)
    print("  cp %s/%s-* ~/.config/systemd/user/" % (out_dir, NAME))
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now %s %s %s"
          % (unit("rc-panel"), timers, extra))
    print("\nonce, as root:")
    print("  loginctl enable-linger %s" % USER)
    if args.memory_high:
        print("  systemctl set-property user-%d.slice MemoryHigh=%s"
              % (os.getuid(), args.memory_high))
    print("\nremove: python3 %s --uninstall" % os.path.basename(__file__))
    return 0


if __name__ == "__main__":
    sys.exit(main())
