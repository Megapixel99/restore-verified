"""The two halves agree about the manifest, or the manifest is not a contract.

THIS IS THE REASON THE TWO HALVES SHIP FROM ONE REPOSITORY. `Sentinel` exists because
the process that breaks a tree may be gone — killed, timed out, crashed — by the time
anybody asks whether the tree came back. That process is frequently not the one doing
the asking, and it is frequently not the same language: a Node build script invokes a
Python codemod, or a Python CI harness shells out to `jscodeshift`.

So the manifest has to be ONE DOCUMENT, and "one document" is a claim about bytes rather
than about intentions. These tests round-trip a real manifest in both directions and
compare the verdicts, because two implementations that merely resemble each other are
two implementations that will disagree on the day it matters.

The tests skip — loudly, with a reason — when `node` is not on PATH, so a Python-only
contributor can still run the suite. CI asserts they were not skipped, because a skipped
parity test and a passing one look identical in a tally.

THE FOUR HAZARDS THIS FAMILY HAS ACTUALLY SHIPPED, so the next person writing a row here
starts from a checklist rather than from imagination. Every one of them was found after
release, and every one was invisible to the tests that existed at the time:

  1. UNIT OF MEASURE. `--timeout 600` meant ten minutes to one half and six tenths of a
     second to the other (this package, 0.1.2; and again in `didrun` 0.1.2-0.1.4, which
     is the reason this list is prose the next repo can copy rather than a library it
     would have to depend on).
  2. AN UNENFORCED REQUIRED FLAG. `run` with no `--paths` recorded nothing, watched
     nothing, and reported that the tree came back. The Python half got the refusal free
     from `required=True`; the JavaScript half had to write it down and did not. A
     guarantee one half gets from its parser is a guarantee nobody wrote a test for.
  3. ERROR-PATH EXIT CODES. Every parity test here was happy-path until the row below
     existed. `--timeout 0` was a usage error on npm and an expired deadline on PyPI.
  4. A DEADLINE THAT FIRES BUT DOES NOT BOUND. Asserting that a timeout *fires* is not
     asserting *when*. `didrun` 0.1.5 passed a purpose-written unit test while a
     `--timeout 2` run took five seconds, because the kill reached the child and the
     grandchild held the pipe open. Comparison cannot catch this: two halves that both
     overrun agree perfectly. It needs a flat assertion on elapsed time, which is why
     one lives below next to the comparative rows.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # python/
REPO = os.path.dirname(ROOT)
JS = os.path.join(REPO, "js", "src", "index.js")
sys.path.insert(0, ROOT)

from restore_verified import Sentinel  # noqa: E402
from restore_verified.cli import EXIT_TIMEOUT  # noqa: E402
from restore_verified.sentinel import MANIFEST_VERSION  # noqa: E402

NODE = shutil.which("node")


def halves():
    """The two shipped command lines, which is the only place the contract is real.

    Both are invoked as the user invokes them rather than imported, because every hazard
    in the module docstring lived in argument handling — the layer an import skips.
    """
    return (
        ("python", [sys.executable, "-m", "restore_verified.cli"]),
        ("javascript", [NODE, os.path.join(REPO, "js", "src", "cli.js")]),
    )


def run_node(script):
    """Run `script` with the JS half importable, and return parsed stdout."""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node is not on PATH, so the cross-half contract cannot be checked")
class TheManifestIsOneDocument(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-parity-")
        self.file = os.path.join(self.dir, "m.txt")
        with open(self.file, "w") as fh:
            fh.write("ORIGINAL\n")

    def test_the_two_halves_agree_on_the_version_number(self):
        # A version that disagrees is a manifest one half silently refuses.
        js = run_node(
            f"import {{ MANIFEST_VERSION }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify(MANIFEST_VERSION));"
        )
        self.assertEqual(js, MANIFEST_VERSION)

    def test_the_two_halves_agree_on_the_drift_exit_code(self):
        # A CI file branches on this number and must not have to ask which half ran.
        from restore_verified.cli import EXIT_DRIFT

        js = run_node(
            f"import {{ EXIT_DRIFT }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify(EXIT_DRIFT));"
        )
        self.assertEqual(js, EXIT_DRIFT)

    def test_the_two_halves_agree_on_the_timeout_exit_code(self):
        # The other number a CI file branches on. `timeout(1)`'s 124, from both halves.
        from restore_verified.cli import EXIT_TIMEOUT

        js = run_node(
            f"import {{ EXIT_TIMEOUT }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify(EXIT_TIMEOUT));"
        )
        self.assertEqual(js, EXIT_TIMEOUT)

    def test_the_two_halves_measure_TIMEOUT_IN_THE_SAME_UNIT(self):
        """The flag is seconds in both halves, and this is the test that says so.

        THE JAVASCRIPT HALF PASSED IT TO `spawn` UNCONVERTED, and `spawn` takes
        milliseconds. `--timeout 600` therefore meant ten minutes to the Python half and
        six tenths of a second to the JavaScript one, while one README documented both
        and both shipped a binary of the same name. A CI file that set a deadline got a
        different deadline depending on which half won the PATH — and the failure was
        silent, because a command killed early still produces a plausible-looking report.

        Asserted from the outside, through both real CLIs, because the unit is a
        property of the command line rather than of either implementation.
        """
        for label, argv in (
            ("python", [sys.executable, "-m", "restore_verified.cli"]),
            ("javascript", [NODE, os.path.join(REPO, "js", "src", "cli.js")]),
        ):
            with self.subTest(half=label):
                env = dict(os.environ, PYTHONPATH=ROOT)

                # A one-second command under a five-SECOND deadline must survive. Read
                # as milliseconds, five is a deadline of five thousandths of a second.
                survived = subprocess.run(
                    argv + ["run", "--paths", self.file, "--timeout", "5",
                            "--", "sleep", "1"],
                    capture_output=True, text=True, env=env, timeout=120,
                )
                self.assertEqual(
                    survived.returncode, 0,
                    f"{label} killed a 1s command under a 5s deadline: {survived.stderr}",
                )

                # And the deadline still bites when it is genuinely exceeded.
                killed = subprocess.run(
                    argv + ["run", "--paths", self.file, "--timeout", "0.3",
                            "--", "sleep", "30"],
                    capture_output=True, text=True, env=env, timeout=120,
                )
                self.assertEqual(killed.returncode, EXIT_TIMEOUT, killed.stderr)

    def test_the_two_halves_skip_the_same_directories(self):
        # Two halves that disagreed about what they walked would disagree about whether
        # a tree came back.
        from restore_verified.sentinel import SKIP_DIRS

        js = run_node(
            f"import {{ SKIP_DIRS }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify([...SKIP_DIRS].sort()));"
        )
        self.assertEqual(sorted(js), sorted(SKIP_DIRS))

    def test_python_writes_a_manifest_that_javascript_verifies(self):
        sentinel = Sentinel.record([self.file], keep_content=True)
        manifest = sentinel.save(os.path.join(self.dir, "from-python.json"))

        # Break the tree AFTER recording, exactly as a killed harness would leave it.
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        drift = run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.load({json.dumps(manifest)});"
            f"console.log(JSON.stringify(s.verify().map(d => [d.kind, d.path])));"
        )
        self.assertEqual(len(drift), 1, "the JavaScript half did not see the change")
        self.assertEqual(drift[0][0], "changed")
        self.assertEqual(drift[0][1], os.path.abspath(self.file))

    def test_javascript_writes_a_manifest_that_python_verifies(self):
        manifest = os.path.join(self.dir, "from-js.json")
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: true }});"
            f"console.log(JSON.stringify(s.save({json.dumps(manifest)})));"
        )
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        drift = Sentinel.load(manifest).verify()
        self.assertEqual(len(drift), 1, "the Python half did not see the change")
        self.assertEqual(drift[0].kind, "changed")
        self.assertEqual(drift[0].path, os.path.abspath(self.file))

    def test_a_manifest_written_by_javascript_can_be_RESTORED_by_python(self):
        # Verification is the cheap half. Restoring across the boundary means the
        # content copies referenced by the manifest are found and used by the other
        # runtime — which is the part that actually gets somebody's work back.
        manifest = os.path.join(self.dir, "restorable.json")
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: true }});"
            f"console.log(JSON.stringify(s.save({json.dumps(manifest)})));"
        )
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        still = Sentinel.load(manifest).restore()
        self.assertEqual(still, [], "Python could not restore from a JavaScript manifest")
        with open(self.file) as fh:
            self.assertEqual(fh.read(), "ORIGINAL\n")

    def test_the_two_halves_produce_BYTE_IDENTICAL_manifests_for_one_tree(self):
        """The strongest form of the claim, and the one that catches quiet drift.

        Verdicts agreeing proves the two halves read the document the same way. Bytes
        agreeing proves they WRITE it the same way, which is what makes a manifest
        diffable across the boundary and what stops one half adding a field the other
        will silently ignore.
        """
        py_manifest = os.path.join(self.dir, "py.json")
        js_manifest = os.path.join(self.dir, "js.json")
        Sentinel.record([self.file], keep_content=False).save(py_manifest)
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: false }});"
            f"console.log(JSON.stringify(s.save({json.dumps(js_manifest)})));"
        )
        with open(py_manifest) as fh:
            py = json.load(fh)
        with open(js_manifest) as fh:
            js = json.load(fh)

        # `root` is the working directory of whichever process recorded, and `scratch`
        # is a temp path. Neither is part of the contract; everything else is.
        for doc in (py, js):
            doc.pop("root", None)
            doc.pop("scratch", None)
        self.assertEqual(py, js)


if __name__ == "__main__":
    unittest.main(verbosity=2)


@unittest.skipUnless(NODE, "node is not on PATH, so the cross-half contract cannot be checked")
class TheHalvesRefuseTheSameShapes(unittest.TestCase):
    """The error paths, which is where the halves actually drifted.

    EVERY OTHER PARITY TEST IN THIS FILE IS HAPPY-PATH, and both defects this class was
    written for lived one branch off it. A half that accepts what the other refuses is
    two different programs wearing one name, and the accepting one is not the safe
    direction: `run` with no `--paths` exited 0 on npm having watched nothing, and said
    "the tree came back" while doing it.

    THE EXIT CODE IS COMPARED AND THE WORDING IS NOT, and that is a real limit rather
    than an oversight. The Python half's refusals are argparse's sentences and the
    JavaScript half's are hand-written, so `zerocase`'s word-for-word refusal table
    cannot be ported here until this half hand-writes its usage text the way `didrun`
    and `zerocase` both already do. Until then the assertion is that a shape is refused
    and refused with the same number, which is what a CI file can branch on.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-refuse-")
        self.file = os.path.join(self.dir, "m.txt")
        with open(self.file, "w") as fh:
            fh.write("ORIGINAL\n")

    # (label, argv-after-the-program). 2 is what this tool reports when it cannot run,
    # and the shapes below are the ones a person actually types.
    def shapes(self):
        return (
            ("run without --paths", ["run", "--", "true"]),
            ("record without --paths", ["record"]),
            ("verify without --manifest", ["verify"]),
            ("a zero deadline", ["run", "--paths", self.file, "--timeout", "0", "--", "true"]),
            ("a negative deadline", ["run", "--paths", self.file, "--timeout", "-1", "--", "true"]),
            ("a deadline that is not a number", ["run", "--paths", self.file, "--timeout", "abc", "--", "true"]),
            ("seconds with a unit suffix", ["run", "--paths", self.file, "--timeout", "30s", "--", "true"]),
        )

    def test_both_halves_refuse_the_same_shapes_with_the_same_status(self):
        env = dict(os.environ, PYTHONPATH=ROOT)
        for label, args in self.shapes():
            with self.subTest(shape=label):
                seen = {}
                for half, argv in halves():
                    proc = subprocess.run(argv + args, capture_output=True, text=True,
                                          env=env, timeout=120)
                    seen[half] = proc.returncode
                self.assertEqual(
                    seen["python"], seen["javascript"],
                    f"{label}: python exited {seen['python']} and javascript exited "
                    f"{seen['javascript']} for the same command line",
                )
                self.assertEqual(
                    seen["python"], 2,
                    f"{label}: expected 2 (this tool could not run), got {seen['python']}",
                )

    def test_a_run_that_watches_nothing_never_reports_that_the_tree_came_back(self):
        """The defect itself, asserted on the file rather than on the exit code.

        `restore-verified run -- cmd` with no `--paths` printed `recorded 0 file(s)` and
        then `the tree came back: every file matches the digest recorded before the run`
        and exited 0, while the command it had just run destroyed the file it was
        pointed at. A zero denominator reporting clean, under this package's own name.

        The command really does mutate the file, so a half that runs it at all fails
        here twice over — on the status and on the bytes.
        """
        env = dict(os.environ, PYTHONPATH=ROOT)
        for half, argv in halves():
            with self.subTest(half=half):
                with open(self.file, "w") as fh:
                    fh.write("ORIGINAL\n")
                proc = subprocess.run(
                    argv + ["run", "--", "sh", "-c", f"echo MUTATED > {self.file}"],
                    capture_output=True, text=True, env=env, timeout=120,
                )
                self.assertEqual(proc.returncode, 2, f"{half} ran a guard over no paths")
                self.assertNotIn("the tree came back", proc.stdout + proc.stderr)
                with open(self.file) as fh:
                    self.assertEqual(
                        fh.read(), "ORIGINAL\n",
                        f"{half} ran the command despite watching nothing",
                    )


@unittest.skipUnless(NODE, "node is not on PATH, so the cross-half contract cannot be checked")
class TheDeadlineBoundsTheRun(unittest.TestCase):
    """A timeout that fires is not a timeout that bounds, and only one of those is the promise.

    COMPARISON CANNOT CATCH THIS, which is why the assertion here is a flat number and
    not a diff between the halves. `didrun` 0.1.5 shipped a `--timeout 2` that took five
    seconds, and it did so while passing a test written one day earlier for exactly that
    flag — because that test asked whether the deadline fired, and both halves reported
    that it had. Two halves that both overrun agree perfectly, so a parity suite made
    only of comparisons is blind here by construction.

    The mechanism there was piped stdio: killing the direct child leaves a grandchild
    holding the pipe open, so the runner waits for the grandchild anyway. This half uses
    inherited stdio and does not have the bug — this row exists so that a future change
    to piped output cannot introduce it silently.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-deadline-")
        self.file = os.path.join(self.dir, "m.txt")
        with open(self.file, "w") as fh:
            fh.write("ORIGINAL\n")

    def test_a_one_second_deadline_bounds_a_thirty_second_command_in_both_halves(self):
        env = dict(os.environ, PYTHONPATH=ROOT)
        # Generous, because this asserts a BOUND rather than a stopwatch reading: a
        # loaded CI runner may take real time to start an interpreter. Ten seconds still
        # fails a deadline that did not bound a thirty-second command at all, which is
        # the defect, and does not fail on a slow machine, which is not.
        ceiling = 10.0
        for half, argv in halves():
            with self.subTest(half=half):
                # A GRANDCHILD, deliberately: `sh -c` puts a `sleep` behind the process
                # actually signalled, which is the shape that survived the sibling's
                # test. The documented usage of this flag (`-- ./harness.sh`) is exactly
                # this shape.
                started = time.monotonic()
                proc = subprocess.run(
                    argv + ["run", "--paths", self.file, "--timeout", "1",
                            "--", "sh", "-c", "sleep 30"],
                    capture_output=True, text=True, env=env, timeout=120,
                )
                elapsed = time.monotonic() - started
                self.assertEqual(proc.returncode, EXIT_TIMEOUT, proc.stderr)
                self.assertLess(
                    elapsed, ceiling,
                    f"{half} reported the deadline fired but took {elapsed:.1f}s to "
                    f"bound a 30s command under --timeout 1",
                )
