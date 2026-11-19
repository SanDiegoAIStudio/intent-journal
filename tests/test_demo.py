"""The demo script walks the six parts and checks itself."""

import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "examples", "demo.sh")


class DemoTests(unittest.TestCase):
    def test_demo_prints_the_six_parts(self):
        """source: the demo shows a finished run, a repeat with nothing left to do, a kill after the send, a stop, a proof that the send happened, and a kill before the send that is sent once the proof says it did not."""
        proc = subprocess.run(
            ["/bin/sh", DEMO],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        out = proc.stdout
        marks = [
            "(1) normal run",
            "DID: invoice 1042 (intent ",
            "exit 0",
            "one line in the outbox",
            "(2) the same key again",
            "NOTHING TO DO:",
            "still one line in the outbox",
            "(3) killed right after the send",
            "1 open, 1 done, 0 cleared",
            "exit 3",
            "(4) the next run of any key",
            "STOP:",
            "outbox unchanged",
            "(5) recover, then the stopped run",
            " happened",
            "recovered 1 of 1: 1 happened, 0 did not happen, 0 still open",
            "DID: invoice 1044 (intent ",
            "(6) killed before the send",
            " did not happen",
            "recovered 1 of 1: 0 happened, 1 did not happen, 0 still open",
            "DID: notice 17 (intent ",
            "outbox holds 4 lines, no line twice",
        ]
        pos = 0
        for mark in marks:
            found = out.find(mark, pos)
            self.assertGreaterEqual(found, 0, "%s\n%s" % (mark, out))
            pos = found + len(mark)
        self.assertTrue(out.rstrip("\n").endswith("outbox holds 4 lines, no line twice"))


if __name__ == "__main__":
    unittest.main()
