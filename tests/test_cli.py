"""Tool tests, driven through subprocess."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "intent_journal.py")
PY = sys.executable


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, "journal")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, args, env=None, timeout=10):
        return subprocess.run(
            [PY, TOOL] + args,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_did_and_nothing_to_do(self):
        """source: a finished run says it did the work, and the same key afterwards says there is nothing to do."""
        first = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/true",
        ])
        self.assertEqual(first.returncode, 0)
        self.assertRegex(first.stdout, r"^DID: invoice 1042 \(intent [0-9a-f]{12}\)\n$")
        second = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/true",
        ])
        self.assertEqual(second.returncode, 0)
        self.assertRegex(
            second.stdout,
            r"^NOTHING TO DO: invoice-1042 was done at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
            r"\(intent [0-9a-f]{12}\)\n$",
        )

    def test_intent_is_visible_to_the_command(self):
        """source: the command can read the intent line from the journal while it is still running."""
        marker = os.path.join(self.tmp.name, "marker")
        code = (
            "import os\n"
            "body = open(os.environ['JOURNAL'], encoding='utf-8').read()\n"
            "flag = 'yes' if os.environ['INTENT_ID'] in body else 'no'\n"
            "open(os.environ['MARKER'], 'w', encoding='utf-8').write(flag)\n"
        )
        env = os.environ.copy()
        env["JOURNAL"] = self.journal
        env["MARKER"] = marker
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", PY, "-c", code,
        ], env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self._read(marker), "yes")

    def test_failed_command_stays_open(self):
        """source: a command that exits non-zero leaves the intent open and the tool exits with failure."""
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/false",
        ])
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(
            proc.stdout,
            r"^FAILED: /usr/bin/false exited 1; the intent stays open \([0-9a-f]{12}\)\n$",
        )
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3)
        self.assertEqual(status.stdout.strip().splitlines()[-1], "1 open, 0 done, 0 cleared")

    def test_failure_is_clean_records_did_not_happen(self):
        """source: a clean failure records that the step did not happen and still exits with failure."""
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--failure-is-clean", "--", "/usr/bin/false",
        ])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("recorded as did not happen", proc.stdout)
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 0)
        self.assertEqual(status.stdout.strip(), "0 open, 0 done, 1 cleared")

    def test_open_intent_stops_the_next_run(self):
        """source: an open intent stops the next run before the command starts, and the tool exits with the stop code."""
        self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/false",
        ])
        marker = os.path.join(self.tmp.name, "marker")
        code = "open(%r, 'w', encoding='utf-8').write('x')" % marker
        proc = self._run([
            "run", self.journal, "--what", "invoice 1043", "--key", "invoice-1043",
            "--", PY, "-c", code,
        ])
        self.assertEqual(proc.returncode, 3)
        self.assertIn("STOP: 1 open intent(s). Check each by hand before anything new:", proc.stdout)
        self.assertIn("invoice 1042", proc.stdout)
        self.assertIn(
            'then: python3 intent_journal.py resolve %s ID --happened | --did-not-happen --note "who checked, and where"'
            % self.journal,
            proc.stdout,
        )
        self.assertFalse(os.path.exists(marker))

    def test_block_key_lets_another_key_run(self):
        """source: under block key, an open intent stops its own key and lets a different key run."""
        first = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--block", "key", "--", "/usr/bin/false",
        ])
        self.assertEqual(first.returncode, 1)
        stopped = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--block", "key", "--", "/usr/bin/true",
        ])
        self.assertEqual(stopped.returncode, 3)
        other = self._run([
            "run", self.journal, "--what", "invoice 1043", "--key", "invoice-1043",
            "--block", "key", "--", "/usr/bin/true",
        ])
        self.assertEqual(other.returncode, 0)
        self.assertIn("DID: invoice 1043", other.stdout)

    def test_second_run_is_busy_while_a_step_holds_the_lock(self):
        """source: a second run while a step still holds the lock is told to wait, and its command does not start."""
        marker = os.path.join(self.tmp.name, "marker")
        code = "open(%r, 'w', encoding='utf-8').write('x')" % marker
        first = subprocess.Popen(
            [
                PY, TOOL, "run", self.journal, "--what", "invoice 1042",
                "--key", "invoice-1042", "--block", "key", "--",
                PY, "-c", "import time; time.sleep(30)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                if os.path.exists(self.journal) and "invoice 1042" in self._read(self.journal):
                    break
                time.sleep(0.05)
            else:
                self.fail("first run did not write the intent")
            try:
                second = self._run([
                    "run", self.journal, "--what", "invoice 1043", "--key", "invoice-1043",
                    "--block", "key", "--", PY, "-c", code,
                ], timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("second run waited instead of reporting busy")
            self.assertEqual(second.returncode, 4)
            self.assertIn("BUSY: another run holds %s.lock" % self.journal, second.stdout)
            self.assertFalse(os.path.exists(marker))
        finally:
            try:
                os.killpg(first.pid, signal.SIGKILL)
            except OSError:
                pass
            first.wait(timeout=5)

    def test_damaged_middle_line_does_not_start_the_step(self):
        """source: a damaged middle line makes the tool refuse to run, and the command does not start."""
        self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/true",
        ])
        with open(self.journal, "rb") as handle:
            data = handle.read()
        parts = data.split(b"\n")
        damaged = parts[0] + b"\n" + b"not-a-record\n" + parts[1] + b"\n"
        with open(self.journal, "wb") as handle:
            handle.write(damaged)
        marker = os.path.join(self.tmp.name, "marker")
        code = "open(%r, 'w', encoding='utf-8').write('x')" % marker
        proc = self._run([
            "run", self.journal, "--what", "invoice 1043", "--", PY, "-c", code,
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(
            "COULD NOT: line 2 of %s is not a record; nothing was run" % self.journal,
            proc.stdout,
        )
        self.assertFalse(os.path.exists(marker))
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 2)
        self.assertIn("is not a record; nothing was run", status.stdout)

    def test_unwritable_journal_does_not_start_the_step(self):
        """source: when the intent cannot be written, the tool says so and does not start the command."""
        os.mkdir(self.journal)
        marker = os.path.join(self.tmp.name, "marker")
        code = "open(%r, 'w', encoding='utf-8').write('x')" % marker
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--", PY, "-c", code,
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("COULD NOT: the intent could not be written", proc.stdout)
        self.assertIn("the step was not run", proc.stdout)
        self.assertFalse(os.path.exists(marker))

    def test_missing_command_is_recorded_as_did_not_happen(self):
        """source: a command that cannot be started is recorded as did not happen, with the note that says so."""
        missing = os.path.join(self.tmp.name, "missing-command")
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", missing,
        ])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("could not be started", proc.stdout)
        self.assertIn("recorded as did not happen", proc.stdout)
        body = self._read(self.journal)
        self.assertIn('"note":"the command could not be started"', body)
        self.assertIn('"happened":false', body)
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 0)
        self.assertIn("0 open, 0 done, 1 cleared", status.stdout)

    def test_done_mark_failure_leaves_the_intent_open(self):
        """source: when the step finishes but the done mark cannot be written, the intent stays open."""
        code = "import os, sys; os.rename(sys.argv[1], sys.argv[1] + '.kept'); os.mkdir(sys.argv[1])"
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", PY, "-c", code, self.journal,
        ])
        kept = self.journal + ".kept"

        def restore():
            if os.path.isdir(self.journal):
                os.rmdir(self.journal)
            if os.path.isfile(kept) and not os.path.exists(self.journal):
                os.rename(kept, self.journal)

        try:
            self.assertEqual(proc.returncode, 2)
            self.assertIn("the step ran but the done mark could not be written", proc.stdout)
            self.assertIn("the intent stays open", proc.stdout)
            restore()
            status = self._run(["status", self.journal])
            self.assertEqual(status.returncode, 3)
            self.assertTrue(status.stdout.strip().endswith("1 open, 0 done, 0 cleared"))
        finally:
            restore()

    def test_status_of_a_missing_journal_and_json(self):
        """source: a journal that does not exist yet reports empty counts, and json status is one object."""
        missing = self._run(["status", self.journal])
        self.assertEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, "0 open, 0 done, 0 cleared\n")
        as_json = self._run(["status", self.journal, "--json"])
        self.assertEqual(as_json.returncode, 0)
        payload = json.loads(as_json.stdout)
        self.assertEqual(payload["open"], [])
        self.assertEqual(payload["done"], 0)
        self.assertEqual(payload["cleared"], 0)
        self.assertIs(payload["torn_tail"], False)

    def test_status_lists_open_intents_and_a_cut_off_tail(self):
        """source: status lists each open intent, notes a cut-off last line, and ends with the counts."""
        self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/true",
        ])
        self._run([
            "run", self.journal, "--what", "invoice 1043", "--key", "invoice-1043",
            "--", "/usr/bin/false",
        ])
        with open(self.journal, "ab") as handle:
            handle.write(b'{"event":"intent","id":"cccccccccccc"')
        proc = self._run(["status", self.journal])
        self.assertEqual(proc.returncode, 3)
        self.assertIn("invoice 1043", proc.stdout)
        self.assertIn("note: the last line was cut off mid-write and is ignored", proc.stdout)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "1 open, 1 done, 0 cleared")
        as_json = self._run(["status", "--json", self.journal])
        self.assertEqual(as_json.returncode, 3)
        payload = json.loads(as_json.stdout)
        self.assertEqual(len(payload["open"]), 1)
        self.assertIs(payload["torn_tail"], True)
        self.assertEqual(payload["done"], 1)

    def test_resolve_from_the_tool(self):
        """source: resolve records a checked intent, and rejects an empty note or an id that is not open."""
        self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/false",
        ])
        status = self._run(["status", self.journal])
        intent_id = status.stdout.split()[0]
        empty = self._run([
            "resolve", self.journal, intent_id, "--did-not-happen", "--note", "",
        ])
        self.assertEqual(empty.returncode, 2)
        self.assertIn("note is empty", empty.stdout)
        bad = self._run([
            "resolve", self.journal, "abcdefabcdef", "--happened", "--note", "checked the outbox",
        ])
        self.assertEqual(bad.returncode, 2)
        self.assertIn("no such intent", bad.stdout)
        done = self._run([
            "resolve", self.journal, intent_id, "--happened", "--note", "checked the outbox",
        ])
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "RESOLVED: %s happened\n" % intent_id)
        closed = self._run([
            "resolve", self.journal, intent_id, "--did-not-happen", "--note", "checked again",
        ])
        self.assertEqual(closed.returncode, 2)
        self.assertIn("is not open", closed.stdout)
        again = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/true",
        ])
        self.assertEqual(again.returncode, 0)
        self.assertIn("NOTHING TO DO:", again.stdout)

    def test_recover_three_proof_answers(self):
        """source: a proof command maps exit 0, exit 1, and any other exit onto happened, did not happen, and still open."""
        pairs = (
            ("invoice-1042", "invoice 1042"),
            ("invoice-1043", "invoice 1043"),
            ("notice-17", "notice 17"),
        )
        for key, what in pairs:
            proc = self._run([
                "run", self.journal, "--what", what, "--key", key, "--block", "key",
                "--", "/usr/bin/false",
            ])
            self.assertEqual(proc.returncode, 1)
        prove = (
            'if [ "$INTENT_WHAT" = "invoice 1042" ]; then exit 0; fi; '
            'if [ "$INTENT_WHAT" = "invoice 1043" ]; then exit 1; fi; '
            "exit 2"
        )
        proc = self._run(["recover", self.journal, "--prove", prove])
        self.assertEqual(proc.returncode, 3)
        text = proc.stdout
        self.assertIn(" happened\n", text)
        self.assertIn(" did not happen\n", text)
        self.assertRegex(text, r"[0-9a-f]{12} unknown \(exit 2\)")
        self.assertEqual(
            text.strip().splitlines()[-1],
            "recovered 2 of 3: 1 happened, 1 did not happen, 1 still open",
        )
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3)
        self.assertTrue(status.stdout.strip().endswith("1 open, 1 done, 1 cleared"))

    def test_recover_dry_run_prints_would_and_keeps_the_bytes(self):
        """source: a dry run of recovery says what it would record and leaves the journal byte for byte the same."""
        self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/usr/bin/false",
        ])
        with open(self.journal, "rb") as handle:
            before = handle.read()
        proc = self._run([
            "recover", self.journal, "--prove", "exit 0", "--dry-run",
        ])
        with open(self.journal, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)
        self.assertIn("would: ", proc.stdout)
        self.assertIn(" happened", proc.stdout)
        self.assertTrue(proc.stdout.strip().endswith("recovered 1 of 1: 1 happened, 0 did not happen, 0 still open"))
        self.assertEqual(proc.returncode, 0)

    def test_usage_errors_are_one_line_on_stderr(self):
        """source: a usage mistake exits with the usage code and one line on stderr that starts with usage."""
        proc = self._run([])
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.startswith("usage:"))
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertEqual(proc.stdout, "")
        other = self._run(["dance"])
        self.assertEqual(other.returncode, 2)
        self.assertTrue(other.stderr.startswith("usage:"))

    def test_signal_ended_step_stays_open_under_failure_is_clean(self):
        """source: a step that a signal ends stays open even when a failure is promised clean, so the same key does not run again."""
        outbox = os.path.join(self.tmp.name, "outbox")
        command = ["/bin/sh", "-c", 'echo sent >> "$1"; kill -9 $$', "sh", outbox]
        args = [
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--failure-is-clean", "--",
        ] + command
        proc = self._run(args)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "")
        self.assertRegex(
            proc.stdout,
            r"^FAILED: /bin/sh was killed by signal 9; the intent stays open \([0-9a-f]{12}\)\n$",
        )
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout.strip().splitlines()[-1], "1 open, 0 done, 0 cleared")
        second = self._run(args)
        self.assertEqual(second.returncode, 3)
        self.assertEqual(second.stderr, "")
        self.assertTrue(second.stdout.startswith("STOP:"))
        with open(outbox, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().splitlines()), 1)
        other_journal = os.path.join(self.tmp.name, "journal-plain")
        other_outbox = os.path.join(self.tmp.name, "outbox-plain")
        plain = self._run([
            "run", other_journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--", "/bin/sh", "-c", 'echo sent >> "$1"; kill -9 $$', "sh", other_outbox,
        ])
        self.assertEqual(plain.returncode, 1)
        self.assertEqual(plain.stderr, "")
        self.assertRegex(
            plain.stdout,
            r"^FAILED: /bin/sh was killed by signal 9; the intent stays open \([0-9a-f]{12}\)\n$",
        )

    def test_exit_above_128_stays_open_under_failure_is_clean(self):
        """source: an exit past the signal boundary stays open even when a failure is promised clean."""
        for code in (129, 137):
            journal = os.path.join(self.tmp.name, "above-%s" % code)
            proc = self._run([
                "run", journal, "--what", "invoice 1042", "--key", "invoice-1042",
                "--failure-is-clean", "--", "/bin/sh", "-c", "exit %d" % code,
            ])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stderr, "")
            self.assertRegex(
                proc.stdout,
                r"^FAILED: /bin/sh exited %d; the intent stays open \([0-9a-f]{12}\)\n$" % code,
            )
            status = self._run(["status", journal])
            self.assertEqual(status.returncode, 3)
            self.assertEqual(status.stderr, "")
            self.assertEqual(status.stdout.strip().splitlines()[-1], "1 open, 0 done, 0 cleared")

    def test_exit_128_is_still_a_clean_failure(self):
        """source: an exit on the clean side of the signal boundary is recorded as did not happen when a failure is promised clean."""
        proc = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--failure-is-clean", "--", "/bin/sh", "-c", "exit 128",
        ])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "")
        self.assertRegex(
            proc.stdout,
            r"^FAILED: /bin/sh exited 128; recorded as did not happen \([0-9a-f]{12}\)\n$",
        )
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 0)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout, "0 open, 0 done, 1 cleared\n")

    def test_journal_in_a_missing_folder_is_not_opened(self):
        """source: a journal whose folder does not exist is not opened, and nothing is run or written."""
        journal = os.path.join(self.tmp.name, "nodir", "journal")
        opened = r"^COULD NOT: the journal could not be opened \(.+\); nothing was run or written\n$"
        marker = os.path.join(self.tmp.name, "run-marker")
        code = "open(%r, 'w', encoding='utf-8').write('x')" % marker
        run = self._run(["run", journal, "--what", "invoice 1042", "--", PY, "-c", code])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        self.assertRegex(run.stdout, opened)
        resolve = self._run([
            "resolve", journal, "abcdefabcdef", "--did-not-happen",
            "--note", "checked the outbox",
        ])
        self.assertEqual(resolve.returncode, 2)
        self.assertEqual(resolve.stderr, "")
        self.assertRegex(resolve.stdout, opened)
        recover_marker = os.path.join(self.tmp.name, "recover-marker")
        recover = self._run([
            "recover", journal, "--prove", "printf x > '%s'" % recover_marker,
        ])
        self.assertEqual(recover.returncode, 2)
        self.assertEqual(recover.stderr, "")
        self.assertRegex(recover.stdout, opened)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(recover_marker))
        status = self._run(["status", journal])
        self.assertEqual(status.returncode, 0)
        self.assertEqual(status.stderr, "")
        self.assertEqual(status.stdout, "0 open, 0 done, 0 cleared\n")

    def test_journal_that_is_a_folder_is_not_opened(self):
        """source: a journal path that is a folder cannot be opened, so status, resolve, and recover say so."""
        os.mkdir(self.journal)
        opened = r"^COULD NOT: the journal could not be opened \(.+\); nothing was run or written\n$"
        commands = (
            ["status", self.journal],
            ["status", self.journal, "--json"],
            ["resolve", self.journal, "abcdefabcdef", "--happened", "--note", "checked the outbox"],
            ["recover", self.journal, "--prove", "exit 0"],
        )
        for args in commands:
            proc = self._run(args)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr, "")
            self.assertRegex(proc.stdout, opened)

    def test_recover_keeps_settled_lines_when_a_later_write_fails(self):
        """source: when a later resolution cannot be written, recover prints the intents it already settled and names the one that stays open."""
        first = self._run([
            "run", self.journal, "--what", "invoice 1042", "--key", "invoice-1042",
            "--block", "key", "--", "/usr/bin/false",
        ])
        second = self._run([
            "run", self.journal, "--what", "invoice 1043", "--key", "invoice-1043",
            "--block", "key", "--", "/usr/bin/false",
        ])
        self.assertEqual(first.returncode, 1)
        self.assertEqual(first.stderr, "")
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stderr, "")
        text_a = first.stdout.rstrip("\n")
        text_b = second.stdout.rstrip("\n")
        intent_a = text_a[text_a.rindex("(") + 1:text_a.rindex(")")]
        intent_b = text_b[text_b.rindex("(") + 1:text_b.rindex(")")]
        mark = os.path.join(self.tmp.name, "mark")
        env = os.environ.copy()
        env["J"] = self.journal
        env["MARK"] = mark
        prove = 'if [ -e "$MARK" ]; then mv "$J" "$J.kept" && mkdir "$J"; fi; : > "$MARK"; exit 0'
        kept = self.journal + ".kept"
        try:
            proc = self._run(["recover", self.journal, "--prove", prove], env=env)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr, "")
            self.assertRegex(
                proc.stdout,
                "^%s happened\nCOULD NOT: the resolution could not be written \\(.+\\); the intent stays open \\(%s\\)\n$"
                % (intent_a, intent_b),
            )
        finally:
            if os.path.isdir(self.journal):
                os.rmdir(self.journal)
            if os.path.isfile(kept) and not os.path.exists(self.journal):
                os.rename(kept, self.journal)
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3)
        self.assertEqual(status.stderr, "")
        self.assertIn(intent_b, status.stdout)
        self.assertEqual(status.stdout.strip().splitlines()[-1], "1 open, 1 done, 0 cleared")


if __name__ == "__main__":
    unittest.main()
