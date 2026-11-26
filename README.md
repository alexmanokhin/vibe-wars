# Vibe Wars

A self-hosted dashboard for Claude Code Remote Control sessions: see every
session on your computer, start new ones, type into them, answer their
questions, watch limits and machine load, and switch accounts, from a browser
on the same computer or from your phone over Tailscale.

Not affiliated with Anthropic.

News, tips and new versions: [t.me/failblog](https://t.me/failblog).
Found a bug or something does not work: write to [t.me/alexmanokhin](https://t.me/alexmanokhin).

## Requirements

- A Mac (macOS 13 or newer) or a Linux machine that stays on while you work.
- One paid subscription with its coding agent: Claude (Pro or Max) with Claude Code,
  ChatGPT with Codex, or Mistral's Vibe plan with Vibe. A MiniMax Token Plan also
  works, through Claude Code.
- Python 3.9+ (on a Mac: the Command Line Tools, `xcode-select --install`) and git.
- Tailscale installed and signed in on the computer and on the phone (same account),
  only if you want to open the dashboard from the phone.
- Optional: Telegram, for alerts and the control bot (see below).

## Install

1. Open a terminal and start Claude Code in the folder your sessions will use:

   ```
   mkdir -p ~/rc-work && cd ~/rc-work && claude
   ```

   Claude Code asks whether you trust the folder: press the down arrow to
   "Yes, I trust this folder", then Enter.

2. Paste this into Claude Code:

   ```
   Install Vibe Wars for me: https://github.com/alexmanokhin/vibe-wars (follow its INSTALL.md)
   ```

No Claude subscription? In step 1 start your own agent in the same folder
instead, and paste the same line into it:

- ChatGPT: `mkdir -p ~/rc-work && cd ~/rc-work && codex --sandbox danger-full-access`
  (Codex's own sandbox blocks the install commands on many Linux machines; the flag
  lets it run them, as Claude Code does after you trust the folder)
- Mistral Vibe plan: `mkdir -p ~/rc-work && cd ~/rc-work && vibe`
- MiniMax Token Plan: `mkdir -p ~/rc-work && cd ~/rc-work && ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic ANTHROPIC_AUTH_TOKEN=<your MiniMax plan key> ANTHROPIC_MODEL=MiniMax-M3 claude`

Have more than one? Agents of every subscription you add show up in RECRUIT:
tell your agent `Add Codex to Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Other models")`
(or Mistral Vibe, or MiniMax).

Claude checks the prerequisites, asks only what it must, installs the
background services (launchd on macOS, systemd on Linux), starts them and
prints the address to open.

## Update

Tell Claude Code: `Update Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Updating")`.

## Phone access (optional)

Install Tailscale on the computer and the phone, sign in to the same account,
then tell Claude Code: `Set up phone access for Vibe Wars (follow ~/vibe-wars/INSTALL.md, step 6)`.

## Telegram (optional)

Alerts and session control from Telegram. Tell Claude Code:
`Connect a Telegram bot to Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Telegram")`.
It walks you through it: create a bot with @BotFather, paste its token, press
Start in the bot. Then `/status` in the bot shows your sessions.

## What runs

| Part | File | What it does |
|---|---|---|
| Panel | `rc_panel.py` | Web UI on `127.0.0.1:8788`: classic view at `/`, HQ view at `/play` |
| Guardian | `rc_guardian.py` | Every 60 s: keeps the session registry, restores sessions after restarts and account switches |
| Control library | `rc_control.py` | All session actions (launch, send, kill, retire, accounts) |
| Load guard | `rc_load_guard.py` | Stops restored sessions before the machine runs out of memory |
| Health | `claude-rc-health.sh` | Heals the Remote Control daemon |
| Bot (optional) | `rc_bot.py` | Telegram: status, start/stop sessions, text in |

State lives in `~/.vibe-wars` (override with `RC_STATE_DIR`). Remove it and the
services to uninstall (see INSTALL.md).

## Security

- The panel listens on 127.0.0.1 only. Every request needs the access token
  from `~/.vibe-wars/panel-token` (a cookie after the first visit).
- Phone access goes through `tailscale serve`, which is private to your tailnet.
  Never expose the panel with Tailscale Funnel or a public port: it can start
  Claude Code sessions with full access to your computer.
- Not in this version: moving sessions between machines, the multi-machine hub,
  serving files from disk, and the external ingest endpoint.

## Status

v0.1. Many code comments are still in Russian.

---

# Vibe Wars (по-русски)

Дашборд для сессий Claude Code Remote Control на своём компьютере: все сессии
в одном окне, запуск новых, сообщения в сессии, ответы на вопросы Claude,
лимиты, нагрузка машины, переключение аккаунтов. Открывается в браузере на
компьютере или с телефона через Tailscale.

Новости, советы и новые версии: [t.me/failblog](https://t.me/failblog).
Нашли баг или что-то не работает: пишите [t.me/alexmanokhin](https://t.me/alexmanokhin).

## Что нужно

- Mac (macOS 13+) или Linux-машина, которая включена, пока вы работаете.
- Одна платная подписка со своим агентом: Claude (Pro или Max) с Claude Code,
  ChatGPT с Codex или Vibe-план Mistral с Vibe. Подходит и MiniMax Token Plan,
  через Claude Code.
- Python 3.9+ (на Mac: Command Line Tools, `xcode-select --install`) и git.
- Tailscale на компьютере и на телефоне под одним аккаунтом, если нужен доступ с телефона.
- По желанию: Telegram для алертов и управления (см. ниже).

## Установка

1. Откройте терминал и запустите Claude Code в папке, где будут жить сессии:

   ```
   mkdir -p ~/rc-work && cd ~/rc-work && claude
   ```

   Claude Code спросит, доверяете ли вы папке: стрелкой вниз выберите
   «Yes, I trust this folder» и нажмите Enter.

2. Вставьте в Claude Code:

   ```
   Install Vibe Wars for me: https://github.com/alexmanokhin/vibe-wars (follow its INSTALL.md)
   ```

Нет подписки Claude? На шаге 1 запустите в той же папке своего агента и
вставьте в него ту же строку:

- ChatGPT: `mkdir -p ~/rc-work && cd ~/rc-work && codex --sandbox danger-full-access`
  (своя песочница Codex на многих Linux-машинах не даёт запускать команды установки;
  с этим флагом он их выполняет, как Claude Code в доверенной папке)
- Vibe-план Mistral: `mkdir -p ~/rc-work && cd ~/rc-work && vibe`
- MiniMax Token Plan: `mkdir -p ~/rc-work && cd ~/rc-work && ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic ANTHROPIC_AUTH_TOKEN=<ключ плана MiniMax> ANTHROPIC_MODEL=MiniMax-M3 claude`

Есть несколько подписок? Агенты каждой появятся в RECRUIT: скажите агенту
`Add Codex to Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Other models")`
(или Mistral Vibe, или MiniMax).

Claude проверит требования, задаст только нужные вопросы, установит фоновые
службы (launchd на Mac, systemd на Linux), запустит их и покажет адрес.

## Обновление

Скажите Claude Code: `Update Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Updating")`.

## Доступ с телефона (по желанию)

Установите Tailscale на компьютер и телефон, войдите в один аккаунт, затем
скажите Claude Code: `Set up phone access for Vibe Wars (follow ~/vibe-wars/INSTALL.md, step 6)`.

## Telegram (по желанию)

Алерты и управление сессиями из Telegram. Скажите Claude Code:
`Connect a Telegram bot to Vibe Wars (follow ~/vibe-wars/INSTALL.md, "Telegram")`.
Он проведёт по шагам: создать бота в @BotFather, вставить его токен, нажать
Start в боте. После этого `/status` в боте показывает ваши сессии.

## Безопасность

Панель слушает только 127.0.0.1 и требует токен доступа. С телефона — только
через `tailscale serve` (не Funnel, не открытый порт). Перенос сессий между
машинами, хаб нескольких машин, отдача файлов и внешний ingest в этой версии
отключены.

Лицензия MIT.
