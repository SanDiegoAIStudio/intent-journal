"""Crash tests. Each one kills a runner this file started."""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "intent_journal.py")
SEND = os.path.join(ROOT, "examples", "send.sh")
PY = sys.executable


class CrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, "journal")
        self.outbox = os.path.join(self.tmp.name, "outbox")
        with open(self.outbox, "w", encoding="utf-8"):
            pass

    def tearDown(self):
        self.tmp.cleanup()

    def _outbox(self):
        with open(self.outbox, encoding="utf-8") as handle:
            return handle.read().splitlines()

    def _run(self, args, env=None):
        return subprocess.run(
            [PY, TOOL] + args, capture_output=True, text=True, env=env
        )

    def _send(self, what, key, crash=None, block=None):
        env = os.environ.copy()
        if crash is not None:
            env["CRASH"] = crash
        else:
            env.pop("CRASH", None)
        args = ["run", self.journal, "--what", what, "--key", key]
        if block is not None:
            args.extend(["--block", block])
        args.extend(["--", "/bin/sh", SEND, self.outbox, what])
        return self._run(args, env=env)

    def _open_id(self):
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3, status.stdout + status.stderr)
        return status.stdout.split()[0]

    def test_kill_after_the_step_blocks_the_next_run(self):
        """source: a runner killed after the step leaves the outbox line and one open intent, and the next run stops without sending again."""
        killed = self._send("invoice 1042", "invoice-1042", crash="after")
        self.assertNotEqual(killed.returncode, 0)
        self.assertEqual(self._outbox(), ["invoice 1042"])
        intent_id = self._open_id()
        before = self._outbox()
        nxt = self._send("invoice 1043", "invoice-1043")
        self.assertEqual(nxt.returncode, 3)
        self.assertIn("STOP:", nxt.stdout)
        self.assertIn(intent_id, nxt.stdout)
        self.assertEqual(self._outbox(), before)

    def test_resolve_happened_makes_the_same_key_nothing_to_do(self):
        """source: after a kill, resolving the open intent as happened makes the same key report that it has no work left."""
        self._send("invoice 1042", "invoice-1042", crash="after")
        intent_id = self._open_id()
        resolved = self._run([
            "resolve", self.journal, intent_id, "--happened", "--note", "checked the outbox",
        ])
        self.assertEqual(resolved.returncode, 0)
        self.assertIn("happened", resolved.stdout)
        again = self._send("invoice 1042", "invoice-1042")
        self.assertEqual(again.returncode, 0)
        self.assertIn("NOTHING TO DO:", again.stdout)
        self.assertEqual(self._outbox(), ["invoice 1042"])

    def test_resolve_did_not_happen_sends_on_the_next_run(self):
        """source: after a kill, resolving the open intent as did not happen lets the same key run, and the line arrives."""
        self._send("invoice 1042", "invoice-1042", crash="after")
        self.assertEqual(self._outbox(), ["invoice 1042"])
        intent_id = self._open_id()
        resolved = self._run([
            "resolve", self.journal, intent_id, "--did-not-happen",
            "--note", "checked the outbox",
        ])
        self.assertEqual(resolved.returncode, 0)
        again = self._send("invoice 1042", "invoice-1042")
        self.assertEqual(again.returncode, 0)
        self.assertIn("DID:", again.stdout)
        self.assertEqual(self._outbox(), ["invoice 1042", "invoice 1042"])

    def test_kill_before_the_step_leaves_an_empty_outbox(self):
        """source: a runner killed before the step leaves an open intent and an empty outbox."""
        killed = self._send("invoice 1042", "invoice-1042", crash="before")
        self.assertNotEqual(killed.returncode, 0)
        self.assertEqual(self._outbox(), [])
        status = self._run(["status", self.journal])
        self.assertEqual(status.returncode, 3)
        self.assertIn("invoice 1042", status.stdout)
        self.assertTrue(status.stdout.strip().endswith("1 open, 0 done, 0 cleared"))

    def test_block_key_stops_only_the_open_key_after_a_kill(self):
        """source: under block key, an open intent left by a kill stops its own key and lets another key run."""
        killed = self._send("invoice 1042", "invoice-1042", crash="before", block="key")
        self.assertNotEqual(killed.returncode, 0)
        self.assertEqual(self._outbox(), [])
        stopped = self._send("invoice 1042", "invoice-1042", block="key")
        self.assertEqual(stopped.returncode, 3)
        self.assertIn("STOP:", stopped.stdout)
        self.assertEqual(self._outbox(), [])
        other = self._send("invoice 1043", "invoice-1043", block="key")
        self.assertEqual(other.returncode, 0)
        self.assertIn("DID:", other.stdout)
        self.assertEqual(self._outbox(), ["invoice 1043"])


if __name__ == "__main__":
    unittest.main()
