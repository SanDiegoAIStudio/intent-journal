#!/bin/sh
# Walk through a step that cannot be undone, then remove the temp folder.
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd) || exit 1
tool="$root/intent_journal.py"
send="$root/examples/send.sh"
prove="$root/examples/prove.sh"
base=${TMPDIR:-/tmp}
base=${base%/}
work="$base/intent-demo-$$"
mkdir "$work" || {
  printf '%s\n' "could not make a temp folder"
  exit 1
}
trap 'rm -rf "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$work" || exit 1
journal=journal
outbox=outbox
PROVE=$prove
export PROVE
: > "$outbox"

fail() {
  printf '%s\n' "$1"
  exit 1
}

count() {
  wc -l < "$outbox" | tr -d '[:space:]'
}

run_py() {
  CRASH= python3 "$tool" "$@" >"$work/stdout" 2>"$work/stderr"
  code=$?
  if [ -s "$work/stderr" ]; then
    fail "unexpected error output"
  fi
}

run_killed() {
  /bin/sh -c 'CRASH=$1 python3 "$2" run "$3" --what "$4" --key "$5" -- /bin/sh "$6" "$7" "$4"; exit $?' \
    sh "$1" "$tool" "$journal" "$2" "$3" "$send" "$outbox" \
    >"$work/stdout" 2>"$work/stderr"
  code=$?
}

if ! grep -q . "$send" || ! grep -q . "$prove"; then
  fail "example scripts are missing"
fi

printf '%s\n' "(1) normal run"
run_py run "$journal" --what "invoice 1042" --key invoice-1042 -- /bin/sh "$send" "$outbox" "invoice 1042"
if [ "$code" -ne 0 ]; then
  fail "part 1 exited $code"
fi
if ! grep -q '^DID: invoice 1042 (intent ' "$work/stdout"; then
  fail "part 1 did not print the DID line"
fi
if [ "$(count)" -ne 1 ] || ! grep -Fxq "invoice 1042" "$outbox"; then
  fail "part 1 did not leave one outbox line"
fi
cat "$work/stdout"
printf '%s\n' "exit 0" "one line in the outbox" ""

printf '%s\n' "(2) the same key again"
run_py run "$journal" --what "invoice 1042" --key invoice-1042 -- /bin/sh "$send" "$outbox" "invoice 1042"
if [ "$code" -ne 0 ]; then
  fail "part 2 exited $code"
fi
if ! grep -q '^NOTHING TO DO: invoice-1042 was done at ' "$work/stdout"; then
  fail "part 2 did not print NOTHING TO DO"
fi
if [ "$(count)" -ne 1 ]; then
  fail "part 2 changed the outbox"
fi
cat "$work/stdout"
printf '%s\n' "exit 0" "still one line in the outbox" ""

printf '%s\n' "(3) killed right after the send"
run_killed after "invoice 1043" invoice-1043
if [ "$code" -le 128 ]; then
  fail "part 3 runner was not killed"
fi
if [ "$(count)" -ne 2 ] || ! grep -Fxq "invoice 1043" "$outbox"; then
  fail "part 3 did not leave the sent line"
fi
run_py status "$journal"
if [ "$code" -ne 3 ]; then
  fail "part 3 status exited $code"
fi
opens=$(grep -c '^  ' "$work/stdout" || true)
if [ "$opens" -ne 1 ]; then
  fail "part 3 status did not show one open intent"
fi
if ! grep -q '^1 open, 1 done, 0 cleared$' "$work/stdout"; then
  fail "part 3 status counts were wrong"
fi
if ! grep -q 'invoice 1043' "$work/stdout"; then
  fail "part 3 status omitted the open intent"
fi
cat "$work/stdout"
printf '%s\n' "exit 3" ""

printf '%s\n' "(4) the next run of any key"
cp "$outbox" "$work/before"
run_py run "$journal" --what "invoice 1044" --key invoice-1044 -- /bin/sh "$send" "$outbox" "invoice 1044"
if [ "$code" -ne 3 ]; then
  fail "part 4 exited $code"
fi
if ! grep -q '^STOP: 1 open intent(s). Check each by hand before anything new:' "$work/stdout"; then
  fail "part 4 did not print STOP"
fi
if ! grep -q 'invoice 1043' "$work/stdout"; then
  fail "part 4 did not name the open intent"
fi
if ! cmp -s "$outbox" "$work/before"; then
  fail "part 4 changed the outbox"
fi
cat "$work/stdout"
printf '%s\n' "exit 3" "outbox unchanged" ""

printf '%s\n' "(5) recover, then the stopped run"
run_py recover "$journal" --prove 'sh "$PROVE" outbox'
if [ "$code" -ne 0 ]; then
  fail "part 5 recover exited $code"
fi
if ! grep -q ' happened$' "$work/stdout"; then
  fail "part 5 did not record that the send happened"
fi
if ! grep -q '^recovered 1 of 1: 1 happened, 0 did not happen, 0 still open$' "$work/stdout"; then
  fail "part 5 recover counts were wrong"
fi
cat "$work/stdout"
printf '%s\n' ""
run_py run "$journal" --what "invoice 1044" --key invoice-1044 -- /bin/sh "$send" "$outbox" "invoice 1044"
if [ "$code" -ne 0 ]; then
  fail "part 5 rerun exited $code"
fi
if ! grep -q '^DID: invoice 1044 (intent ' "$work/stdout"; then
  fail "part 5 did not send the stopped key"
fi
if [ "$(count)" -ne 3 ] || ! grep -Fxq "invoice 1044" "$outbox"; then
  fail "part 5 did not add the stopped line"
fi
cat "$work/stdout"
printf '%s\n' "exit 0" ""

printf '%s\n' "(6) killed before the send"
before=$(count)
run_killed before "notice 17" notice-17
if [ "$code" -le 128 ]; then
  fail "part 6 runner was not killed"
fi
if [ "$(count)" -ne "$before" ] || grep -Fxq "notice 17" "$outbox"; then
  fail "part 6 wrote a line before the send"
fi
run_py recover "$journal" --prove 'sh "$PROVE" outbox'
if [ "$code" -ne 0 ]; then
  fail "part 6 recover exited $code"
fi
if ! grep -q ' did not happen$' "$work/stdout"; then
  fail "part 6 did not record that the send did not happen"
fi
if ! grep -q '^recovered 1 of 1: 0 happened, 1 did not happen, 0 still open$' "$work/stdout"; then
  fail "part 6 recover counts were wrong"
fi
cat "$work/stdout"
printf '%s\n' ""
run_py run "$journal" --what "notice 17" --key notice-17 -- /bin/sh "$send" "$outbox" "notice 17"
if [ "$code" -ne 0 ]; then
  fail "part 6 rerun exited $code"
fi
if ! grep -q '^DID: notice 17 (intent ' "$work/stdout"; then
  fail "part 6 did not send after the proof"
fi
cat "$work/stdout"
printf '%s\n' "exit 0" ""

lines=$(count)
unique=$(sort "$outbox" | uniq | wc -l | tr -d '[:space:]')
if [ "$lines" -ne 4 ] || [ "$unique" -ne 4 ]; then
  fail "outbox holds $lines lines"
fi
printf '%s\n' "outbox holds 4 lines, no line twice"
exit 0
