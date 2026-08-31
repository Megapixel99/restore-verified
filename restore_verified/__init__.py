"""Temporarily modify a file, and PROVE it came back.

Three failures, and they are not the same failure. A tool that breaks a file on
purpose — a mutation harness, a codemod, a benchmark that swaps a config, a test that
patches a fixture — has to survive all three, and the usual answer covers only the
first:

  1. AN EXCEPTION mid-run. `try/finally` covers this, and this is what every
     "in-place edit" package on either registry implements.

  2. A SIGNAL. `finally` does NOT run on SIGTERM: the default disposition terminates
     the process, no handler runs, no unwinding happens, and the file stays broken.
     `kill` and most CI cancel buttons send SIGTERM.

  3. THE RESTORE ITSELF FAILING. A restore that *ran* is not a restore that *worked*.
     Reading the buffer back after mutating, writing it in a different encoding, or
     restoring one of the two files touched all satisfy 1 and 2 and still leave the
     tree wrong — and every later run scores code nobody wrote. Hashing before and
     comparing after is the only check that separates them.

There is a fourth, and it cannot be fixed from in here: SIGKILL cannot be caught,
blocked or handled. The ordinary way to be SIGKILLed is not an impatient person but a
TIMEOUT — `subprocess.run(..., timeout=...)` kills the child outright, and so does the
kill step of a CI runner that has waited long enough. So a guard satisfying all three
above, invoked under a timeout it then exceeds, leaves the tree exactly as broken as
one carrying none of them. That check belongs to whatever INVOKED the tool, and it is
`restore_verified.Sentinel` — see `sentinel.py`.

WHAT THIS IS NOT FOR. In a clean git checkout, `git diff --quiet` already catches an
unrestored change and `git checkout -- FILE` already fixes it, for free, and you should
use that. This exists for the cases where that is not true, which are not exotic:

  * A DIRTY WORKING TREE — the normal state of a developer's checkout. `git diff` reads
    DIRTY both before and after, so it cannot tell the two apart, and `git checkout --`
    silently discards the uncommitted work it was supposed to be protecting.
  * Untracked or ignored files: generated code, fetched fixtures, local config.
  * No repository at all: a container, an installed package, an unpacked tarball.

A snapshot here is per-file and taken when you start. Git's is repo-wide and taken at
the last commit. Those are the same thing only on a clean tree.
"""

from .guard import (
    Guard,
    RestoreFailed,
    Interrupted,
    guarded,
)
from .sentinel import Sentinel, Drift

__all__ = [
    "guarded",
    "Guard",
    "RestoreFailed",
    "Interrupted",
    "Sentinel",
    "Drift",
]

__version__ = "0.0.1"
