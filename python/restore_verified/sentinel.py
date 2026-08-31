"""The out-of-process half: the check that belongs one level up.

`Guard` covers exceptions and every signal that can be caught. It cannot cover SIGKILL,
because nothing can: no handler runs, no `finally` runs, no unwinding happens. And the
ordinary way to be SIGKILLed is not an impatient person — it is a TIMEOUT.
`subprocess.run(..., timeout=...)` calls `Popen.kill()` when the deadline passes, which
is SIGKILL on POSIX, and so does the kill step of a CI runner that has waited long
enough.

So a tool carrying a perfect in-process guard, invoked under a timeout it then exceeds,
leaves the tree exactly as broken as one carrying no guard at all. The remedy is not in
the tool and cannot be: it belongs to WHATEVER INVOKED IT, which has to check that the
tree came back rather than trust that the tool was given the chance to put it back.

That is this file. It is the same argument `Guard.restore` makes about verification,
one process outward: a guard that ran is not a guard that finished.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Iterable, Sequence

MANIFEST_VERSION = 1

# Directories never worth snapshotting. They are large, they are rebuilt rather than
# authored, and a tool that churns them is not the tool this is watching for.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".claude",
}


@dataclass(frozen=True)
class Drift:
    """One file that is not what it was. `kind` is what happened to it."""

    path: str
    kind: str  # "changed" | "missing" | "created"
    detail: str

    def __str__(self) -> str:
        return f"{self.kind:8} {self.path} — {self.detail}"


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


class Sentinel:
    """A record of what a tree looked like, kept OUTSIDE that tree.

        sentinel = Sentinel.record(["src/"])
        run_the_harness_under_a_timeout()
        drift = sentinel.verify()
        if drift:
            sentinel.restore()      # only when `keep_content=True`

    `record` walks the paths and digests every file. With `keep_content=True` (the
    default for an explicit list of files, off for a whole tree) it also copies them,
    which is what makes `restore` possible as well as `verify`.
    """

    def __init__(self, entries: dict, root: str, scratch: str | None, keep_content: bool):
        self.entries = entries
        self.root = root
        self.scratch = scratch
        self.keep_content = keep_content

    # -- recording ---------------------------------------------------------------

    @classmethod
    def record(
        cls,
        paths: Sequence[str],
        *,
        keep_content: bool | None = None,
        patterns: Iterable[str] | None = None,
        root: str | None = None,
    ) -> "Sentinel":
        """Digest everything under `paths`.

        `keep_content` defaults to True when every path is a file and False when any is
        a directory — copying a source tree to watch it is usually the wrong trade, and
        making that choice silently either way would surprise somebody. Pass it
        explicitly when it matters.
        """
        files: list[str] = []
        saw_dir = False
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.isdir(ap):
                saw_dir = True
                for dirpath, dirnames, filenames in os.walk(ap):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in filenames:
                        full = os.path.join(dirpath, name)
                        if patterns and not any(
                            fnmatch.fnmatch(name, pat) for pat in patterns
                        ):
                            continue
                        files.append(full)
            else:
                files.append(ap)

        if keep_content is None:
            keep_content = not saw_dir
        scratch = tempfile.mkdtemp(prefix="restore-verified-sentinel-") if keep_content else None
        base = os.path.abspath(root) if root else os.getcwd()

        entries: dict[str, dict] = {}
        for full in sorted(set(files)):
            if not os.path.exists(full):
                entries[full] = {"existed": False}
                continue
            digest, size = _digest_file(full)
            entry = {"existed": True, "digest": digest, "size": size,
                     "mode": os.stat(full).st_mode}
            if keep_content:
                assert scratch is not None
                copy = os.path.join(scratch, _digest_bytes(full.encode()) + ".snapshot")
                shutil.copyfile(full, copy)
                entry["copy"] = copy
            entries[full] = entry
        return cls(entries, base, scratch, keep_content)

    # -- persistence -------------------------------------------------------------

    def save(self, path: str | None = None) -> str:
        """Write the manifest so ANOTHER process can do the verifying.

        The whole point of this half is that the process which broke the tree may be
        gone — killed, timed out, or crashed — by the time anybody asks. A record that
        only lived in that process's memory would answer no question at all.
        """
        if path is None:
            fd, path = tempfile.mkstemp(prefix="restore-verified-", suffix=".json")
            os.close(fd)
        payload = {
            "manifest": MANIFEST_VERSION,
            "root": self.root,
            "keep_content": self.keep_content,
            "scratch": self.scratch,
            "entries": self.entries,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        return path

    @classmethod
    def load(cls, path: str) -> "Sentinel":
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("manifest") != MANIFEST_VERSION:
            raise ValueError(
                f"{path} was written by manifest version {payload.get('manifest')!r} "
                f"and this is version {MANIFEST_VERSION} — the two do not mean the "
                f"same thing by `entries`"
            )
        return cls(payload["entries"], payload["root"], payload.get("scratch"),
                   payload.get("keep_content", False))

    # -- the question ------------------------------------------------------------

    def verify(self) -> list[Drift]:
        """Did everything come back? A list of what did not, empty when it all did.

        THREE OUTCOMES, NOT TWO, and they are kept apart for the reason `assay` keeps
        its verdicts apart: a file that is *missing* and a file that is *different*
        send you to opposite ends of the problem, and collapsing them into "not clean"
        throws away the half of the message that says what to do.
        """
        drift: list[Drift] = []
        for path, entry in sorted(self.entries.items()):
            exists = os.path.exists(path)
            if not entry.get("existed"):
                if exists:
                    drift.append(Drift(path, "created",
                                       "did not exist before the run and does now"))
                continue
            if not exists:
                drift.append(Drift(path, "missing", "existed before the run and is gone"))
                continue
            digest, size = _digest_file(path)
            if digest != entry["digest"]:
                drift.append(Drift(
                    path, "changed",
                    f"{entry['size']} bytes -> {size} bytes, digest "
                    f"{entry['digest'][:12]} -> {digest[:12]}",
                ))
        return drift

    def restore(self) -> list[Drift]:
        """Put back everything that drifted, and verify again. Returns what is STILL wrong.

        It refuses rather than half-works when the manifest holds no content: a
        digest-only record can tell you the tree is wrong and cannot tell you what it
        should have been, and pretending otherwise is how a "restore" that silently
        did nothing gets reported as a fix.
        """
        if not self.keep_content:
            raise ValueError(
                "this manifest holds digests but no content, so it can verify and "
                "cannot restore — record with keep_content=True to be able to put "
                "files back"
            )
        for path, entry in self.entries.items():
            if not entry.get("existed"):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            copy = entry.get("copy")
            if not copy or not os.path.exists(copy):
                continue
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copyfile(copy, path)
                os.chmod(path, entry["mode"])
            except OSError:
                continue
        return self.verify()

    # -- housekeeping ------------------------------------------------------------

    def discard(self) -> None:
        """Throw the snapshots away. Verification is impossible afterwards."""
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {"files": len(self.entries), "keep_content": self.keep_content,
                "root": self.root}


def drift_report(drift: Sequence[Drift]) -> str:
    """What to print. Says the denominator even when it is zero."""
    if not drift:
        return "the tree came back: every file matches the digest recorded before the run"
    lines = [f"THE TREE DID NOT COME BACK — {len(drift)} file(s):"]
    for d in drift:
        lines.append("  " + str(d))
    lines.append("")
    lines.append("  Everything measured after this point scores code nobody wrote.")
    return "\n".join(lines)


__all__ = ["Sentinel", "Drift", "drift_report", "MANIFEST_VERSION"]
