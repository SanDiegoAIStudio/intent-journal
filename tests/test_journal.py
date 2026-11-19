"""Library tests for the intent journal."""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intent_journal import Journal, JournalDamaged, OpenIntents  # noqa: E402


def _boom():
    raise RuntimeError("stopped in the step")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal")
        self.journal = Journal(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _text(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def _bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def _records(self):
        rows = []
        for line in self._text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append(None)
        return rows

    def test_intent_is_on_disk_before_the_step(self):
        """source: the intent line is already in the journal when the step runs, so a crash cannot hide that the step was attempted."""
        seen = {}

        def step():
            if not os.path.exists(self.path):
                seen["body"] = ""
                return
            with open(self.path, encoding="utf-8") as handle:
                seen["body"] = handle.read()

        outcome = self.journal.run_once(step, "invoice 1042", key="invoice-1042")
        self.assertEqual(outcome.state, "did")
        self.assertIn(outcome.intent_id, seen.get("body", ""))
        self.assertIn('"event":"intent"', seen.get("body", ""))
        self.assertNotIn('"event":"done"', seen.get("body", ""))
        self.assertEqual(self.journal.status()["done"], 1)
        self.assertEqual(self.journal.status()["open"], 0)
        self.assertIn('"event":"done"', self._text())

    def test_unwritable_journal_does_not_start_the_step(self):
        """source: when the intent line cannot be stored, the step must not start."""
        os.mkdir(self.path)
        ran = []

        def step():
            ran.append(1)

        with self.assertRaises(Exception):
            self.journal.run_once(step, "invoice 1042")
        self.assertEqual(ran, [])

    def test_done_key_does_not_run_again(self):
        """source: a key whose newest closed intent is done has no work left, and the step is not called."""
        calls = []

        def step():
            calls.append(1)

        first = self.journal.run_once(step, "invoice 1042", key="invoice-1042")
        second = self.journal.run_once(step, "invoice 1042", key="invoice-1042")
        self.assertEqual(first.state, "did")
        self.assertEqual(second.state, "nothing_to_do")
        self.assertEqual(second.intent_id, first.intent_id)
        self.assertEqual(calls, [1])
        record = self._records()[0]
        self.assertEqual(len(record["id"]), 12)
        self.assertRegex(record["id"], r"^[0-9a-f]{12}$")
        self.assertRegex(record["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_cleared_key_runs_again(self):
        """source: a key resolved as did not happen may run again, and after that success the key has no work left."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042", failure_is_clean=True)
        if self.journal.status()["open"]:
            self.fail("cleared key was still open")
        calls = []

        def step():
            calls.append(1)

        try:
            outcome = self.journal.run_once(step, "invoice 1042", key="invoice-1042")
        except OpenIntents:
            self.fail("cleared key was treated as open")
        self.assertEqual(outcome.state, "did")
        self.assertEqual(calls, [1])
        again = self.journal.run_once(step, "invoice 1042", key="invoice-1042")
        self.assertEqual(again.state, "nothing_to_do")
        self.assertEqual(calls, [1])

    def test_failure_is_clean_clears_the_intent(self):
        """source: when a step fails and the caller promised the failure is clean, the intent is recorded as did not happen."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", failure_is_clean=True)
        status = self.journal.status()
        self.assertEqual(status["cleared"], 1)
        self.assertEqual(status["open"], 0)
        body = self._text()
        self.assertIn('"event":"failed"', body)
        self.assertIn('"happened":false', body)

    def test_failed_record_leaves_the_intent_open(self):
        """source: a failed record is information only and does not close the intent."""
        try:
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042")
        except RuntimeError as err:
            self.assertIn("stopped", str(err))
        else:
            self.fail("the step exception did not come back out")
        self.assertEqual(self.journal.status()["open"], 1)
        self.assertEqual(self.journal.status()["done"], 0)
        body = self._text()
        self.assertIn('"event":"failed"', body)
        self.assertIn("RuntimeError:", body)

    def test_failure_detail_keeps_the_type_and_caps_the_text(self):
        """source: a failed record names the exception type and keeps only the first stretch of its text."""
        def step():
            raise RuntimeError("y" * 400)

        with self.assertRaises(RuntimeError):
            self.journal.run_once(step, "invoice 1042")
        failed = [row for row in self._records() if row and row.get("event") == "failed"][0]
        self.assertTrue(failed["detail"].startswith("RuntimeError: "))
        self.assertIn("y" * 200, failed["detail"])
        self.assertNotIn("y" * 201, failed["detail"])

    def test_block_all_stops_every_key(self):
        """source: with block all, one open intent stops every later run."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042")
        ran = []

        def step():
            ran.append(1)

        with self.assertRaises(OpenIntents) as caught:
            self.journal.run_once(step, "invoice 1043", key="other")
        self.assertEqual(ran, [])
        self.assertEqual(caught.exception.intents[0]["what"], "invoice 1042")

    def test_block_key_stops_only_its_key(self):
        """source: block key holds back the key that is still open and lets another key run."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042", block="key")
        ran = []

        def step():
            ran.append(1)

        try:
            outcome = self.journal.run_once(step, "invoice 1043", key="invoice-1043", block="key")
        except OpenIntents:
            self.fail("block key stopped a different key")
        self.assertEqual(outcome.state, "did")
        self.assertEqual(ran, [1])
        with self.assertRaises(OpenIntents) as caught:
            self.journal.run_once(step, "invoice 1042 again", key="invoice-1042", block="key")
        self.assertEqual(caught.exception.intents[0]["key"], "invoice-1042")

    def test_block_key_without_a_key_is_not_stopped(self):
        """source: a run that has no key, under block key, is not stopped by open intents."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042", block="key")

        def step():
            return None

        try:
            outcome = self.journal.run_once(step, "invoice 1043", block="key")
        except OpenIntents:
            self.fail("a run with no key was stopped under block key")
        self.assertEqual(outcome.state, "did")

    def test_open_intents_are_oldest_first(self):
        """source: open intents are listed from the oldest record to the newest."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042", block="key")
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1043", key="invoice-1043", block="key")
        found = self.journal.open_intents()
        self.assertEqual([row["what"] for row in found], ["invoice 1042", "invoice 1043"])
        self.assertEqual(len(self.journal.open_intents(key="invoice-1043")), 1)

    def test_resolve_rejects_an_empty_note_and_a_closed_id(self):
        """source: resolve demands a note and an intent that is still open."""
        outcome = self.journal.run_once(lambda: None, "invoice 1042", key="invoice-1042")
        with self.assertRaises(ValueError):
            self.journal.resolve(outcome.intent_id, True, "")
        with self.assertRaises(ValueError):
            self.journal.resolve(outcome.intent_id, True, "   ")
        with self.assertRaises(ValueError):
            self.journal.resolve("abcdefabcdef", True, "checked the outbox")
        with self.assertRaises(ValueError):
            self.journal.resolve(outcome.intent_id, False, "checked the outbox")

    def test_resolve_happened_marks_the_key_done(self):
        """source: resolving an open intent as happened means that key has no work left."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042")
        intent_id = self.journal.open_intents()[0]["id"]
        try:
            self.journal.resolve(intent_id, True, "checked the outbox")
        except ValueError as err:
            self.fail("open intent was refused: %s" % err)
        self.assertEqual(self.journal.status()["open"], 0)
        self.assertEqual(self.journal.status()["done"], 1)
        calls = []
        outcome = self.journal.run_once(lambda: calls.append(1), "invoice 1042", key="invoice-1042")
        self.assertEqual(outcome.state, "nothing_to_do")
        self.assertEqual(outcome.intent_id, intent_id)
        self.assertEqual(calls, [])

    def test_recover_three_answers_and_a_broken_proof(self):
        """source: proof answers of happened, did not happen, and unknown, and a proof that raises, leave only the unknown ones open."""
        for key, what in (("a", "invoice 1042"), ("b", "invoice 1043"), ("c", "invoice 1044"), ("d", "notice 17")):
            with self.assertRaises(RuntimeError):
                self.journal.run_once(_boom, what, key=key, block="key")

        def prove(intent):
            if intent["what"] == "invoice 1042":
                return True
            if intent["what"] == "invoice 1043":
                return False
            if intent["what"] == "invoice 1044":
                return None
            raise RuntimeError("checker broke")

        counts = self.journal.recover(prove)
        self.assertEqual(counts, {"happened": 1, "did_not_happen": 1, "unknown": 2})
        self.assertEqual(self.journal.status()["done"], 1)
        self.assertEqual(self.journal.status()["cleared"], 1)
        self.assertEqual(self.journal.status()["open"], 2)
        notes = [row.get("note") for row in self._records() if row and row.get("event") == "resolved"]
        self.assertEqual(notes, ["proved", "proved"])

    def test_recover_dry_run_does_not_change_the_journal(self):
        """source: a dry run of recovery counts what it would do and leaves the journal byte for byte the same."""
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042")
        before = self._bytes()
        counts = self.journal.recover(lambda intent: True, dry_run=True)
        after = self._bytes()
        self.assertEqual(before, after)
        self.assertEqual(counts["happened"], 1)
        self.assertEqual(self.journal.status()["open"], 1)

    def test_cutoff_tail_is_ignored_and_marked_on_the_next_append(self):
        """source: a last line cut off mid-write is ignored, and the next append marks it so later reads still work."""
        self.journal.run_once(lambda: None, "invoice 1042", key="invoice-1042")
        with open(self.path, "ab") as handle:
            handle.write(b'{"event":"intent","id":"bbbbbbbbbbbb"')
        try:
            status = self.journal.status()
        except JournalDamaged:
            self.fail("a cut-off last line was treated as damage")
        self.assertTrue(status["torn_tail"])
        self.assertEqual(status["open"], 0)
        self.assertEqual(status["done"], 1)
        self.journal.run_once(lambda: None, "invoice 1043", key="invoice-1043")
        body = self._text()
        self.assertIn('"event":"torn"', body)
        try:
            again = self.journal.status()
        except JournalDamaged:
            self.fail("later read rejected the journal after the cut-off line")
        self.assertFalse(again["torn_tail"])
        self.assertEqual(again["done"], 2)

    def test_valid_record_missing_its_newline_is_kept(self):
        """source: a last line that is a whole record missing only its newline is kept, and a later append does not mark it torn."""
        self.journal.run_once(lambda: None, "invoice 1042", key="invoice-1042")
        with open(self.path, "rb") as handle:
            data = handle.read()
        self.assertTrue(data.endswith(b"\n"))
        with open(self.path, "wb") as handle:
            handle.write(data[:-1])
        status = self.journal.status()
        self.assertFalse(status["torn_tail"])
        self.assertEqual(status["done"], 1)
        self.journal.run_once(lambda: None, "invoice 1043", key="invoice-1043")
        body = self._text()
        self.assertNotIn('"event":"torn"', body)
        self.assertTrue(body.endswith("\n"))
        self.assertEqual(self.journal.status()["done"], 2)

    def test_damaged_middle_line_refuses_the_journal(self):
        """source: a line in the middle that is not a record stops the read, and nothing is run."""
        self.journal.run_once(lambda: None, "invoice 1042", key="invoice-1042")
        with open(self.path, "rb") as handle:
            data = handle.read()
        lines = data.split(b"\n")
        damaged = lines[0] + b"\n" + b"not-a-record\n" + lines[1] + b"\n"
        with open(self.path, "wb") as handle:
            handle.write(damaged)
        with self.assertRaises(JournalDamaged) as caught:
            self.journal.status()
        self.assertEqual(caught.exception.line_number, 2)
        ran = []
        with self.assertRaises(JournalDamaged):
            self.journal.run_once(lambda: ran.append(1), "invoice 1043")
        self.assertEqual(ran, [])

    def test_resolve_says_so_when_its_write_fails(self):
        """source: when a resolution cannot be written, resolve says so and leaves the intent open."""
        import contextlib
        import io
        import unittest.mock
        import intent_journal
        with self.assertRaises(RuntimeError):
            self.journal.run_once(_boom, "invoice 1042", key="invoice-1042")
        intent_id = self.journal.open_intents()[0]["id"]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with unittest.mock.patch.object(
            intent_journal.Journal,
            "_append",
            side_effect=OSError(28, "No space left on device"),
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = intent_journal.main([
                    "resolve", self.path, intent_id, "--happened", "--note", "checked the outbox",
                ])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue(),
            "COULD NOT: the resolution could not be written ([Errno 28] No space left on device); the intent stays open (%s)\n"
            % intent_id,
        )
        self.assertEqual(Journal(self.path).status()["open"], 1)


if __name__ == "__main__":
    unittest.main()
