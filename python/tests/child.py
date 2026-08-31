"""A child process that breaks a file and then waits to be killed.

It exists so the signal tests kill a REAL process rather than calling a handler
directly. A test that invokes the handler by hand proves the handler runs; it says
nothing about whether the signal reaches it, whether `finally` unwinds, or what the
file on disk looks like afterwards — which are the three things in question.

    python child.py guarded  PATH   # the package's guard
    python child.py naive    PATH   # try/finally, which is what everyone writes
    python child.py bare     PATH   # no protection at all, the floor

It prints READY on stdout and flushes before sleeping, so the parent kills it at a
known point rather than racing the interpreter's start-up.
"""

import sys
import time

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])   # python/

from restore_verified import guarded  # noqa: E402

MUTATED = "MUTATED\n"


def run_guarded(path: str) -> None:
    with guarded(path) as g:
        g.write(MUTATED)
        print("READY", flush=True)
        time.sleep(30)


def run_naive(path: str) -> None:
    """What a careful person writes, and what `assay runners` calls restore-in-finally.

    It survives an exception and it does NOT survive SIGTERM, because the default
    disposition for SIGTERM terminates the process without unwinding.
    """
    with open(path) as fh:
        original = fh.read()
    try:
        with open(path, "w") as fh:
            fh.write(MUTATED)
        print("READY", flush=True)
        time.sleep(30)
    finally:
        with open(path, "w") as fh:
            fh.write(original)


def run_bare(path: str) -> None:
    with open(path, "w") as fh:
        fh.write(MUTATED)
    print("READY", flush=True)
    time.sleep(30)


def main() -> int:
    mode, path = sys.argv[1], sys.argv[2]
    {"guarded": run_guarded, "naive": run_naive, "bare": run_bare}[mode](path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
