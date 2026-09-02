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
  3. ERROR-PATH EXIT CODES, AND THEN THE WORDS. Every parity test here was happy-path
     until the row below existed, and `--timeout 0` was a usage error on npm and an
     expired deadline on PyPI. The status was the first half of it: the halves also
     refused the same command line in different sentences, because one of them was
     generating them from `argparse` and the other hand-wrote them. A person reading one
     registry's output could not search for the other's message. Both are compared now.
  4. A DEADLINE THAT FIRES BUT DOES NOT BOUND THE WORK. Asserting that a timeout
     *fires* is not asserting *when*. `--timeout` killed the command and returned 124 on
     time while the command's own children survived as orphans, still holding the stdout
     this tool inherited — so any caller that captured output waited for the work
     anyway: the tool returned in 1.2s and the caller in 20.3s. Both halves had it, so
     the comparison in this file was green throughout; two halves that both overrun
     agree perfectly, which is why the row below asserts a flat elapsed time rather than
     a diff. Fixed by putting the command in its own session and killing the group.
  5. TWO HELP TEXTS THAT SHARE NO GRAMMAR, so nothing could compare them. One half
     generated `usage: restore-verified [-h] {run,record,verify} ...` from argparse and
     the other hand-wrote prose; a CI file is written from whichever one its author read.
     Closed by hand-writing this half's too, so the bytes can be compared.
  6. AND THE COST OF FIXING 4, which is its own hazard. A command in its own session no
     longer receives the terminal's Ctrl-C, so the signals have to be forwarded by hand
     — and forwarding alone HANGS, because a background job in a non-interactive shell
     has SIGINT set to ignore. Every one of those three properties needs its own row,
     below, and none of them is a comparison.
"""

import json
import os
import shutil
import signal
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

    THE WORDS ARE COMPARED AND NOT ONLY THE STATUS. This was the other way round while
    the Python half generated its refusals from `argparse`: `error: the following
    arguments are required: --paths` against `restore-verified run: --paths is
    required`, for the same command line, from one binary name. The status is what a CI
    file branches on and the sentence is what a person searches for, and the halves
    owe both.

    ONE MESSAGE STAYS EACH HALF'S OWN, deliberately: the spawn failure for a command
    that does not exist carries the runtime's text (`spawn foo ENOENT` from Node), and
    inventing a Node-shaped sentence in Python to match would be a worse lie than the
    difference. It is not in the table below, and it is the one refusal compared on
    status alone.
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
            ("an empty deadline", ["run", "--paths", self.file, "--timeout", "", "--", "true"]),
            ("nothing to run after --", ["run", "--paths", self.file, "--"]),
            ("a flag with no value", ["run", "--paths", self.file, "--manifest"]),
            ("an unknown option", ["run", "--paths", self.file, "--nope", "--", "true"]),
            ("an unknown subcommand", ["frobnicate"]),
        )

    def test_both_halves_refuse_the_same_shapes_in_the_same_words(self):
        env = dict(os.environ, PYTHONPATH=ROOT)
        for label, args in self.shapes():
            with self.subTest(shape=label):
                seen = {}
                for half, argv in halves():
                    proc = subprocess.run(argv + args, capture_output=True, text=True,
                                          env=env, timeout=120)
                    seen[half] = proc
                self.assertEqual(
                    seen["python"].returncode, seen["javascript"].returncode,
                    f"{label}: python exited {seen['python'].returncode} and javascript "
                    f"exited {seen['javascript'].returncode} for the same command line",
                )
                self.assertEqual(
                    seen["python"].returncode, 2,
                    f"{label}: expected 2 (this tool could not run), got "
                    f"{seen['python'].returncode}",
                )
                # THE CANARY, for the same reason the usage comparison has one: two
                # empty refusals are equal, and a tool that refuses in silence is the
                # defect rather than the pass.
                self.assertTrue(
                    seen["python"].stderr.strip(),
                    f"{label}: refused with exit 2 and said nothing",
                )
                self.assertEqual(
                    seen["python"].stderr, seen["javascript"].stderr,
                    f"{label}: the halves refuse the same command line differently",
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
class TheDeadlineBoundsTheWork(unittest.TestCase):
    """A deadline that fires is not a deadline that bounds, and only one of those is the promise.

    THE COMPARISON IN THIS FILE WAS GREEN WHILE THIS WAS BROKEN, in both halves at once,
    which is the argument for every assertion in this class being a flat number. Two
    halves that both overrun agree perfectly, so a parity suite made only of diffs
    reports agreement about two runs that both ignored the deadline — this package's own
    subject aimed at its own parity suite.

    `sh -c 'X & wait'` rather than `sh -c 'X'` throughout: a shell `exec`s a single
    simple command, so the plain form leaves no grandchild to orphan and the defect
    hides completely on macOS. It did. CI on Linux is where it surfaced.
    """

    CEILING = 12.0  # a bound, not a stopwatch: starting an interpreter costs real time

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-deadline-")
        self.file = os.path.join(self.dir, "m.txt")
        with open(self.file, "w") as fh:
            fh.write("ORIGINAL\n")

    def _elapsed(self, argv, extra, sigs=()):
        env = dict(os.environ, PYTHONPATH=ROOT)
        started = time.monotonic()
        proc = subprocess.Popen(
            argv + ["run", "--paths", self.file] + extra
            + ["--", "sh", "-c", "sleep 30 & wait"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
        )
        # CAPTURED ON PURPOSE. The orphan holds the inherited stdout, so a pipe is what
        # makes the defect observable at all — with the output going to the terminal the
        # caller returns promptly and the work carries on unnoticed.
        for sig in sigs:
            time.sleep(1.2)
            proc.send_signal(sig)
        proc.communicate(timeout=120)
        return proc.returncode, time.monotonic() - started

    def _both(self, extra, sigs=()):
        return {half: self._elapsed(argv, extra, sigs) for half, argv in halves()}

    def test_a_deadline_bounds_a_caller_that_captures_output(self):
        seen = self._both(["--timeout", "1"])
        for half, (code, elapsed) in seen.items():
            self.assertEqual(code, EXIT_TIMEOUT, half)
            self.assertLess(
                elapsed, self.CEILING,
                f"{half} returned 124 on time but the caller waited {elapsed:.1f}s for "
                f"the command's orphaned children",
            )

    def test_a_forwarded_signal_reaches_the_commands_children(self):
        """The session isolation the fix needs would otherwise swallow Ctrl-C entirely."""
        seen = self._both([], sigs=(signal.SIGTERM,))
        for half, (code, elapsed) in seen.items():
            # 128+n, the convention both halves report and the one a shell agrees with.
            self.assertEqual(code, 128 + signal.SIGTERM, half)
            self.assertLess(
                elapsed, self.CEILING,
                f"{half} took {elapsed:.1f}s — the signal did not reach the tree",
            )

    def test_a_signal_the_command_ignores_does_not_hang_this_one(self):
        """A background job has SIGINT set to ignore, which is POSIX and not a quirk.

        THE STATUS IS COMPARED AND NOT NAMED, deliberately. What comes back here is the
        shell's own exit status for a `wait` that was interrupted, and shells disagree
        about it: 129 on the macOS `sh`, and there is no reason for this file to have an
        opinion about which is right. What this file is entitled to insist on is that
        both halves report the SAME one and that neither hangs — the second of which is
        a flat assertion, because two halves that both hang agree perfectly.
        """
        seen = self._both([], sigs=(signal.SIGINT,))
        self.assertEqual(
            seen["python"][0], seen["javascript"][0],
            f"the halves disagree about a command interrupted mid-wait: {seen}",
        )
        for half, (code, elapsed) in seen.items():
            self.assertNotEqual(code, 0, f"{half} reported success for an interrupted run")
            self.assertLess(
                elapsed, self.CEILING,
                f"{half} forwarded a signal the command ignores and then waited "
                f"{elapsed:.1f}s for it",
            )


@unittest.skipUnless(NODE, "node is not on PATH, so the cross-half contract cannot be checked")
class TheCommandLineSaysTheSameThing(unittest.TestCase):
    """Two copies of the usage text, and a CI file is written from whichever one was read.

    THE OUTPUT IS COMPARED AND NOT THE CONSTANTS. Two strings that differ only in how
    they are escaped render identically, and two that look identical in source can
    render differently — a source comparison gets both wrong, and it is how a sibling
    package nearly missed exactly this. So both real command lines are run.

    This is the row that `didrun` and `zerocase` already carry, and it could not be
    written here until this half stopped generating its help from argparse: the two
    texts shared no grammar, so there was nothing to compare and no test could say so.
    """

    def _help(self, argv):
        return subprocess.run(argv + ["--help"], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONPATH=ROOT), timeout=120)

    def test_the_usage_text_is_identical(self):
        seen = {half: self._help(argv) for half, argv in halves()}
        for half, out in seen.items():
            self.assertEqual(out.returncode, 0, f"{half} --help did not exit 0")
            # THE CANARY. Byte-equality between two empty strings is byte-equality, and
            # an entry point that prints nothing is the defect this file exists to
            # catch rather than a pass.
            self.assertIn("--paths", out.stderr,
                          f"{half} printed something, but it is not the flag list")
            self.assertEqual(out.stdout, "", f"{half} wrote its usage to stdout")
        self.assertEqual(
            seen["python"].stderr, seen["javascript"].stderr,
            "the two halves describe different tools under one name",
        )

    def test_every_subcommand_the_usage_names_is_one_both_halves_accept(self):
        """The list in the text is the list the parsers answer to, in both halves.

        Presence in the help proves only that somebody typed it. This asks each half
        about each subcommand the shared text advertises, and a name one half does not
        know is a name the other should not be advertising.
        """
        text = self._help(halves()[0][1]).stderr
        named = [c for c in ("run", "record", "verify") if f"restore-verified {c}" in text]
        self.assertEqual(len(named), 3, f"the usage stopped naming all three: {named}")
        for half, argv in halves():
            for cmd in named:
                with self.subTest(half=half, subcommand=cmd):
                    # No arguments, so this is refused — but refused as a MISSING FLAG
                    # (2), never as an unknown subcommand, which is what a half that had
                    # dropped it would report.
                    out = subprocess.run(argv + [cmd], capture_output=True, text=True,
                                         env=dict(os.environ, PYTHONPATH=ROOT), timeout=120)
                    self.assertEqual(out.returncode, 2, f"{half} {cmd}: {out.stderr[:200]}")
                    self.assertNotIn("unknown", (out.stderr or "").lower(),
                                     f"{half} does not know the subcommand {cmd!r}")
