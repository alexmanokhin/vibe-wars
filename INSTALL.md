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
- The coding agent. You may be Claude Code, Codex or Mistral Vibe; the user
  needs at least one paid subscription:
  - Claude Code: `claude --version`; signed in if `claude -p "reply ok"` from
    a folder that is not the home folder prints `ok` (not `Not logged in`).
    If `echo "$ANTHROPIC_BASE_URL"` contains `minimax`, this Claude Code runs on
    a MiniMax plan key: there is no Claude subscription (MiniMax mode).
  - Codex (ChatGPT subscription): `codex --version`, signed in with ChatGPT.
  - Mistral Vibe (Vibe plan): `vibe --version`, set up with a Vibe key.
  If none of them works, ASK the user which subscription they have and stop.
  Without a Claude subscription do the "Other models" section below as part
  of the install (it sets the default for new sessions).
- Tailscale (only for phone access): `tailscale status` (macOS app CLI:
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`). If missing,
  phone access is skipped; everything else still works.

## 2. Pick the settings

Use these defaults unless the user asks otherwise; do not ask about them.

- Work folder: `~/rc-work` (sessions run there; must not be the home folder).
- Port: `8788`. If `lsof -nP -iTCP:8788 -sTCP:LISTEN` (Linux:
  `ss -ltn 'sport = :8788'`) shows a listener, use the next free port from 8789.
- Live session cap: 8 GB RAM -> 6, 16 GB -> 12, 32 GB+ -> 20.

If the user mentioned Telegram, install everything else first and then do the
"Telegram" section below.

## 3. Prepare the work folder

```
mkdir -p ~/rc-work
```

Claude Code must trust the folder once, or every session the panel starts
hangs on the trust question. (Codex or Mistral Vibe only, no Claude Code on
this computer: skip this step; the panel starts them with their own trust
flags.) If you (this Claude Code) were started in
`~/rc-work`, it is already trusted: go on. Otherwise ASK the user to open a
second terminal, run `cd ~/rc-work && claude`, answer "Yes, I trust this
folder", type `/exit`, and tell you when done.

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
for i in $(seq 1 20); do c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:PORT/api/state); [ "$c" = 401 ] && break; python3 -c 'import time; time.sleep(1)'; done; echo "$c"   # 401 = up and protected
cd REPO && RC_PANEL_PORT=PORT /usr/bin/python3 rc_panel.py --url              # login URL with the token
curl -s -o /dev/null -w '%{http_code}\n' "$(RC_PANEL_PORT=PORT /usr/bin/python3 rc_panel.py --url | head -1)"  # 302
tail -n 20 ~/.vibe-wars/rc-guardian.log
```

If the panel does not answer: macOS `launchctl print gui/$(id -u)/com.vibe-wars.rc-panel | head -30`
and `ls ~/.vibe-wars/logs`; Linux `journalctl --user -u vibe-wars-rc-panel -n 50`.

Print the full URL from `--url` in your reply, as a link the user can click:
it carries their own access token for their own computer, and showing it to
them is the point of this step (do not make them run a command for it). Tell
them to open it once in the browser on this computer; the browser then
remembers the token. Then tell them: in the panel, the HQ link (or `/play`)
opens the game view; RECRUIT starts a new Claude session.

## Other models (Codex, Mistral Vibe, MiniMax)

Sessions can run on other subscriptions besides Claude. Each appears in the
model list of RECRUIT once set up. Only subscriptions, no per-token API keys.

- **Codex** (ChatGPT Plus or Pro): needs the Codex CLI signed in with ChatGPT.
  If `codex --version` fails, install it (`npm install -g @openai/codex`; on a
  Mac also `brew install codex`), then ASK the user to run `codex` once in a
  terminal and sign in with ChatGPT. Nothing to configure in Vibe Wars.
- **Mistral Vibe** (Vibe plan, runs DeepSeek V4.1 Flash): needs the Vibe CLI
  (`vibe --version`; install: `curl -LsSf https://mistral.ai/vibe/install.sh | bash`)
  and a Vibe key. If Vibe is already set up (`vibe --setup`), the key is
  `MISTRAL_API_KEY` in `~/.vibe/.env`: use it, do not ask. Otherwise ASK the
  user to create one at https://chat.mistral.ai/code/extensions?focus=key and
  paste it. Check it:
  `curl -s https://api.mistral.ai/v1/billing/budget -H "Authorization: Bearer <key>"`
  must show `vibe_budget`. Write `MISTRAL_VIBE_API_KEY=<key>` into
  `~/.vibe-wars/providers/keys.env` (folder mode 700, file mode 600).
- **MiniMax** (Token Plan): ASK for the plan key (platform.minimax.io, Token
  Plan). Check it with one call:
  `curl -s https://api.minimax.io/anthropic/v1/messages -H "x-api-key: <key>" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"MiniMax-M3","max_tokens":20,"messages":[{"role":"user","content":"say ok"}]}'`
  must answer with text. Write `MINIMAX_API_KEY=<key>` into the same keys.env.
  MiniMax sessions run in Claude Code, so the work folder must be trusted
  (step 3).

No Claude subscription: add `--default-provider codex` (or `vibe`, `minimax`)
to the step 4a or 4b command, so new sessions and the Telegram bot use it.

Adding one later: do the matching item above, re-run step 4a/4b with the same
settings (as in "Updating") and restart the panel. Verify: in HQ, RECRUIT
lists the model. Tell the user these sessions have no link in the Claude app;
they live in the panel, on the phone through Tailscale and in the Telegram bot.

## 6. Phone access through Tailscale (optional)

On Linux, `tailscale serve` as a normal user needs one-time permission. If it
says access denied (or before running it), ASK the user to type
`! sudo tailscale set --operator=$USER` in this prompt, then continue.

```
tailscale serve --bg PORT
tailscale serve status
```

If it prints "Serve is not enabled on your tailnet" with a link: give the user
the link and tell them to leave "HTTPS certificates" checked and UNCHECK
"Tailscale Funnel" (checked by default; Funnel would open things to the
internet), then press the button. Run the command again after they say done.

`tailscale serve status` shows `https://<machine>.<tailnet>.ts.net`. Tell the
user to open `https://<machine>.<tailnet>.ts.net/?token=<token>` on the phone
once (the phone must be signed in to the same Tailscale account). If the phone
gets 403, add that host name to `RC_ALLOWED_HOSTS` in the panel unit's
environment and restart the panel.

## Telegram (optional: when the user asks for it)

The bot sends alerts and lets the user check and control sessions from
Telegram. It needs a bot token and the user's chat id; the user only gives the
token, you find the chat id.

1. If the user has no token yet, tell them: in Telegram open @BotFather, send
   `/newbot`, pick a name and a username ending in `bot`, and paste the token
   it replies with here. Wait for the token.
2. Check it: `curl -s https://api.telegram.org/bot<TOKEN>/getMe` must show
   `"ok":true`. Note `result.username`.
3. Tell the user: open `https://t.me/<username>`, press Start, and say when
   done. Then read the chat id:

   ```
   curl -s https://api.telegram.org/bot<TOKEN>/getUpdates | python3 -c 'import json,sys; r=[u["message"]["chat"] for u in json.load(sys.stdin)["result"] if "message" in u and u["message"]["chat"]["type"]=="private"]; print(r[-1]["id"] if r else "none")'
   ```

   `none` means no message arrived yet: ask them to send the bot any text and
   run it again. (Do this before the bot service runs: once it runs, it takes
   the updates itself.)
4. Write `~/.vibe-wars/telegram.env` with mode 600:

   ```
   TELEGRAM_BOT_TOKEN=<token>
   RC_BOT_CHAT=<chat id>
   ```

5. Re-run the step 4a or 4b command with the same settings as before plus
   `--with-bot` (read the settings from the existing plist or unit, as in
   "Updating"), then start the bot:
   - macOS: `cp ~/.vibe-wars/launchd/com.vibe-wars.rc-bot.plist ~/Library/LaunchAgents/`
     and `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vibe-wars.rc-bot.plist`.
   - Linux: `cp ~/.vibe-wars/systemd/vibe-wars-rc-bot.service ~/.config/systemd/user/`,
     `systemctl --user daemon-reload`, `systemctl --user enable --now vibe-wars-rc-bot.service`.
6. Tell the user to send `/status` to the bot: it answers within a few
   seconds. `/help` lists the commands.

## Updating (when the user asks to update Vibe Wars)

```
cd REPO && git pull --ff-only
```

Then re-run the step 4a or 4b command with the same settings as before (read
them from the existing plist or unit: `--workdir`, `--port`, `--max-live`,
plus `--with-bot` / `--with-daemon` if present) and restart the panel:

- macOS: `cp ~/.vibe-wars/launchd/com.vibe-wars.*.plist ~/Library/LaunchAgents/`,
  then for each plist `launchctl bootout gui/$(id -u) "$p"` and
  `launchctl bootstrap gui/$(id -u) "$p"`.
- Linux: `systemctl --user daemon-reload && systemctl --user restart vibe-wars-rc-panel.service`,
  and if the bot is installed also `systemctl --user restart vibe-wars-rc-bot.service`.

The other services run on timers and pick up the new code by themselves.
Run step 5 again and tell the user what changed (`git log --oneline -5`).

## 7. Report to the user

Tell them, in short: the local URL, the phone URL (if set up), where the token
is (`~/.vibe-wars/panel-token`), and how to uninstall:

- macOS: `for p in ~/Library/LaunchAgents/com.vibe-wars.*.plist; do launchctl bootout gui/$(id -u) "$p"; rm "$p"; done`,
  then `/usr/bin/python3 REPO/rc_guardian.py --stop-sessions` (stops the sessions the panel started).
- Linux: `python3 make_units.py --uninstall` prints the commands; run them all.
- Then `tailscale serve reset` (if used), remove `~/.vibe-wars` and REPO.
  `loginctl disable-linger "$USER"` on Linux if nothing else needs it.
