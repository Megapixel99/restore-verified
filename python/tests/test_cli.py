"""The CLI's own contract, and most of it is about the numbers.

A CI file branches on the exit status and on nothing else — it does not read the stderr
prose — so a status that is wrong, or that differs from the JavaScript half's for the
same event, IS the failure. These are the tests that would have caught a negative
`returncode` handed to the interpreter as an exit status, and a documented pair of flags
that ended in a traceback rather than in the explanation the code had already written.
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # python/, which is where the package lives
sys.path.insert(0, ROOT)

from restore_verified import Sentinel  # noqa: E402
from restore_verified.cli import EXIT_DRIFT, EXIT_TIMEOUT  # noqa: E402


def cli(*args, tmpdir=None):
    env = dict(os.environ, PYTHONPATH=ROOT)
    if tmpdir:
        # An isolated `TMPDIR` so "what did this run leave behind?" is answerable at
        # all: the shared one already holds whatever every other run left.
        env["TMPDIR"] = tmpdir
    return subprocess.run(
        [sys.executable, "-m", "restore_verified.cli", *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-cli-")
        self.path = os.path.join(self.dir, "m.txt")
        with open(self.path, "w") as fh:
            fh.write("ORIGINAL\n")


class TheExitStatus(Fixture):
    def test_a_child_killed_by_a_signal_reports_128_plus_n(self):
        # A NEGATIVE `returncode` IS NOT AN EXIT STATUS. Returning -15 from `main` made
        # the interpreter exit 241 — a number that means nothing, differs from the 143
        # every shell reports for the same event, and differed again from what the
        # JavaScript half returned for it.
        out = cli("run", "--paths", self.path, "--", "sh", "-c", "kill -TERM $$")
        self.assertEqual(out.returncode, 143, out.stderr)

    def test_a_command_that_outlives_its_deadline_exits_124(self):
        out = cli("run", "--paths", self.path, "--timeout", "0.2", "--", "sleep", "30")
        self.assertEqual(out.returncode, EXIT_TIMEOUT, out.stderr)
        self.assertIn("SIGKILLed", out.stderr)

    def test_drift_outranks_the_commands_own_verdict(self):
        out = cli("run", "--paths", self.path, "--",
                  "sh", "-c", "printf MUTATED > %s" % self.path)
        self.assertEqual(out.returncode, EXIT_DRIFT, out.stderr)

    def test_a_command_that_cannot_be_run_is_this_tool_failing(self):
        out = cli("run", "--paths", self.path, "--", "no-such-command-anywhere")
        self.assertEqual(out.returncode, 2, out.stderr)


class TheErrorPaths(Fixture):
    def test_a_missing_manifest_is_a_sentence_rather_than_a_traceback(self):
        # Exit 1 with a traceback reads to CI as "the command under test failed" rather
        # than "this tool could not run", which are opposite conclusions.
        out = cli("verify", "--manifest", "/nonexistent/nope.json")
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("restore-verified verify:", out.stderr)

    def test_restore_without_content_explains_itself(self):
        # Two documented flags, and the combination used to raise ValueError out of
        # `main` — a traceback in place of the explanation `Sentinel.restore` writes.
        manifest = os.path.join(self.dir, "digests.json")
        Sentinel.record([self.path], keep_content=False).save(manifest)
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")

        out = cli("verify", "--manifest", manifest, "--restore")
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("digests but no content", out.stderr)

    def test_a_mistyped_command_does_not_leave_the_snapshot_behind(self):
        # The snapshots are on disk before the command is even spawned. Returning on
        # `FileNotFoundError` without discarding them left a copy of the tree in the
        # temp directory for every typo.
        scratch_home = os.path.join(self.dir, "tmp")
        os.makedirs(scratch_home)

        out = cli("run", "--paths", self.path, "--keep-content", "--",
                  "no-such-command-anywhere", tmpdir=scratch_home)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertEqual(
            os.listdir(scratch_home), [], "the snapshot outlived the failed run"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
