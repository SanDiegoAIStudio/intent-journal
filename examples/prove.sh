#!/bin/sh
# Exit 0 when OUTBOX has a line equal to INTENT_WHAT, 1 when it does not, 2 when it cannot be read.
if [ "$#" -ne 1 ]; then
  echo "usage: prove.sh OUTBOX" >&2
  exit 2
fi
outbox=$1
if [ ! -f "$outbox" ] || [ ! -r "$outbox" ]; then
  exit 2
fi
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$line" = "$INTENT_WHAT" ]; then
    exit 0
  fi
done < "$outbox"
exit 1
