intent-journal is a journal for a step that cannot be taken back, such as sending a message, charging a card, deleting a folder, or posting. It writes down what it is about to do, gets that line onto the disk, does the step, and then writes that the step is done.

## What you get

One Python file, `intent_journal.py`, is both a small library and a command line tool. The journal is one append-only text file of JSON objects, one to a line. Nothing in it is rewritten or deleted. `examples/send.sh` is a stand-in for the step, `examples/prove.sh` looks for the line that step leaves behind, and `examples/demo.sh` walks through a normal run, a repeat, a crash, a stop, and a recovery. To stage a crash, `examples/send.sh` kills the process that started it when `CRASH` is set to `before` or `after`, so run it only with `CRASH` unset outside the demo.

## Quick start

From this folder:

```
sh examples/demo.sh
```

The ids and times differ on each run:

```
(1) normal run
DID: invoice 1042 (intent 1b584682ffd9)
exit 0
one line in the outbox

(2) the same key again
NOTHING TO DO: invoice-1042 was done at 2026-11-19T09:14:34Z (intent 1b584682ffd9)
exit 0
still one line in the outbox

(3) killed right after the send
  7f0a18f32bd9  2026-11-19T09:14:34Z  invoice-1043  invoice 1043
1 open, 1 done, 0 cleared
exit 3

(4) the next run of any key
STOP: 1 open intent(s). Check each by hand before anything new:
  7f0a18f32bd9  2026-11-19T09:14:34Z  invoice-1043  invoice 1043
  then: python3 intent_journal.py resolve journal ID --happened | --did-not-happen --note "who checked, and where"
exit 3
outbox unchanged

(5) recover, then the stopped run
7f0a18f32bd9 happened
recovered 1 of 1: 1 happened, 0 did not happen, 0 still open

DID: invoice 1044 (intent ef98a6d1d901)
exit 0

(6) killed before the send
88f98bf5acba did not happen
recovered 1 of 1: 0 happened, 1 did not happen, 0 still open

DID: notice 17 (intent 3da94a071080)
exit 0

outbox holds 4 lines, no line twice
```

## The three moves

Every run reads the journal first. If an intent has no done mark and no resolution, the run stops and lists it (with `--block key`, only an intent with the run's own key stops it). The tool never runs that step again by itself. A person, or a proof command that looks where the step leaves a trace, then says whether it happened.

When nothing is open, the run does three things in order. It writes the intent and syncs that line to disk. It does the step. It writes the done mark. A crash between the first line and the done mark leaves the intent open, which is the whole point: the next start can see that the step is unproven.

This is the shape of one run. The names in the sketch are the moves. The real call is `run_once`, further down.

```python
def once(journal, step, what):
    opens = journal.open_intents()
    if opens:
        for item in opens:
            print(item["id"], item["what"])
        raise SystemExit("check each open intent first")
    record = journal.write_intent(what)
    journal.sync()
    step()
    journal.write_done(record)
    journal.sync()
    return "did"
```

## Wrap a command

```
python3 intent_journal.py run JOURNAL --what TEXT [--key KEY] [--block all|key] [--failure-is-clean] -- COMMAND [ARG...]
```

`COMMAND` is started without a shell. The tool adds `INTENT_ID`, `INTENT_KEY`, and `INTENT_WHAT` to its environment. The command's own output passes through untouched. After it ends, the tool prints one last line on stdout.

By default a run stops when any intent in the journal is open (`--block all`). With `--block key`, only an open intent with the same `--key` stops it, so a run with `--block key` and no `--key` is never stopped by an open intent.

| Last line | Exit |
| --- | --- |
| `DID: WHAT (intent ID)` | 0 |
| `NOTHING TO DO: KEY was done at TIME (intent ID)` | 0 |
| `STOP: N open intent(s). ...` and one line per open intent | 3 |
| `FAILED: COMMAND exited M; the intent stays open (ID)` | 1 |
| `FAILED: COMMAND exited M; recorded as did not happen (ID)` | 1 |
| `FAILED: COMMAND was killed by signal N; the intent stays open (ID)` | 1 |
| `BUSY: another run holds JOURNAL.lock` | 4 |
| `COULD NOT: REASON` | 2 |

In these lines `COMMAND` is the first word of the command. A usage mistake prints one line on stderr that starts with `usage:` and exits 2. The command is not started for `STOP`, `BUSY`, or a journal that cannot be read.

A scheduled job runs `python3 intent_journal.py run journal --what "invoice 1042" --key invoice-1042 -- sh examples/send.sh outbox "invoice 1042"` and treats exit 3 as a stop a person has to check.

## Use it from Python

`send` below is the step. `run_once` calls it with no arguments. `hand_off` stands for the real work, such as handing invoice 1042 to customer 17.

```python
from intent_journal import Journal

def send():
    hand_off("invoice 1042")

journal = Journal("journal")
outcome = journal.run_once(send, "invoice 1042", key="invoice-1042")
print(outcome.state, outcome.intent_id)
```

`outcome.state` is `did` or `nothing_to_do`. A key whose newest closed intent is already done returns `nothing_to_do` and writes nothing.

Every other ending is an exception. `run_once` raises `OpenIntents` when an intent is open (its `intents` attribute lists them), `Busy` when another run holds the lock, and `JournalDamaged` when a line of the journal is not a record. An exception from the step is raised again after the failure is written down, and the intent stays open unless `failure_is_clean=True` was passed. When the journal or its lock cannot be opened or written, it raises an `OSError` or the module's internal `_WriteFailed`, so catch `Exception` to cover every case.

## When a run stops

`python3 intent_journal.py status JOURNAL` lists each open intent and ends with `N open, D done, C cleared`. Exit 0 means nothing is open. Exit 3 means something is. Add `--json` for one JSON object and no other output. When the journal cannot be opened, `--json` prints the plain `COULD NOT` line described below instead and exits 2.

Check the open intent by hand, in the place the step would have left a trace. Then record what you found:

```
python3 intent_journal.py resolve JOURNAL ID --happened --note "who checked, and where"
python3 intent_journal.py resolve JOURNAL ID --did-not-happen --note "who checked, and where"
```

`--happened` means the step is done and that key has no work left. `--did-not-happen` means the step did not happen and may be run again.

When many intents are open and each one leaves a trace, point recover at a proof command:

```
python3 intent_journal.py recover JOURNAL --prove "sh examples/prove.sh outbox"
```

The proof command runs through `sh -c` with `INTENT_ID`, `INTENT_KEY`, `INTENT_WHAT`, and `INTENT_AT` set. Exit 0 means the step happened. Exit 1 means it did not. Any other exit leaves the intent open. Proof is a place the step leaves a trace, such as a sent folder, a payment id, or a row in a table. `--dry-run` prints the same answers with `would:` in front and writes nothing. `recover` exits 3 when a proof left an intent open, and 0 otherwise.

When the journal or its lock cannot be opened, `status`, `resolve`, and `recover` print `COULD NOT: the journal could not be opened (REASON); nothing was run or written` and exit 2. When a resolution cannot be written, `resolve` prints `COULD NOT: the resolution could not be written (REASON); the intent stays open (ID)` and exits 2. `recover` first prints the lines for the intents it checked before that one, then the same line. `resolve` also exits 2, with a one-line reason, when the note is empty, the ID is not in the journal, or the intent is not open.

## Failures

A command that exits non-zero does not prove the step did nothing. The command may have completed the effect and then exited badly, or the runner may have died after the effect and before any record of the failure. The tool writes a failed record, which is information only, and leaves the intent open.

`--failure-is-clean` is the promise that a failure returned by the step means the effect did not happen. Use it for a step that refuses before it changes anything, such as a check that rejects invoice 1042 before it is sent. The tool then records the intent as did not happen, and the key may run again. Do not make that promise for a step that might have acted before it failed.

A command that cannot be started at all, such as a program that is not installed, is recorded as did not happen with or without `--failure-is-clean`, because nothing ran.

A command that a signal ended, or one that exited above 128, which is how a shell reports a child that a signal ended, leaves the intent open even with `--failure-is-clean`, because the step may have finished before the signal. The last line then reads `FAILED: COMMAND was killed by signal N; the intent stays open (ID)`, or `FAILED: COMMAND exited M; the intent stays open (ID)` for an exit above 128.

## What each run says

A run says one of three things, and it always says something.

- Did the work: `DID`, exit 0.
- Had no work: `NOTHING TO DO`, exit 0. The key was already done.
- Could not: `COULD NOT` (exit 2) when the journal is damaged, the journal could not be opened, the intent could not be written, the command could not be started, the done mark could not be written, or the failure could not be written. `STOP` (exit 3) when an earlier intent is still open. `BUSY` (exit 4) when another run holds the lock. `FAILED` (exit 1) when the command exited non-zero or a signal ended it.

A run ends with one of these lines, apart from the two Python errors listed under Limits: Ctrl-C while the step runs, and a lock error other than busy. If you see no line, the process died or was stopped before it could print, and the journal is where you look next.

## How it keeps its word

The intent line is on the disk before the step runs. The append writes the bytes, flushes them, and then asks for a full sync of the file. On macOS that sync is `fcntl.F_FULLFSYNC` when the platform has it, and `os.fsync` otherwise. When the append created the journal file, the folder is synced too, so the new name survives a crash.

A last line with no newline, which does not parse, is a write that a crash cut off. It is ignored and reported. The next append closes that line and writes a torn record after it, so later reads accept the file. Any other line that does not parse stops the read. Nothing is run.

One run at a time. The run takes an exclusive, non-waiting lock on `JOURNAL.lock` and holds it until the run ends, including while the step is in progress. A second run is told the journal is busy and does not start its step.

## Tests

The tests need Python 3.9 or newer, `/bin/sh`, `/usr/bin/true` and `/usr/bin/false`.

```
python3 -m unittest discover -s tests -v
python3 tests/mutate.py
```

The mutation run first runs the two test files the planted bugs are judged by, `tests/test_journal.py` and `tests/test_cli.py`, on the unchanged code and stops when one fails. It then plants one bug at a time in a copy of `intent_journal.py`. A bug counts as caught only when a test fails or ends in an error. The run prints one line for each bug and ends with:

```
caught 38 of 38
```

The command exits 0 when every row is caught. It exits 1 when a row is not caught. It exits 2 when a row's old text is found a number of times other than one, or that old text equals its new text, or a test file does not pass on the unchanged code.

## Limits

- The journal and its lock are local files. Two machines doing the same step need a shared store, and this is not one.
- With block `all`, one unproven step stops every later run until it is resolved. That is the safe default. Block `key` is for steps that do not depend on each other.
- The tool never retries and never schedules anything.
- The journal only grows. Start a new file at a moment when nothing is open.
- The `what` text is stored as plain text. Keep secrets out of it.
- When the service you call accepts an idempotency key, pass it `INTENT_KEY`. That is a second line of defence, and the stronger one.
- Time stamps come from the local clock.
- A kill of the whole machine between the write and the sync can lose the last line on a drive that ignores sync requests.
- It needs `fcntl`, so it does not run on Windows outside WSL.
- A journal path that does not exist yet reads as a new, empty journal: `status` prints `0 open, 0 done, 0 cleared` and exits 0, and `run` starts a journal there. A mistyped path therefore does not see the open intents of the real journal.
- Ctrl-C while the step runs stops the tool with a Python error and no last line. The intent stays open, so the next run stops on it until it is resolved.
- The journal's folder must support file locks. When taking the lock fails with an error other than busy, `run` and `recover` stop with a Python error, `resolve` stops with exit 2, and nothing is written.
- A proof command that fails on an error of its own and exits 1, as Python does when an import fails, reads as did not happen. Write a proof so that its own errors exit 2 or higher.
- `examples/prove.sh` compares whole lines with WHAT. When two open intents share a WHAT and only one was sent, both read as happened, and a WHAT that holds a newline never matches, so it reads as did not happen.
- A proof command has no time limit and runs while the lock is held, so a proof that hangs holds up every run of that journal.
- A crash that cuts off a write leaves one bad last line, which the next append repairs. If a second crash cuts off that repair, two bad lines remain, and the journal reads as damaged until a person removes them.

It was tested on macOS with Python 3.9 and Python 3.14.

## License

MIT. The full text is in `LICENSE`.

## Built at San Diego AI Studio

This tool comes out of San Diego AI Studio, the builders' community in San Diego, where builders show the systems they run. More projects: https://sandiegoaistudio.com/projects

