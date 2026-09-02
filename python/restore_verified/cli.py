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
    try:
        completed = subprocess.run(args.command, timeout=args.timeout)
        code = completed.returncode
        if code < 0:
            # A CHILD KILLED BY A SIGNAL HAS A NEGATIVE `returncode`, AND A PROCESS
            # CANNOT EXIT WITH ONE. Returning -15 from here made the interpreter exit
            # 241 -- a number that means nothing, differs from the 143 every shell
            # reports for the same event, and differed again from what the JavaScript
            # half returned. 128+n is the convention both halves now follow.
            code = 128 - code
    except subprocess.TimeoutExpired:
        # `subprocess.run` has already SIGKILLed the child by the time this is caught.
        # This is not an error path bolted on for completeness — it is the motivating
        # case, and it is the one where the child had no chance to clean up.
        killed = True
        code = EXIT_TIMEOUT
        sys.stderr.write(
            f"[restore-verified] the command exceeded {args.timeout}s and was "
            f"SIGKILLed — no handler, no `finally`, no cleanup ran\n"
        )
    except FileNotFoundError:
        sys.stderr.write(f"restore-verified: cannot run {args.command[0]!r}\n")
        # The snapshots are already on disk by this point. Returning without discarding
        # them left a copy of the tree in the temp directory for every mistyped command.
        sentinel.discard()
        return 2
    except KeyboardInterrupt:
        killed = True
        code = 130

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
    parser = argparse.ArgumentParser(
        prog="restore-verified",
        description="Run something that edits files in place, and prove the tree came back.",
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

    run = sub.add_parser("run", help="record, run a command, then verify")
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

    rec = sub.add_parser("record", help="write a manifest and exit")
    common(rec)
    rec.add_argument("--manifest", help="where to write it (default: a temp file)")
    rec.set_defaults(func=_cmd_record)

    ver = sub.add_parser("verify", help="check a tree against a manifest")
    ver.add_argument("--manifest", required=True)
    ver.add_argument("--restore", action="store_true")
    ver.set_defaults(func=_cmd_verify)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
