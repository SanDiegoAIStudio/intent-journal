"""Plant one bug at a time in a copy of the journal and see whether the tests catch it."""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "intent_journal.py")

# Each swap is applied to a fresh copy of the original. The test file is the
# one that should fail the assertion this bug breaks.
MUTATIONS = [
    (
        "step_before_intent",
        "test_journal.py",
        '        self._commit(record, "intent")\n        self._run_step(step, iid, failure_is_clean)',
        '        self._run_step(step, iid, failure_is_clean)\n        self._commit(record, "intent")',
    ),
    (
        "block_all_ignores_open",
        "test_journal.py",
        "            blocking = list(opens)",
        "            blocking = list()",
    ),
    (
        "block_key_ignores_same_key",
        "test_journal.py",
        'blocking = [rec for rec in opens if key is not None and rec.get("key") == key]',
        'blocking = [rec for rec in opens if key is not None and rec.get("key") != key]',
    ),
    (
        "done_key_runs_again",
        "test_journal.py",
        'newest["state"] == "done"',
        'newest["state"] == "missing"',
    ),
    (
        "cleared_key_treated_as_done",
        "test_journal.py",
        'newest["state"] == "done"',
        'newest["state"] != "open"',
    ),
    (
        "success_skips_done_mark",
        "test_journal.py",
        '        self._commit({"event": "done", "id": iid, "at": _now()}, "done", iid)',
        "        self._last_id = self._last_id",
    ),
    (
        "failure_writes_done",
        "test_journal.py",
        '{"event": "failed", "id": iid, "at": _now(), "detail": detail}',
        '{"event": "done", "id": iid, "at": _now(), "detail": detail}',
    ),
    (
        "failure_is_clean_skips_resolve",
        "test_journal.py",
        '        if note is None and failure_is_clean and not getattr(exc, "may_have_acted", False):',
        "        if note is None and False:",
    ),
    (
        "missing_command_stays_open",
        "test_cli.py",
        "        if note is not None:",
        "        if failure_is_clean:",
    ),
    (
        "resolve_allows_empty_note",
        "test_cli.py",
        'note.strip() == ""',
        'note.strip() == "never"',
    ),
    (
        "resolve_allows_closed_id",
        "test_journal.py",
        '            if item["state"] != "open":\n'
        '                raise ValueError("intent %s is not open" % intent_id)',
        '            if item["state"] == "open":\n'
        '                raise ValueError("intent %s is not open" % intent_id)',
    ),
    (
        "prove_true_counts_unknown",
        "test_journal.py",
        'self._settle(counts, intent, "happened", True, dry_run)',
        'self._settle(counts, intent, "unknown", None, dry_run)',
    ),
    (
        "prove_false_counts_unknown",
        "test_journal.py",
        'self._settle(counts, intent, "did_not_happen", False, dry_run)',
        'self._settle(counts, intent, "unknown", None, dry_run)',
    ),
    (
        "prove_unknown_counts_happened",
        "test_journal.py",
        'self._settle(counts, intent, "unknown", None, dry_run)',
        'self._settle(counts, intent, "happened", True, dry_run)',
    ),
    (
        "dry_run_writes",
        "test_journal.py",
        "        if happened is None or dry_run:",
        "        if happened is None:",
    ),
    (
        "cutoff_tail_is_damage",
        "test_journal.py",
        "        return records, True",
        "        raise JournalDamaged(1, self.path)",
    ),
    (
        "torn_record_not_written",
        "test_journal.py",
        '            prefix += _dumps({"event": "torn", "at": _now()})',
        '            prefix += b""',
    ),
    (
        "damaged_line_is_skipped",
        "test_journal.py",
        "                self._reject(idx + 1)\n                continue",
        "                idx += 1\n                continue",
    ),
    (
        "lock_not_taken",
        "test_cli.py",
        "fcntl.LOCK_EX | fcntl.LOCK_NB",
        "fcntl.LOCK_UN",
    ),
    (
        "stop_exits_zero",
        "test_cli.py",
        "EXIT_STOP = 3",
        "EXIT_STOP = 0",
    ),
    (
        "failed_exits_zero",
        "test_cli.py",
        "EXIT_FAILED = 1",
        "EXIT_FAILED = 0",
    ),
    (
        "could_not_exits_zero",
        "test_cli.py",
        "EXIT_COULD_NOT = 2",
        "EXIT_COULD_NOT = 0",
    ),
    (
        "busy_exits_zero",
        "test_cli.py",
        "EXIT_BUSY = 4",
        "EXIT_BUSY = 0",
    ),
    (
        "did_exits_nonzero",
        "test_cli.py",
        "EXIT_DID = 0",
        "EXIT_DID = 1",
    ),
    (
        "status_count_line",
        "test_cli.py",
        '_STATUS_COUNTS = "%d open, %d done, %d cleared"',
        '_STATUS_COUNTS = "%d open, %d done, %d shut"',
    ),
    (
        "recover_count_line",
        "test_cli.py",
        '_RECOVER_COUNTS = "recovered %d of %d: %d happened, %d did not happen, %d still open"',
        '_RECOVER_COUNTS = "recovered %d of %d: %d happened, %d did not happen, %d left open"',
    ),
    (
        "killed_step_cleared",
        "test_cli.py",
        "self.may_have_acted = code < 0 or code > 128",
        "self.may_have_acted = code > 128",
    ),
    (
        "above_128_cleared",
        "test_cli.py",
        "self.may_have_acted = code < 0 or code > 128",
        "self.may_have_acted = code < 0",
    ),
    (
        "clean_boundary_128",
        "test_cli.py",
        "code > 128",
        "code >= 128",
    ),
    (
        "may_have_acted_ignored",
        "test_cli.py",
        '        if note is None and failure_is_clean and not getattr(exc, "may_have_acted", False):',
        "        if note is None and failure_is_clean:",
    ),
    (
        "how_read_from_the_flag",
        "test_cli.py",
        'how = "recorded as did not happen" if journal._last_cleared else "the intent stays open"',
        'how = "recorded as did not happen" if failure_is_clean else "the intent stays open"',
    ),
    (
        "signal_printed_as_exit",
        "test_cli.py",
        '        if err.code < 0:\n'
        '            _out("FAILED: %s was killed by signal %d; %s (%s)" % (command[0], -err.code, how, journal._last_id))\n'
        '        else:\n'
        '            _out("FAILED: %s exited %s; %s (%s)" % (command[0], err.code, how, journal._last_id))',
        '        _out("FAILED: %s exited %s; %s (%s)" % (command[0], err.code, how, journal._last_id))',
    ),
    (
        "lock_open_error_not_caught",
        "test_cli.py",
        '        try:\n'
        '            fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)\n'
        '        except OSError as err:\n'
        '            raise _OpenFailed(err.errno, err.strerror, err.filename)',
        '        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)',
    ),
    (
        "read_error_not_caught",
        "test_cli.py",
        '        try:\n'
        '            with open(self.path, "rb") as handle:\n'
        '                data = handle.read()\n'
        '        except OSError as err:\n'
        '            raise _OpenFailed(err.errno, err.strerror, err.filename)',
        '        with open(self.path, "rb") as handle:\n'
        '            data = handle.read()',
    ),
    (
        "status_open_error_not_caught",
        "test_cli.py",
        '            counts, opens = journal.status(), journal.open_intents()\n'
        '        except _OpenFailed as err:\n'
        '            _out("COULD NOT: the journal could not be opened (%s); nothing was run or written" % err)\n'
        '            return EXIT_COULD_NOT',
        '            counts, opens = journal.status(), journal.open_intents()',
    ),
    (
        "resolve_write_error_not_caught",
        "test_journal.py",
        '    except OSError as err:\n'
        '        _out("COULD NOT: the resolution could not be written (%s); the intent stays open (%s)" % (err, iid))\n'
        '        return EXIT_COULD_NOT\n'
        '    except Busy:',
        '    except Busy:',
    ),
    (
        "recover_write_error_not_caught",
        "test_cli.py",
        '    except OSError as err:\n'
        '        prefix = "would: " if dry_run else ""\n'
        '        for iid, label, code in lines[:-1]:\n'
        '            if label == "unknown":\n'
        '                _out("%s%s unknown (exit %s)" % (prefix, iid, code))\n'
        '            else:\n'
        '                _out("%s%s %s" % (prefix, iid, label))\n'
        '        _out("COULD NOT: the resolution could not be written (%s); the intent stays open (%s)" % (err, lines[-1][0]))\n'
        '        return EXIT_COULD_NOT\n'
        '    except Busy:',
        '    except Busy:',
    ),
    (
        "recover_prints_the_failed_intent_as_settled",
        "test_cli.py",
        "for iid, label, code in lines[:-1]:",
        "for iid, label, code in lines:",
    ),
]


def _verdict(output, returncode):
    if returncode == 0:
        return "survived"
    failures = re.findall(r"failures=(\d+)", output)
    errors = re.findall(r"errors=(\d+)", output)
    if (failures and int(failures[-1]) > 0) or (errors and int(errors[-1]) > 0):
        return "caught"
    return "crashed"


def _sandbox(body, test_name):
    work = tempfile.mkdtemp(prefix="intent-mutate-")
    tests = os.path.join(work, "tests")
    os.makedirs(tests)
    with open(os.path.join(work, "intent_journal.py"), "w", encoding="utf-8") as handle:
        handle.write(body)
    shutil.copy(os.path.join(ROOT, "tests", test_name), os.path.join(tests, test_name))
    return work


def _suite(work):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return "timed out"
    return _verdict(out + "\n" + err, proc.returncode)


def _baseline(original, test_name):
    work = _sandbox(original, test_name)
    try:
        return _suite(work)
    finally:
        shutil.rmtree(work)


def _run_one(original, name, test_name, old, new):
    mutated = original.replace(old, new, 1)
    try:
        compile(mutated, "intent_journal.py", "exec")
    except SyntaxError:
        return "does not compile"
    work = _sandbox(mutated, test_name)
    try:
        return _suite(work)
    finally:
        shutil.rmtree(work)


def _test_files():
    seen = []
    for _name, test_name, _old, _new in MUTATIONS:
        if test_name not in seen:
            seen.append(test_name)
    return seen


def main():
    with open(SOURCE, encoding="utf-8") as handle:
        original = handle.read()
    for name, _test_name, old, new in MUTATIONS:
        count = original.count(old)
        if count != 1 or old == new:
            sys.stderr.write("%s: expected 1 match, found %d\n" % (name, count))
            return 2
    for test_name in _test_files():
        if _baseline(original, test_name) != "survived":
            sys.stderr.write("baseline failed: %s\n" % test_name)
            return 2
    caught = 0
    for name, test_name, old, new in MUTATIONS:
        verdict = _run_one(original, name, test_name, old, new)
        sys.stdout.write("%s %s\n" % (name, verdict))
        sys.stdout.flush()
        if verdict == "caught":
            caught += 1
    total = len(MUTATIONS)
    sys.stdout.write("caught %d of %d\n" % (caught, total))
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
