"""The in-process half: `with guarded(path) as g: ...`."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import tempfile
import threading
from typing import Iterable, Sequence

# The signals worth catching. SIGKILL and SIGSTOP are deliberately absent because they
# cannot be caught — that is what `Sentinel` is for, and pretending otherwise here
# would be the exact "a guard that looks like a guarantee" failure this package is
# about.
DEFAULT_SIGNALS: tuple[int, ...] = tuple(
    s for s in (
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGHUP", None),
        getattr(signal, "SIGQUIT", None),
    ) if s is not None
)


class RestoreFailed(RuntimeError):
    """The file did not come back. The tree is wrong and the run is not trustworthy.

    This is an ERROR and never a warning. Everything downstream of an unrestored file
    scores code nobody wrote, so a message on stderr that a caller can miss is not a
    remedy — it is the failure wearing a nicer coat.
    """


class Interrupted(BaseException):
    """A caught signal, raised so that `finally` blocks run.

    It inherits from BaseException rather than Exception on purpose. A bare
    `except Exception:` inside the guarded body — which is ordinary, defensive code —
    would otherwise swallow the interruption and the process would carry on running
    against a mutated tree after someone asked it to stop.
    """

    def __init__(self, signum: int):
        self.signum = signum
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - non-standard signal number
            name = str(signum)
        super().__init__(f"interrupted by {name}")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Guard:
    """One or more files, snapshotted, restored, and the restore CHECKED.

    Use `guarded(...)` rather than constructing this directly.
    """

    def __init__(
        self,
        paths: Sequence[str],
        *,
        signals: Iterable[int] = DEFAULT_SIGNALS,
        catch_signals: bool = True,
        restore_mtime: bool = True,
    ):
        self.paths = [os.path.abspath(p) for p in paths]
        self.signals = tuple(signals)
        self.catch_signals = catch_signals
        self.restore_mtime = restore_mtime

        # Why signals could not be caught, or None. It is REPORTED rather than
        # silently skipped: a guard that quietly declines half its job is worse than
        # no guard, because the caller believes it is covered.
        self.signal_note: str | None = None

        self._snapshots: dict[str, dict] = {}
        self._scratch: str | None = None
        self._previous: dict[int, object] = {}
        self._pending_signal: int | None = None
        self._entered = False

    # -- snapshot ----------------------------------------------------------------

    def _snapshot(self, path: str) -> dict:
        """What `path` was, kept OUTSIDE the tree.

        A `foo.py.bak` beside the code under test is scratch state in the directory
        being measured: it changes what a file walker collects, what a test discovers
        and what a coverage denominator counts. A clean target is not a clean tree.
        """
        if not os.path.exists(path):
            return {"existed": False}
        with open(path, "rb") as fh:
            data = fh.read()
        st = os.stat(path)
        assert self._scratch is not None
        copy = os.path.join(self._scratch, _digest(path.encode()) + ".snapshot")
        with open(copy, "wb") as fh:
            fh.write(data)
        return {
            "existed": True,
            "digest": _digest(data),
            "size": len(data),
            "copy": copy,
            "mode": st.st_mode,
            "times": (st.st_atime, st.st_mtime),
        }

    # -- signals -----------------------------------------------------------------

    def _install_signals(self) -> None:
        if not self.catch_signals:
            self.signal_note = "signal handling was switched off by the caller"
            return
        if threading.current_thread() is not threading.main_thread():
            # `signal.signal` raises here. Saying so is the whole point: on a worker
            # thread this object degrades to a `try/finally`, and the caller has to
            # know that rather than infer it from a traceback that never happens.
            self.signal_note = (
                "not on the main thread, so no signal could be installed — this guard "
                "covers exceptions only; use Sentinel around the whole process"
            )
            return
        for signum in self.signals:
            try:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._on_signal)
            except (OSError, ValueError, RuntimeError) as exc:  # pragma: no cover
                self._previous.pop(signum, None)
                self.signal_note = f"could not install a handler for {signum}: {exc}"

    def _on_signal(self, signum, _frame):
        # Recording it is what lets `__exit__` re-deliver it after the file is back.
        self._pending_signal = signum
        raise Interrupted(signum)

    def _remove_signals(self) -> None:
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)  # type: ignore[arg-type]
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                pass
        self._previous.clear()

    def _redeliver(self, signum: int) -> None:
        """Die the way the sender asked, now that the tree is back.

        SWALLOWING A SIGNAL TURNS A KILL INTO A HANG. Someone who sends SIGTERM is
        entitled to a process that stops and an exit status that says why; a guard that
        catches the signal, tidies up and then returns normally has converted `kill`
        into "nothing happened", which is a worse bug than the one it fixed. So the
        handler is put back to the default and the signal is re-raised at ourselves.
        """
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except (OSError, ValueError, RuntimeError):  # pragma: no cover
            pass

    # -- restore -----------------------------------------------------------------

    def restore(self) -> None:
        """Put every file back, then READ IT AGAIN and check.

        The second half is the one with no incumbent. `in-place` restores on an
        exception; `fs-transaction` rolls a failed write back; neither re-reads what it
        wrote. A restore that ran is not a restore that worked, and the ways it can run
        and not work — a buffer captured after mutating, a differing encoding, one of
        two files — all leave the restore path looking perfectly healthy.
        """
        failures: list[str] = []
        for path in self.paths:
            snap = self._snapshots.get(path)
            if snap is None:
                continue
            try:
                self._restore_one(path, snap)
            except OSError as exc:
                failures.append(f"{path}: could not be written back ({exc})")
                continue
            problem = self._verify_one(path, snap)
            if problem:
                failures.append(problem)
        if failures:
            raise RestoreFailed(
                "the tree did not come back:\n  " + "\n  ".join(failures)
            )

    def _restore_one(self, path: str, snap: dict) -> None:
        if not snap["existed"]:
            if os.path.exists(path):
                os.remove(path)
            return
        shutil.copyfile(snap["copy"], path)
        os.chmod(path, snap["mode"])
        if self.restore_mtime:
            # A build system, a test cache and a file watcher all key on mtime. A file
            # whose CONTENT came back but whose mtime did not is a file that will be
            # rebuilt, re-linted and re-tested for no reason — and on a big tree that
            # is the difference between a guard nobody notices and one everybody turns
            # off. Pass restore_mtime=False if you want the touch to be visible.
            os.utime(path, snap["times"])

    def _verify_one(self, path: str, snap: dict) -> str | None:
        if not snap["existed"]:
            if os.path.exists(path):
                return f"{path}: was created during the run and could not be removed"
            return None
        if not os.path.exists(path):
            return f"{path}: is missing after the restore"
        with open(path, "rb") as fh:
            data = fh.read()
        if _digest(data) != snap["digest"]:
            return (
                f"{path}: restored to {len(data)} bytes with a different digest "
                f"(expected {snap['digest'][:12]}, got {_digest(data)[:12]})"
            )
        return None

    # -- context manager ---------------------------------------------------------

    def __enter__(self) -> "Guard":
        self._entered = True
        self._scratch = tempfile.mkdtemp(prefix="restore-verified-")
        try:
            for path in self.paths:
                self._snapshots[path] = self._snapshot(path)
        except BaseException:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
            raise
        self._install_signals()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._remove_signals()
        try:
            self.restore()
        finally:
            if self._scratch:
                shutil.rmtree(self._scratch, ignore_errors=True)
                self._scratch = None
        pending = self._pending_signal
        self._pending_signal = None
        if pending is not None:
            self._redeliver(pending)
        return False

    # -- writing -----------------------------------------------------------------

    def write(self, path_or_text, text=None) -> None:
        """Replace a guarded file's contents.

        `g.write(text)` when guarding one path; `g.write(path, text)` otherwise. It
        refuses a path that was never snapshotted, because writing to an unguarded file
        inside a `with` block that looks like it covers everything is the failure this
        package exists to make impossible.
        """
        if text is None:
            if len(self.paths) != 1:
                raise ValueError(
                    "write(text) needs exactly one guarded path; this guard holds "
                    f"{len(self.paths)} — call write(path, text)"
                )
            path, text = self.paths[0], path_or_text
        else:
            path = os.path.abspath(path_or_text)
        if path not in self._snapshots:
            raise ValueError(f"{path} is not guarded by this block")
        data = text.encode() if isinstance(text, str) else text
        with open(path, "wb") as fh:
            fh.write(data)

    def read(self, path=None) -> str:
        """The ORIGINAL text, from the snapshot rather than from disk.

        Reading the file after mutating it and calling the result "the original" is
        one of the three ways a restore runs and does not work. Taking it from the
        snapshot makes that mistake unavailable.
        """
        if path is None:
            if len(self.paths) != 1:
                raise ValueError("read() needs exactly one guarded path")
            path = self.paths[0]
        path = os.path.abspath(path)
        snap = self._snapshots.get(path)
        if snap is None:
            raise ValueError(f"{path} is not guarded by this block")
        if not snap["existed"]:
            raise FileNotFoundError(path)
        with open(snap["copy"], "rb") as fh:
            return fh.read().decode()


def guarded(*paths, **kwargs) -> Guard:
    """Guard one or more paths for the duration of a `with` block.

        with guarded("src/parser.py") as g:
            g.write(g.read().replace("<=", "<"))
            run_the_suite()
        # restored here, and the restore is checked

    Raises `RestoreFailed` if any file did not come back byte for byte.
    """
    flat: list[str] = []
    for p in paths:
        if isinstance(p, (list, tuple, set)):
            flat.extend(p)
        else:
            flat.append(p)
    if not flat:
        raise ValueError("guarded() needs at least one path")
    return Guard(flat, **kwargs)
