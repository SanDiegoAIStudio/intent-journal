#!/bin/sh
# Stand-in for a step that cannot be undone. Appends one line, unless asked to kill the runner.
if [ "$#" -ne 2 ]; then
  echo "usage: send.sh OUTBOX TEXT" >&2
  exit 2
fi
outbox=$1
text=$2
crash=${CRASH-}
if [ "$crash" = "before" ]; then
  kill -s KILL "$PPID" 2>/dev/null
  exit 0
fi
printf '%s\n' "$text" >> "$outbox" || exit 1
if [ "$crash" = "after" ]; then
  kill -s KILL "$PPID" 2>/dev/null
  exit 0
fi
exit 0
