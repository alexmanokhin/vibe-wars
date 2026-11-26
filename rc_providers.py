#!/usr/bin/env python3
"""Model providers for sessions: Claude Code itself, pointed at another model.

MiniMax (and Kimi, GLM, Qwen) serve an Anthropic-compatible API, so a session is the same `claude` process with three environment
variables changed. Everything the panel does keeps working: the transcript,
the FIFO, the pty log, restores. What does not: Remote Control. With an API
token in the environment Claude Code silently skips it (checked 25.11: the
flag stays in argv, no card, no relay socket), so such a session has no app
link and holds no socket while idle. The guardian must not take that for a
husk (husk_pids skips provider sessions).

Keys live in <dir>/keys.env (mode 600), one VAR=value per line. A launch
writes <dir>/<provider>.env from the key and the template below and the
session wrapper sources it, so the key is never in argv or in /tmp.
"""

import os
import re

import rc_config as CFG

DIR = os.environ.get("RC_PROVIDERS_DIR") or os.path.join(CFG.STATE_DIR, "providers")
KEYS = os.path.join(DIR, "keys.env")
EXTRA_KEYS = []
# Mistral Vibe plan key (rc_vibe)
VIBE_KEY_VARS = ("MISTRAL_VIBE_API_KEY",)

PROVIDERS = {
    # MiniMax Token Plan key (a subscription), Anthropic-style endpoint
    "minimax": {
        "label": "MiniMax",
        "key_var": "MINIMAX_API_KEY",
        "base_url": "https://api.minimax.io/anthropic",
        "model": "MiniMax-M3",
        "fast": "MiniMax-M3",
        "context": 200000,
    },
}


def _keys():
    out = {}
    for path in [KEYS] + EXTRA_KEYS:
        try:
            with open(path) as fh:
                for line in fh:
                    m = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
                    if m and not line.lstrip().startswith("#") and m.group(1) not in out:
                        out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        except OSError:
            pass
    return out


def key(provider):
    p = PROVIDERS.get(provider)
    if not p:
        return ""
    return _keys().get(p["key_var"]) or os.environ.get(p["key_var"], "")


def available():
    """[{id, label, model}] for providers that have a key. Only subscription
    plans (a CLI login or a plan key), never a per-token API."""
    return [{"id": pid, "label": p["label"], "model": p["model"]}
            for pid, p in PROVIDERS.items() if key(pid) and not p.get("per_token")]


def save_key(var, value):
    """Put VAR=value into keys.env (mode 600), replacing an older line."""
    if not re.match(r"^[A-Z0-9_]+$", var or ""):
        raise ValueError("bad key name")
    value = (value or "").strip()
    if not value or re.search(r"[\s'\"\\]", value):
        raise ValueError("the key looks wrong: paste it as one line")
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    lines = []
    try:
        with open(KEYS) as fh:
            lines = [l.rstrip("\n") for l in fh
                     if not re.match(r"\s*(?:export\s+)?%s\s*=" % var, l)]
    except OSError:
        pass
    lines.append("%s=%s" % (var, value))
    tmp = KEYS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, KEYS)


def check_key(provider, value):
    """(ok, text): one cheap call with the pasted key before it is saved."""
    import json
    import urllib.error
    import urllib.request
    if provider == "vibe":
        req = urllib.request.Request("https://api.mistral.ai/v1/models",
                                     headers={"Authorization": "Bearer " + value})
    elif provider in PROVIDERS:
        p = PROVIDERS[provider]
        body = json.dumps({"model": p["fast"], "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(p["base_url"].rstrip("/") + "/v1/messages", data=body,
                                     headers={"x-api-key": value, "Authorization": "Bearer " + value,
                                              "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
    else:
        return False, "unknown provider"
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300, "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "the provider refused this key (%d)" % exc.code
        return False, "the provider answered %d" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, "no answer from the provider: %s" % str(exc)[:80]


def key_var(provider):
    """The variable a pasted key is saved under."""
    if provider == "vibe":
        return VIBE_KEY_VARS[-1]
    p = PROVIDERS.get(provider or "")
    return p["key_var"] if p else ""


def vibe_key():
    k = _keys()
    for v in VIBE_KEY_VARS:
        if k.get(v) or os.environ.get(v):
            return k.get(v) or os.environ.get(v)
    return ""


def vibe_home(model=None):
    """VIBE_HOME of panel sessions: their own key and config, never ~/.vibe.
    Rewritten before every launch, so a new key takes effect on the next one."""
    import rc_vibe
    home = os.path.join(DIR, "vibe-home")
    k = vibe_key()
    if not k:
        raise ValueError("no Mistral Vibe key: put MISTRAL_VIBE_API_KEY=... in %s" % KEYS)
    rc_vibe.prepare_home(home, k, model or rc_vibe.DEFAULT_MODEL)
    return home


def label(provider):
    p = PROVIDERS.get(provider or "")
    return p["label"] if p else ""


def env_file(provider):
    """Write <dir>/<provider>.env for one launch; returns its path.
    Raises ValueError when the provider is unknown or has no key."""
    p = PROVIDERS.get(provider or "")
    if not p:
        raise ValueError("unknown provider %r" % (provider or "")[:20])
    k = key(provider)
    if not k:
        raise ValueError("no %s key: put %s=... in %s" % (p["label"], p["key_var"], KEYS))
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    path = os.path.join(DIR, provider + ".env")
    lines = [
        "ANTHROPIC_BASE_URL=" + p["base_url"],
        "ANTHROPIC_AUTH_TOKEN=" + k,
        "ANTHROPIC_MODEL=" + p["model"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL=" + p["model"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL=" + p["model"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL=" + p["fast"],
        "ANTHROPIC_SMALL_FAST_MODEL=" + p["fast"],
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % p["context"],
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
    ]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join("%s=%s" % (l.split("=", 1)[0], _q(l.split("=", 1)[1])) for l in lines) + "\n")
    os.replace(tmp, path)
    return path


def _q(v):
    return "'" + v.replace("'", "'\\''") + "'"


def balance(provider):
    """(ok, text) of the provider's balance, or (None, '') if it has no check."""
    import json
    import urllib.request
    p = PROVIDERS.get(provider or "")
    if not p or not p.get("balance_url") or not key(provider):
        return None, ""
    req = urllib.request.Request(p["balance_url"], headers={"Authorization": "Bearer " + key(provider)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:80]
    infos = d.get("balance_infos") or [{}]
    b = infos[0]
    return bool(d.get("is_available")), "%s %s" % (b.get("total_balance", "?"), b.get("currency", ""))


if __name__ == "__main__":
    for pid in PROVIDERS:
        print(pid, "key" if key(pid) else "no key", balance(pid))
