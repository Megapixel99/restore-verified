"""`restore-verified` — run something that edits files, and check the tree came back.

    restore-verified run --paths src/ -- pytest mutations_run.py
    restore-verified run --paths src/ --timeout 600 --restore -- ./harness.sh

    restore-verified record --paths src/ --manifest /tmp/before.json
    restore-verified verify --manifest /tmp/before.json

`run` is the one that matters, and `--timeout` is why. Python's own
`subprocess.run(..., timeout=...)` SIGKILLs the child when the deadline passes — which
is the commonest way in practice for a file-editing tool to be killed at exactly the
moment its file is broken. This command both imposes that deadline and catches what it
does, which no test runner, CI step or mutation framework currently does.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys

from .sentinel import Sentinel, drift_report

# THE TREE BEING WRONG OUTRANKS THE COMMAND'S OWN VERDICT, so it gets an exit code of
# its own rather than reusing 1. A harness that exits 0 having left a file mutated is
# the exact failure this exists to report, and folding that into the command's status
# would hide it behind a green run.
EXIT_DRIFT = 3

# The deadline expired, as `timeout(1)` reports it. Named rather than spelled inline
# because the JavaScript half exports the same number and the parity suite compares them:
# a CI file branching on the status must not have to ask which half it invoked.
EXIT_TIMEOUT = 124


# THE USAGE TEXT, HAND-WRITTEN, AND IT IS A COPY ON PURPOSE.
#
# This used to be argparse's, which meant the two halves could not be compared: one
# generated `usage: restore-verified [-h] {run,record,verify} ...` from its parser and
# the other hand-wrote prose, so they shared no grammar and nothing could assert they
# described the same tool. A CI file is written from whichever half its author happened
# to read, and the flags in it are the flags that half documents.
#
# So the text is written out here to match the JavaScript half BYTE FOR BYTE, and
# `test_the_usage_text_is_identical` compares the two halves' actual `--help` output
# rather than these constants — a source comparison would pass on two strings that
# render differently, which is how the sibling packages nearly missed the same thing.
#
# The cost is a second copy of forty lines, and the copy is the one that can drift. That
# is exactly the trade `didrun` and `zerocase` both made, for the same reason: a test
# that fails the moment they disagree is cheaper than a generator that would have to
# produce argparse's shape and this one from one source.
#
# Written to STDERR and exiting 0, both of which follow the JavaScript half.
USAGE = """restore-verified — run something that edits files, and prove the tree came back.

  restore-verified run    --paths P... [--timeout S] [--restore] [-v] -- COMMAND...
  restore-verified record --paths P... [--manifest FILE] [--keep-content]
  restore-verified verify --manifest FILE [--restore]

  --paths P...       files or directories to watch
  --pattern GLOB     only watch files matching this glob (repeatable)
  --keep-content     copy the files too, so they can be restored and not merely
                     checked (implied by --restore)
  --timeout S        SIGKILL the command after S SECONDS and verify anyway — the
                     case this tool exists for. SECONDS, not milliseconds: the
                     Python half takes seconds and one README documents both, so a
                     CI file must not depend on which half is installed
  --restore          put drifted files back from the snapshot (still exits 3)

Exit: 0 the tree came back · 3 it did not · 2 this tool could not run ·
      otherwise the command's own status.
"""


def _usage() -> None:
    sys.stderr.write(USAGE)


# THE SIGNALS A PERSON OR A RUNNER ACTUALLY SENDS, forwarded to the command's group.
# Putting the command in its own session is what lets the deadline kill its children
# too, and the cost is that the terminal stops delivering Ctrl-C to it: job control
# signals go to the foreground process group, which after `start_new_session` is this
# process alone. Forwarding is what buys the isolation back.
_FORWARDED = tuple(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP")
    if hasattr(signal, name)
)

_POSIX = os.name == "posix"


def _signal_the_group(proc, pgid, sig) -> None:
    """Signal the command's whole process group, falling back to the command alone.

    THE GROUP ID IS PASSED IN, READ ONCE AT SPAWN, and that is the entire point. Asking
    `os.getpgid(proc.pid)` at signal time fails the moment the command itself has died —
    which is the common case, because the first signal usually kills the shell and
    leaves its children behind. Looking the group up through a corpse meant the
    survivors were never signalled at all, and they are the ones still holding the
    inherited stdout.

    `os.killpg` still raises once the whole group is gone, which is a race that cannot
    be avoided: the command may exit between the deadline firing and the signal landing.
    Falling back to the direct child is what this did before the group existed and is
    never worse.
    """
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def _cmd_run(args) -> int:
    if not args.command:
        sys.stderr.write("restore-verified run: nothing to run after `--`\n")
        return 2

    sentinel = Sentinel.record(
        args.paths, keep_content=args.restore or args.keep_content,
        patterns=args.pattern or None,
    )
    if args.verbose:
        sys.stderr.write(f"[restore-verified] recorded {len(sentinel)} file(s)\n")

    killed = False
    # A NEW SESSION, SO THE DEADLINE REACHES THE WHOLE TREE. `subprocess.run(timeout=)`
    # kills the command and nothing below it, so a harness that spawns workers -- which
    # is what `-- ./harness.sh` and `-- npx jscodeshift` both are -- outlived its own
    # deadline as an orphan, still holding the stdout this process inherited. The tool
    # returned 124 on time and any caller capturing output waited for the work anyway.
    try:
        proc = subprocess.Popen(args.command, start_new_session=_POSIX)
    except FileNotFoundError:
        sys.stderr.write(f"restore-verified: cannot run {args.command[0]!r}\n")
        # The snapshots are already on disk by this point. Returning without discarding
        # them left a copy of the tree in the temp directory for every mistyped command.
        sentinel.discard()
        return 2

    # Read ONCE, while the command is certainly alive. With `start_new_session` it is
    # its own group leader, so this is its pid — but asked for rather than assumed.
    pgid = None
    if _POSIX:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None

    previous = {}
    # THE SECOND SIGNAL ESCALATES, and it has to, because forwarding alone can hang.
    # A background job in a non-interactive shell has SIGINT set to ignore -- that is
    # POSIX, not a quirk -- so `-- sh -c 'worker & wait'` survives a forwarded Ctrl-C
    # and this process would wait for it forever. Waiting is right (the tree still has
    # to be verified, which is the whole premise) but waiting FOREVER is worse than the
    # leak it replaced. So: the first signal is passed on as sent, and a second one of
    # any kind SIGKILLs the group, which nothing can ignore.
    forwarded_once = False

    def _forward(signum, _frame):
        # `killed` is what turns the report on, and a forwarded signal is a kill.
        nonlocal killed, forwarded_once
        killed = True
        if forwarded_once:
            _signal_the_group(proc, pgid, signal.SIGKILL)
            return
        forwarded_once = True
        _signal_the_group(proc, pgid, signum)

    if _POSIX:
        for sig in _FORWARDED:
            try:
                previous[sig] = signal.signal(sig, _forward)
            except (ValueError, OSError):
                # Not the main thread, or the platform will not have it. The command
                # simply keeps whatever disposition it inherited.
                pass
    try:
        code = proc.wait(timeout=args.timeout)
        if code < 0:
            # A CHILD KILLED BY A SIGNAL HAS A NEGATIVE `returncode`, AND A PROCESS
            # CANNOT EXIT WITH ONE. Returning -15 from here made the interpreter exit
            # 241 -- a number that means nothing, differs from the 143 every shell
            # reports for the same event, and differed again from what the JavaScript
            # half returned. 128+n is the convention both halves now follow.
            code = 128 - code
    except subprocess.TimeoutExpired:
        # THE MOTIVATING CASE, and the one where the command had no chance to clean up.
        # The group rather than the process, so the children go with it.
        killed = True
        code = EXIT_TIMEOUT
        _signal_the_group(proc, pgid, signal.SIGKILL)
        proc.wait()
        sys.stderr.write(
            f"[restore-verified] the command exceeded {args.timeout}s and was "
            f"SIGKILLed — no handler, no `finally`, no cleanup ran\n"
        )
    except KeyboardInterrupt:
        # Only reachable where the handler above could not be installed.
        killed = True
        _signal_the_group(proc, pgid, signal.SIGKILL)
        proc.wait()
        code = 130
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        if killed:
            # THE SWEEP, and it is the half that actually fixes the reported defect.
            # Killing the command does not kill what the command started: the first
            # signal takes out the shell and its workers carry on holding the stdout
            # this process inherited, so the caller waits for them however promptly this
            # one returns. Only done when this process did the killing — a command that
            # exits on its own may have left something running deliberately, and that
            # is not this tool's business.
            _signal_the_group(proc, pgid, signal.SIGKILL)

    drift = sentinel.verify()
    if drift and args.restore:
        still = sentinel.restore()
        if still:
            sys.stderr.write(drift_report(still) + "\n")
            sys.stderr.write(
                f"[restore-verified] restored from the snapshot and "
                f"{len(still)} file(s) are STILL wrong\n"
            )
            sentinel.discard()
            return EXIT_DRIFT
        sys.stderr.write(
            f"[restore-verified] {len(drift)} file(s) did not come back; restored "
            f"from the snapshot and verified\n"
        )
        for d in drift:
            sys.stderr.write(f"    {d}\n")
        sentinel.discard()
        # STILL A FAILURE. The tool under test left the tree wrong; that this command
        # could put it back does not make the run trustworthy, it makes it recoverable.
        return EXIT_DRIFT

    sentinel.discard()
    if drift:
        sys.stderr.write(drift_report(drift) + "\n")
        if not args.restore:
            sys.stderr.write(
                "  Re-run with --restore to put them back from the snapshot.\n"
            )
        return EXIT_DRIFT

    if args.verbose or killed:
        sys.stderr.write(f"[restore-verified] {drift_report(drift)}\n")
    return code


def _cmd_record(args) -> int:
    sentinel = Sentinel.record(
        args.paths, keep_content=args.keep_content, patterns=args.pattern or None
    )
    path = sentinel.save(args.manifest)
    sys.stdout.write(path + "\n")
    sys.stderr.write(
        f"[restore-verified] recorded {len(sentinel)} file(s)"
        f"{' with content' if sentinel.keep_content else ' (digests only)'}\n"
    )
    return 0


def _cmd_verify(args) -> int:
    try:
        sentinel = Sentinel.load(args.manifest)
    except (OSError, ValueError) as exc:
        # A MISSING OR UNREADABLE MANIFEST IS THIS TOOL FAILING TO RUN, WHICH IS EXIT 2
        # AND A SENTENCE. A traceback here reads as a crash in the thing being checked.
        sys.stderr.write(f"restore-verified verify: {exc}\n")
        return 2
    drift = sentinel.verify()
    if drift and args.restore:
        try:
            still = sentinel.restore()
        except ValueError as exc:
            # `--restore` against a manifest recorded without `--keep-content`. A
            # documented combination of two documented flags, and it used to end in a
            # traceback rather than in the explanation `Sentinel.restore` already wrote.
            sys.stderr.write(drift_report(drift) + "\n")
            sys.stderr.write(f"restore-verified verify: {exc}\n")
            return 2
        sys.stderr.write(drift_report(drift) + "\n")
        if still:
            sys.stderr.write(f"[restore-verified] {len(still)} still wrong after restore\n")
        return EXIT_DRIFT
    sys.stdout.write(drift_report(drift) + "\n")
    return EXIT_DRIFT if drift else 0


def _positive_seconds(raw: str) -> float:
    """`--timeout` in seconds, refused unless it is a real deadline.

    `--timeout 0` AND `--timeout -1` ARE NOT DEADLINES, and this half used to treat them
    as ones that had already expired: `subprocess.run` SIGKILLed the child before it
    could do anything and this reported 124, which reads as "your command overran" about
    a command that never got to start. The JavaScript half already refused both with 2,
    so the same command line meant "kill it instantly" on PyPI and "you have made a
    mistake" on npm.

    Refusing is the side that was chosen because 2 is what this tool reports when it
    cannot run, and a deadline of zero is a typo in every case anybody has had. It also
    leaves the npm half's behaviour unchanged.

    NaN and infinity are refused here too, for the reason the JavaScript half gives:
    `float("30s")` raises, but a timeout that came through as NaN would silently be no
    deadline at all — the one thing this command exists to impose.
    """
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"needs a positive number of seconds, not {raw!r}"
        )
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            f"needs a positive number of seconds, not {raw!r}"
        )
    return seconds


def build_parser() -> argparse.ArgumentParser:
    # `add_help=False` THROUGHOUT: `-h` is handled before argparse ever sees it, in
    # `main`, so that both halves answer it with the same bytes. Left on, argparse would
    # intercept `-h` first and print its own generated help. The parser still generates
    # the usage line in its ERROR messages, which is a smaller divergence and the next
    # thing to close.
    parser = argparse.ArgumentParser(
        prog="restore-verified",
        description="Run something that edits files in place, and prove the tree came back.",
        add_help=False,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--paths", nargs="+", required=True,
                       help="files or directories to watch")
        p.add_argument("--pattern", action="append",
                       help="only watch files matching this glob (repeatable)")
        p.add_argument("--keep-content", action="store_true",
                       help="copy the files too, so they can be restored and not "
                            "merely checked (implied by --restore)")

    run = sub.add_parser("run", help="record, run a command, then verify", add_help=False)
    common(run)
    run.add_argument("--timeout", type=_positive_seconds, default=None,
                     help="seconds before the command is SIGKILLed — the case this "
                          "tool exists for")
    run.add_argument("--restore", action="store_true",
                     help="put drifted files back from the snapshot (still exits %d)"
                          % EXIT_DRIFT)
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="-- then the command to run")
    run.set_defaults(func=_cmd_run)

    rec = sub.add_parser("record", help="write a manifest and exit", add_help=False)
    common(rec)
    rec.add_argument("--manifest", help="where to write it (default: a temp file)")
    rec.set_defaults(func=_cmd_record)

    ver = sub.add_parser("verify", help="check a tree against a manifest", add_help=False)
    ver.add_argument("--manifest", required=True)
    ver.add_argument("--restore", action="store_true")
    ver.set_defaults(func=_cmd_verify)

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # ONLY THE FLAGS BEFORE `--`, because everything after it belongs to the command
    # being guarded. `run --paths x -- pytest -h` is asking pytest for help, and a tool
    # that answered on its behalf would swallow the run.
    head = argv[: argv.index("--")] if "--" in argv else argv
    if "-h" in head or "--help" in head:
        _usage()
        return 0
    if not head:
        # Bare, or nothing but a command: 2, because this tool was not told what to do.
        _usage()
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
