"""The claims, each one killed by a mutation to the code it tests.

The signal tests spawn a real child and really kill it. That is slower than calling a
handler in-process and it is the only version worth having: the question is not "does
the handler run" but "what does the file on disk look like after somebody types
`kill`".
"""

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from restore_verified import RestoreFailed, Sentinel, guarded  # noqa: E402

ORIGINAL = "def f(x):\n    return x + 1\n"
CHILD = os.path.join(HERE, "child.py")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-test-")
        self.path = os.path.join(self.dir, "m.py")
        with open(self.path, "w") as fh:
            fh.write(ORIGINAL)

    def read(self):
        with open(self.path) as fh:
            return fh.read()


class TheOrdinaryPath(Fixture):
    def test_it_restores_and_the_write_took_effect(self):
        with guarded(self.path) as g:
            g.write("MUTATED\n")
            # The mutation must actually be on disk, or every other assertion here is
            # about a guard that never had anything to restore.
            self.assertEqual(self.read(), "MUTATED\n")
        self.assertEqual(self.read(), ORIGINAL)

    def test_an_exception_does_not_skip_the_restore(self):
        with self.assertRaises(ZeroDivisionError):
            with guarded(self.path) as g:
                g.write("MUTATED\n")
                1 / 0
        self.assertEqual(self.read(), ORIGINAL)

    def test_read_gives_the_original_even_after_the_write(self):
        # Reading the file back after mutating it and calling that "the original" is
        # one of the three documented ways a restore runs and does not work.
        with guarded(self.path) as g:
            g.write("MUTATED\n")
            self.assertEqual(g.read(), ORIGINAL)
        self.assertEqual(self.read(), ORIGINAL)

    def test_nothing_is_written_beside_the_code_under_test(self):
        before = set(os.listdir(self.dir))
        with guarded(self.path) as g:
            g.write("MUTATED\n")
            during = set(os.listdir(self.dir))
        self.assertEqual(during, before, "the guard left scratch state in the tree")
        self.assertEqual(set(os.listdir(self.dir)), before)

    def test_the_mtime_comes_back_too(self):
        old = os.stat(self.path).st_mtime
        time.sleep(0.01)
        with guarded(self.path) as g:
            g.write("MUTATED\n")
        self.assertAlmostEqual(os.stat(self.path).st_mtime, old, places=4)

    def test_a_file_that_did_not_exist_is_removed_again(self):
        new = os.path.join(self.dir, "created.py")
        with guarded(new) as g:
            g.write("MUTATED\n")
            self.assertTrue(os.path.exists(new))
        self.assertFalse(os.path.exists(new))

    def test_writing_an_unguarded_path_is_refused(self):
        other = os.path.join(self.dir, "other.py")
        with open(other, "w") as fh:
            fh.write("x\n")
        with guarded(self.path) as g:
            with self.assertRaises(ValueError):
                g.write(other, "MUTATED\n")


class TheVerification(Fixture):
    """`restore-verified`'s own name: a restore that ran is not a restore that worked."""

    def test_a_restore_that_writes_the_wrong_bytes_is_caught(self):
        # Sabotage the snapshot so the restore path runs perfectly and puts back
        # something that is not what was there. Nothing about the control flow is
        # wrong here — only the bytes — which is exactly the class of failure that
        # `try/finally` cannot see.
        with self.assertRaises(RestoreFailed) as caught:
            with guarded(self.path) as g:
                g.write("MUTATED\n")
                snap = g._snapshots[os.path.abspath(self.path)]
                with open(snap["copy"], "w") as fh:
                    fh.write("NOT WHAT WAS THERE\n")
        self.assertIn("different digest", str(caught.exception))

    def test_a_file_deleted_under_the_guard_is_reported(self):
        with self.assertRaises(RestoreFailed):
            with guarded(self.path) as g:
                g.write("MUTATED\n")
                snap = g._snapshots[os.path.abspath(self.path)]
                os.remove(snap["copy"])
                os.remove(self.path)

    def test_the_check_can_pass(self):
        # A verification that always failed would satisfy the two tests above and be
        # useless. This is the control.
        with guarded(self.path) as g:
            g.write("MUTATED\n")
        self.assertEqual(self.read(), ORIGINAL)


class Signals(Fixture):
    """The half `try/finally` does not cover, tested by killing a real process."""

    def spawn(self, mode):
        proc = subprocess.Popen(
            [sys.executable, CHILD, mode, self.path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), "READY", "the child never started")
        self.assertEqual(self.read(), "MUTATED\n", "the child never broke the file")
        return proc

    def kill_and_wait(self, proc, signum):
        proc.send_signal(signum)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=5)
            self.fail(f"the child ignored {signum} — a swallowed signal is a hang")
        finally:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        return proc.returncode

    def test_SIGTERM_leaves_a_try_finally_harness_broken(self):
        # THE CONTROL, and the reason this package exists. If this ever passes, the
        # premise is wrong and the guard is unnecessary.
        proc = self.spawn("naive")
        self.kill_and_wait(proc, signal.SIGTERM)
        self.assertEqual(
            self.read(), "MUTATED\n",
            "try/finally survived SIGTERM — the premise of this package is wrong",
        )

    def test_SIGTERM_does_not_leave_a_guarded_harness_broken(self):
        proc = self.spawn("guarded")
        self.kill_and_wait(proc, signal.SIGTERM)
        self.assertEqual(self.read(), ORIGINAL)

    def test_SIGINT_does_not_leave_a_guarded_harness_broken(self):
        proc = self.spawn("guarded")
        self.kill_and_wait(proc, signal.SIGINT)
        self.assertEqual(self.read(), ORIGINAL)

    def test_the_signal_is_re_delivered_so_a_kill_still_kills(self):
        # Swallowing a signal turns `kill` into "nothing happened", which is a worse
        # bug than the one being fixed. The child must die OF the signal.
        proc = self.spawn("guarded")
        code = self.kill_and_wait(proc, signal.SIGTERM)
        self.assertEqual(
            code, -signal.SIGTERM,
            f"exited {code}; a guard that catches SIGTERM and returns normally has "
            f"converted a kill into a no-op",
        )

    def test_SIGKILL_defeats_the_guard_and_the_sentinel_catches_it(self):
        # The honest limit, asserted rather than described. Nothing in-process can
        # survive SIGKILL, so the check has to live one level up.
        sentinel = Sentinel.record([self.path], keep_content=True)
        proc = self.spawn("guarded")
        self.kill_and_wait(proc, signal.SIGKILL)
        self.assertEqual(
            self.read(), "MUTATED\n",
            "SIGKILL was survived, which would mean this test is not doing what it says",
        )
        drift = sentinel.verify()
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].kind, "changed")
        self.assertEqual(sentinel.restore(), [])
        self.assertEqual(self.read(), ORIGINAL)


class OffTheMainThread(Fixture):
    """`signal.signal` only works on the main thread. Say so; do not fail silently."""

    def test_it_says_why_it_could_not_install_a_handler(self):
        note = {}

        def work():
            with guarded(self.path) as g:
                note["why"] = g.signal_note
                g.write("MUTATED\n")

        t = threading.Thread(target=work)
        t.start()
        t.join()
        self.assertEqual(self.read(), ORIGINAL, "the restore must still happen")
        self.assertIsNotNone(note["why"])
        self.assertIn("main thread", note["why"])

    def test_on_the_main_thread_there_is_nothing_to_say(self):
        with guarded(self.path) as g:
            self.assertIsNone(g.signal_note)
            g.write("MUTATED\n")


class TheSentinel(Fixture):
    def test_a_clean_run_reports_no_drift(self):
        sentinel = Sentinel.record([self.dir])
        self.assertEqual(sentinel.verify(), [])

    def test_it_distinguishes_changed_missing_and_created(self):
        # Three outcomes rather than "not clean": a missing file and a changed one
        # send you to opposite ends of the problem.
        created = os.path.join(self.dir, "new.py")
        gone = os.path.join(self.dir, "gone.py")
        with open(gone, "w") as fh:
            fh.write("bye\n")
        sentinel = Sentinel.record([self.dir, created])
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        os.remove(gone)
        with open(created, "w") as fh:
            fh.write("hello\n")
        kinds = {d.kind: d.path for d in sentinel.verify()}
        self.assertEqual(set(kinds), {"changed", "missing", "created"})
        self.assertEqual(kinds["changed"], self.path)

    def test_a_manifest_survives_the_process_that_wrote_it(self):
        # The point of the out-of-process half: the process that broke the tree is
        # gone by the time anybody asks.
        sentinel = Sentinel.record([self.path], keep_content=True)
        manifest = sentinel.save()
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        reloaded = Sentinel.load(manifest)
        self.assertEqual(len(reloaded.verify()), 1)
        self.assertEqual(reloaded.restore(), [])
        self.assertEqual(self.read(), ORIGINAL)

    def test_a_digest_only_manifest_refuses_to_pretend_it_can_restore(self):
        sentinel = Sentinel.record([self.dir])  # a directory: digests only
        self.assertFalse(sentinel.keep_content)
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        self.assertEqual(len(sentinel.verify()), 1)
        with self.assertRaises(ValueError):
            sentinel.restore()

    def test_a_manifest_from_another_version_is_refused(self):
        sentinel = Sentinel.record([self.path], keep_content=True)
        manifest = sentinel.save()
        import json
        with open(manifest) as fh:
            payload = json.load(fh)
        payload["manifest"] = 999
        with open(manifest, "w") as fh:
            json.dump(payload, fh)
        with self.assertRaises(ValueError):
            Sentinel.load(manifest)


class TheGitControl(Fixture):
    """Why this is not just `git checkout --`, stated as a test rather than a claim."""

    def git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.dir, capture_output=True,
                              text=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q", ".")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.git("add", "m.py")
        self.git("commit", "-qm", "init")

    def test_on_a_clean_tree_git_already_does_this_and_you_should_use_git(self):
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        self.assertNotEqual(self.git("diff", "--quiet").returncode, 0,
                            "git should catch an unrestored change on a clean tree")

    def test_on_a_dirty_tree_git_cannot_tell_the_two_apart(self):
        dirty = ORIGINAL.rstrip("\n") + "  # uncommitted work\n"
        with open(self.path, "w") as fh:
            fh.write(dirty)
        before = self.git("diff", "--quiet").returncode
        sentinel = Sentinel.record([self.path], keep_content=True)

        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        after = self.git("diff", "--quiet").returncode

        self.assertEqual(before, after,
                         "git's verdict must be identical before and after, or the "
                         "premise of this package is wrong")
        self.assertNotEqual(before, 0, "the fixture is not dirty, so this proves nothing")

        # The sentinel can, because its baseline is when the run started rather than
        # the last commit.
        self.assertEqual(len(sentinel.verify()), 1)
        self.assertEqual(sentinel.restore(), [])
        self.assertEqual(self.read(), dirty, "the uncommitted work must survive")

    def test_the_git_fix_destroys_uncommitted_work(self):
        dirty = ORIGINAL.rstrip("\n") + "  # uncommitted work\n"
        with open(self.path, "w") as fh:
            fh.write(dirty)
        with open(self.path, "w") as fh:
            fh.write("MUTATED\n")
        self.git("checkout", "--", "m.py")
        self.assertEqual(self.read(), ORIGINAL)
        self.assertNotEqual(self.read(), dirty,
                            "if git preserved the uncommitted work, use git")


if __name__ == "__main__":
    unittest.main(verbosity=2)
