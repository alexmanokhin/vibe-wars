# Vibe Wars

A self-hosted dashboard for Claude Code Remote Control sessions: see every
session on your computer, start new ones, type into them, answer their
questions, watch limits and machine load, and switch accounts, from a browser
on the same computer or from your phone over Tailscale.

Not affiliated with Anthropic.

## Requirements

- A Mac (macOS 13 or newer) or a Linux machine that stays on while you work.
- Claude Code installed and signed in with a paid Claude subscription (Pro or Max).
- Python 3.9+ (on a Mac: the Command Line Tools, `xcode-select --install`) and git.
- Tailscale installed and signed in on the computer and on the phone (same account),
  only if you want to open the dashboard from the phone.
- Optional: a Telegram bot token, for alerts and the control bot.

## Install

Open Claude Code in any folder (not the home folder) and paste:

```
Install Vibe Wars for me: https://github.com/alexmanokhin/vibe-wars (follow its INSTALL.md)
```

Or clone it yourself and give it to Claude Code:

```
git clone https://github.com/alexmanokhin/vibe-wars ~/vibe-wars
cd ~/vibe-wars
claude "Read INSTALL.md and install this for me."
```

Claude checks the prerequisites, asks only what it must, installs the
background services (launchd on macOS, systemd on Linux), starts them and
prints the address to open.

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

v0.1. Some UI strings and many code comments are still in Russian.

---

# Vibe Wars (по-русски)

Дашборд для сессий Claude Code Remote Control на своём компьютере: все сессии
в одном окне, запуск новых, сообщения в сессии, ответы на вопросы Claude,
лимиты, нагрузка машины, переключение аккаунтов. Открывается в браузере на
компьютере или с телефона через Tailscale.

## Что нужно

- Mac (macOS 13+) или Linux-машина, которая включена, пока вы работаете.
- Claude Code, вход с платной подпиской Claude (Pro или Max).
- Python 3.9+ (на Mac: Command Line Tools, `xcode-select --install`) и git.
- Tailscale на компьютере и на телефоне под одним аккаунтом, если нужен доступ с телефона.
- По желанию: токен Telegram-бота для алертов и управления.

## Установка

Откройте Claude Code в любой папке (не в домашней) и вставьте:

```
Install Vibe Wars for me: https://github.com/alexmanokhin/vibe-wars (follow its INSTALL.md)
```

Или склонируйте сами и отдайте Claude Code:

```
git clone https://github.com/alexmanokhin/vibe-wars ~/vibe-wars
cd ~/vibe-wars
claude "Read INSTALL.md and install this for me."
```

Claude проверит требования, задаст только нужные вопросы, установит фоновые
службы (launchd на Mac, systemd на Linux), запустит их и покажет адрес.

## Безопасность

Панель слушает только 127.0.0.1 и требует токен доступа. С телефона — только
через `tailscale serve` (не Funnel, не открытый порт). Перенос сессий между
машинами, хаб нескольких машин, отдача файлов и внешний ingest в этой версии
отключены.

Лицензия MIT.
