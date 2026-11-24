# INSTALL.md - instructions for Claude Code

You are Claude Code, installing this dashboard for the user on this computer.
Work through the steps in order. Ask the user only where a step says ASK.
Explain each step in one short plain sentence: the user may be a beginner.
Never open the panel to the internet (no Tailscale Funnel, no public bind,
no port forwarding): it starts Claude Code sessions with full access to this
computer.

The repo folder (where this file is) is called REPO below.

If you are reading this from the web and the repo is not on this computer yet,
clone it first: `git clone https://github.com/alexmanokhin/vibe-wars ~/vibe-wars`,
then REPO is `~/vibe-wars`.

## 1. Check prerequisites

Run and check each; stop and tell the user exactly what to install if one fails.

- OS: `uname -s` is `Darwin` (macOS) or `Linux`.
- git: `git --version`.
- Python: on macOS `/usr/bin/python3 --version` (3.9 or newer; if missing, tell
  the user to run `xcode-select --install` and wait for it). On Linux
  `python3 --version`.
- Claude Code: `claude --version`. Signed in: `claude -p "reply ok"` from a
  folder that is not the home folder prints `ok` (not `Not logged in`). If not
  signed in, ASK the user to run `claude` in a terminal and sign in with their
  paid Claude subscription, then continue.
- Tailscale (only for phone access): `tailscale status` (macOS app CLI:
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`). If missing,
  phone access is skipped; everything else still works.

## 2. Pick the settings

Use these defaults unless the user asks otherwise; do not ask about them.

- Work folder: `~/rc-work` (sessions run there; must not be the home folder).
- Port: `8788`. If `lsof -nP -iTCP:8788 -sTCP:LISTEN` (Linux:
  `ss -ltn 'sport = :8788'`) shows a listener, use the next free port from 8789.
- Live session cap: 8 GB RAM -> 6, 16 GB -> 12, 32 GB+ -> 20.

ASK one question only if the user mentioned Telegram: the bot token and their
numeric chat id. Write them to `~/.vibe-wars/telegram.env` (mode 600):

```
TELEGRAM_BOT_TOKEN=...
RC_BOT_CHAT=...
```

## 3. Prepare the work folder

```
mkdir -p ~/rc-work
```

Claude Code must trust the folder once. ASK the user to run, in their own
terminal: `cd ~/rc-work && claude`, accept the trust question, then type `/exit`.

## 4a. Install on macOS (launchd)

```
cd REPO
/usr/bin/python3 make_plists.py --workdir ~/rc-work --port PORT --max-live CAP
cp ~/.vibe-wars/launchd/com.vibe-wars.*.plist ~/Library/LaunchAgents/
for p in ~/Library/LaunchAgents/com.vibe-wars.*.plist; do launchctl bootstrap gui/$(id -u) "$p"; done
```

Add `--with-bot` if Telegram was configured. Add `--with-daemon` to also run
`claude remote-control` (needed only for chats started from the Claude app).

## 4b. Install on Linux (systemd user units)

```
cd REPO
python3 make_units.py --workdir ~/rc-work --port PORT --max-live CAP
```

The script prints where it wrote the units and the commands to enable them;
run those (`systemctl --user daemon-reload`, `systemctl --user enable --now ...`).
Then `loginctl enable-linger "$USER"` so the services keep running after logout
(it may need `sudo`; ASK before using sudo).

## 5. Verify

```
sleep 5
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:PORT/api/state      # 401 = up and protected
cd REPO && RC_PANEL_PORT=PORT /usr/bin/python3 rc_panel.py --url              # login URL with the token
curl -s -o /dev/null -w '%{http_code}\n' "$(RC_PANEL_PORT=PORT /usr/bin/python3 rc_panel.py --url | head -1)"  # 302
tail -n 20 ~/.vibe-wars/rc-guardian.log
```

If the panel does not answer: macOS `launchctl print gui/$(id -u)/com.vibe-wars.rc-panel | head -30`
and `ls ~/.vibe-wars/logs`; Linux `journalctl --user -u vibe-wars-rc-panel -n 50`.

Show the user the URL printed by `--url` and tell them to open it once in the
browser on this computer; the token is then remembered by the browser.

## 6. Phone access through Tailscale (optional)

```
tailscale serve --bg PORT
tailscale serve status
```

`tailscale serve status` shows `https://<machine>.<tailnet>.ts.net`. Tell the
user to open `https://<machine>.<tailnet>.ts.net/?token=<token>` on the phone
once (the phone must be signed in to the same Tailscale account). If the phone
gets 403, add that host name to `RC_ALLOWED_HOSTS` in the panel unit's
environment and restart the panel.

## 7. Report to the user

Tell them, in short: the local URL, the phone URL (if set up), where the token
is (`~/.vibe-wars/panel-token`), and how to uninstall:

- macOS: `for p in ~/Library/LaunchAgents/com.vibe-wars.*.plist; do launchctl bootout gui/$(id -u) "$p"; rm "$p"; done`
- Linux: `python3 make_units.py --uninstall` prints the commands.
- Then `tailscale serve reset` (if used) and remove `~/.vibe-wars`.
