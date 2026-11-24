#!/usr/bin/env bash
# Starts the `claude remote-control` daemon that spawns sessions for the Claude app.
# RC_NAME_PREFIX names the sessions in the app ("<prefix>-..."), so two machines
# can be told apart. RC_CLAUDE overrides which claude binary runs; by default the
# copy the guardian keeps (claude-rc) is preferred, so one Full Disk Access grant
# for it also covers chats started from the app.
C="${RC_CLAUDE:-}"
if [ -z "$C" ]; then
  C="$HOME/.local/bin/claude-rc"
  [ -x "$C" ] || C="$HOME/.local/bin/claude"
  [ -x "$C" ] || C="$(command -v claude)"
fi
printf 'y\n' | exec "$C" remote-control \
  --remote-control-session-name-prefix "${RC_NAME_PREFIX:-vibe-wars}" \
  --capacity "${RC_CAPACITY:-64}"
