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


class _Refused(Exception):
    """A command line this tool will not act on, carrying the sentence to print.

    `restore-verified: ` is prepended once, in `main`, exactly as the JavaScript half
    prepends it in the `catch` around its own parse.
    """


def _to_number(raw: str) -> float:
    """JavaScript's `Number()`, closely enough that both halves refuse the same strings.

    `float()` alone is not it. `Number("")` is 0 and `float("")` raises; `Number("0x10")`
    is 16 and `float("0x10")` raises. Neither is a string anybody types at a deadline,
    but the two halves refusing DIFFERENT sets of nonsense is the drift this file exists
    to stop, and it costs four lines to not have.
    """
    text = raw.strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return float(int(text, 0))
    except ValueError:
        return float("nan")


class _Options:
    """What the command line said, with the same defaults as the other half's object."""

    def __init__(self) -> None:
        self.paths = []
        self.pattern = []
        self.manifest = None
        self.timeout = None
        self.keep_content = False
        self.restore = False
        self.verbose = False
        self.help = False
        self.command = []


def _parse(argv):
    """Read the command line, refusing in the same words as the JavaScript half.

    HAND-ROLLED, AND THAT IS THE POINT. This was `argparse`, which meant every refusal
    was argparse's sentence: `error: the following arguments are required: --paths`
    against the other half's `restore-verified run: --paths is required`. Two tools
    under one name, refusing the same command line differently, and a person reading one
    registry's output cannot search for the other's message.

    The structure follows the other half's `parse()` clause for clause so that the two
    can be read side by side, which is the only maintenance story a second copy has.
    """
    sep = argv.index("--") if "--" in argv else -1
    flags = argv if sep == -1 else argv[:sep]
    opts = _Options()
    opts.command = [] if sep == -1 else argv[sep + 1 :]

    i = 0
    while i < len(flags):
        f = flags[i]

        def value():
            nonlocal i
            i += 1
            if i >= len(flags):
                raise _Refused(f"{f} needs a value")
            return flags[i]

        if f == "--paths":
            # `-` AND NOT JUST `--`. Stopping only at `--` swallowed the short flags: in
            # `run --paths src -v -- cmd` the `-v` became a path, so the guard watched a
            # file that does not exist and verbose silently never turned on.
            while i + 1 < len(flags) and not flags[i + 1].startswith("-"):
                i += 1
                opts.paths.append(flags[i])
        elif f == "--pattern":
            opts.pattern.append(value())
        elif f == "--manifest":
            opts.manifest = value()
        elif f == "--timeout":
            raw = value()
            seconds = _to_number(raw)
            # `--timeout 0` and `--timeout -1` are not deadlines, and NaN is silently no
            # deadline at all -- the one thing this command exists to impose.
            if not math.isfinite(seconds) or seconds <= 0:
                raise _Refused(
                    f"--timeout needs a positive number of seconds, not {raw}"
                )
            opts.timeout = seconds
        elif f == "--keep-content":
            opts.keep_content = True
        elif f == "--restore":
            opts.restore = True
        elif f in ("-v", "--verbose"):
            opts.verbose = True
        elif f in ("-h", "--help"):
            opts.help = True
        elif i > 0 or f not in ("run", "record", "verify"):
            raise _Refused(f"unknown option {f}")
        i += 1

    return (flags[0] if flags else None), opts


def _cmd_run(args) -> int:
    # BEFORE the empty-command check, and in this order in both halves: a caller who
    # omits both is told about `--paths` rather than about `--`.
    if not args.paths:
        sys.stderr.write("restore-verified run: --paths is required\n")
        return 2
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
    if not args.paths:
        sys.stderr.write("restore-verified record: --paths is required\n")
        return 2
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
    if not args.manifest:
        sys.stderr.write("restore-verified verify: --manifest is required\n")
        return 2
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cmd, opts = _parse(argv)
    except _Refused as exc:
        sys.stderr.write(f"restore-verified: {exc}\n")
        return 2

    # `--help` exits 0 and a bare invocation exits 2, both writing the usage to stderr,
    # and both halves derive that from the same two facts in the same order.
    if opts.help or not cmd:
        _usage()
        return 0 if cmd else 2

    # EVERY COMMAND'S RAISE IS THIS TOOL FAILING TO RUN, WHICH IS EXIT 2 AND A SENTENCE.
    # A traceback here is the status a CI file reads as "the command under test failed"
    # rather than "this tool could not run", and both are documented paths.
    try:
        if cmd == "run":
            return _cmd_run(opts)
        if cmd == "record":
            return _cmd_record(opts)
        if cmd == "verify":
            return _cmd_verify(opts)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"restore-verified: {exc}\n")
        return 2

    _usage()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
