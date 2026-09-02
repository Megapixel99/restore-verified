# `restore-verified`

[![PyPI](https://img.shields.io/pypi/v/restore-verified?label=PyPI&color=3775A9)](https://pypi.org/project/restore-verified/)
[![npm](https://img.shields.io/npm/v/restore-verified?label=npm&color=CB3837)](https://www.npmjs.com/package/restore-verified)
[![ci](https://github.com/Megapixel99/restore-verified/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Megapixel99/restore-verified/actions/workflows/ci.yml)
[![license MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/Megapixel99/restore-verified/blob/master/LICENSE)

Temporarily modify a file, survive the signal, and **prove the tree came back**.

For anything that breaks a file on purpose and puts it back: a mutation harness, a
codemod, a benchmark that swaps a config, a test that patches a fixture.

```sh
pip install restore-verified      # the Python half
npm install restore-verified      # the JavaScript half
```

```python
from restore_verified import guarded

with guarded("src/parser.py") as g:
    g.write(g.read().replace("<=", "<"))
    run_the_suite()
# restored here — and the restore is checked, byte for byte
```

```js
import { guarded } from "restore-verified";

await guarded("src/parser.js", async (g) => {
  g.write(g.read().replace("<=", "<"));
  await runTheSuite();
});
// restored here — and the restore is checked, byte for byte
```

JavaScript has no `with`, so the callback is what brackets the work. It is not sugar:
it is the only shape that puts the restore in a `finally` the caller cannot forget.

```sh
# the case nothing else covers: the tool is SIGKILLed by a timeout mid-edit
restore-verified run --paths src/ --timeout 600 --restore -- ./harness.sh
```

## Read this first: on a clean git checkout, use git

`git diff --quiet` already catches an unrestored change and `git checkout -- FILE`
already fixes it. That is free, it is correct, and it is what you should do. This
package is for the cases where it is not true, and those are not exotic:

| | clean tree | **dirty tree** (a developer's checkout) |
|---|---|---|
| `git diff --quiet` before | clean | DIRTY |
| `git diff --quiet` after a failed restore | DIRTY (**caught** | DIRTY) **indistinguishable** |
| `git checkout -- FILE` | restores | **destroys the uncommitted work** |

Both rows are asserted in `tests/test_guard.py::TheGitControl`, including the one that
says *if git preserved the uncommitted work, use git*.

The reason is structural, not incidental: **a snapshot here is per-file and taken when
you start; git's is repo-wide and taken at the last commit.** Those are the same thing
only on a clean tree. The other cases git cannot serve at all are untracked or ignored
files (generated code, fetched fixtures, local config) and not being in a repository:
a container, an installed package, an unpacked tarball.

## The four failures, and which layer covers each

| | what covers it | what happens without it |
|---|---|---|
| an **exception** mid-run | `try/finally`, and every in-place-edit package on either registry | the file stays broken |
| a **signal** | this package's `Guard` | **`finally` does not run on SIGTERM.** No handler, no unwinding; the file stays broken |
| the **restore itself being wrong** | this package's verification | a restore that *ran* is not a restore that *worked* |
| **SIGKILL / a timeout** | this package's `Sentinel`, one process outward | nothing in-process can help; the file stays broken |

The second row is measured, not asserted. `tests/child.py` runs the same mutation three
ways in a real subprocess, and the test suite kills it for real:

```
test_SIGTERM_leaves_a_try_finally_harness_broken ......... ok
test_SIGTERM_does_not_leave_a_guarded_harness_broken ..... ok
test_the_signal_is_re_delivered_so_a_kill_still_kills .... ok
test_SIGKILL_defeats_the_guard_and_the_sentinel_catches_it ok
```

The first of those is the control. **If `try/finally` ever survives SIGTERM, the premise
of this package is wrong and the test says so in those words.**

### The third row is the name

`in-place` (PyPI) restores the original *if an exception occurs*. `fs-transaction`
(PyPI) rolls back a failed *write*. A second sweep (PyPI's full 881,198-name index for
`restore`, `rollback`, `revert`, `atomic`, `sigterm` and `in-place`, plus web search for
the combination) turned up nothing further. `atomically` and `write-file-atomic` (npm, 16M and
more downloads a week) make a write all-or-nothing. **None of them re-reads what it put
back.** A restore can run perfectly and still be wrong: a buffer captured *after*
mutating, a different encoding on the way out, one of the two files you touched. All
three leave the restore path looking healthy, and every run after them scores code
nobody wrote. Hashing before and comparing after is the only check that separates
*ran* from *worked*.

### The fourth row is the one with no incumbent anywhere

SIGKILL cannot be caught, blocked or handled. The ordinary way to be SIGKILLed is not
an impatient person; it is a **timeout**. `subprocess.run(..., timeout=...)` calls
`Popen.kill()` when the deadline passes, and so does the kill step of a CI runner that
has waited long enough. A harness carrying a *perfect* in-process guard, invoked under a
timeout it exceeds, leaves the tree exactly as broken as one carrying no guard at all:

```
$ restore-verified run --paths /tmp/demo/m.py --timeout 2 -- python3 harness.py
[restore-verified] the command exceeded 2.0s and was SIGKILLed — no handler, no `finally`, no cleanup ran
THE TREE DID NOT COME BACK — 1 file(s):
  changed  /tmp/demo/m.py — 27 bytes -> 8 bytes, digest 64545701caa5 -> 14309db042d4

  Everything measured after this point scores code nobody wrote.
  Re-run with --restore to put them back from the snapshot.
$ echo $?
3
```

That harness had a flawless guard. The check has to live in whatever invoked it.

## Two halves, one manifest

The half that breaks a tree and the half that checks it came back are frequently not the
same process, and frequently not the same language: a Node build script invoking a
Python codemod, a Python CI harness shelling out to `jscodeshift`. So `Sentinel` writes
**one document**, and that is a claim about bytes rather than about intentions:

```sh
# a Python harness records, a Node script verifies — or the other way round
python3 -m restore_verified.cli record --paths src/ --manifest before.json
npx restore-verified verify --manifest before.json
```

`python/tests/test_parity.py` asserts it in both directions: Python writes a manifest and
JavaScript verifies against it, JavaScript writes one and Python **restores** from it,
and both halves produce **byte-identical manifests for the same tree**. Verdicts agreeing
proves the two read the document the same way; bytes agreeing proves they write it the
same way, which is what stops one half quietly adding a field the other ignores. The
version number, the drift exit code and the skipped-directory list are asserted equal
too.

### What is different in JavaScript, and why

Two things, and neither is cosmetic.

**A signal handler cannot unwind an awaited body.** In Python the handler raises, the
`with` block unwinds, and the restore happens on the ordinary path. Node has no such
path: a handler runs as its own event-loop task and cannot inject an exception into
whatever the body is awaiting. So the JavaScript handler performs the restore *itself*,
synchronously, which is why every filesystem call in that half is the `...Sync` one.

**Registering a handler prevents the default termination.** `process.on("SIGTERM", ...)`
means the process no longer dies on SIGTERM, so a guard that catches, restores and
returns has converted `kill` into "nothing happened". The handler therefore removes
itself and re-raises the signal, and that turned out to have an edge of its own: a
registered signal listener is also a handle keeping the event loop alive, so removing the
last one can leave the loop empty and Node exits *before* the re-raised signal lands. A
timer held across the re-raise closes it. Found by a failing test, not by reading.

## Design decisions worth knowing

**A caught signal is re-delivered.** Swallowing SIGTERM turns `kill` into "nothing
happened", which is a worse bug than the one being fixed. The guard restores the file,
puts the handler back to the default, and re-raises the signal at itself, so the
process dies with status `-15`, as the sender intended. Asserted.

**`Interrupted` inherits from `BaseException`.** A bare `except Exception:` inside the
guarded body (ordinary defensive code) would otherwise swallow the interruption and
keep running against a mutated tree after someone asked it to stop.

**Nothing is written beside the code under test.** The snapshot lives in a temp
directory, not in `foo.py.bak`. A scratch file in the directory being measured changes
what a file walker collects, what a test runner discovers, and what a coverage
denominator counts. A clean target is not a clean tree.

**`g.read()` returns the original, from the snapshot.** Reading the file back after
mutating it and calling that "the original" is one of the three ways a restore runs and
does not work; taking it from the snapshot makes the mistake unavailable.

**mtime is restored too** (pass `restore_mtime=False` to opt out). A build system, a
test cache and a file watcher all key on mtime, and a guard that triggers a full rebuild
on every run is a guard people switch off.

> **The tension, found by building `canfail` on top of this.** If your tool
> *compiles or imports* the file it just restored, restoring mtime is wrong: a bytecode
> cache written from the broken source then looks fresh. Worse, `restore_mtime=False` is
> **not sufficient** either: mtime invalidation has one-second granularity, and an
> edit/run/restore cycle in milliseconds defeats it whichever way you set this. Disable
> the cache (`PYTHONDONTWRITEBYTECODE=1`, `make -B`) rather than relying on the clock.

**Off the main thread it says so.** `signal.signal` only works on the main thread, so
there the guard degrades to a `try/finally`, and sets `g.signal_note` to explain it,
rather than covering half the job silently.

**Three drift outcomes, not two.** `changed`, `missing` and `created` are kept apart: a
missing file and a changed one send you to opposite ends of the problem.

**A digest-only manifest refuses to pretend it can restore.** `Sentinel.record` on a
directory keeps digests and no content by default, so `verify()` works and `restore()`
raises rather than silently doing nothing.

## API

```python
from restore_verified import guarded, Sentinel, RestoreFailed

with guarded("a.py", "b.py") as g:       # one file or many
    g.write("a.py", mutated_text)
    ...
# RestoreFailed if anything did not come back byte for byte

sentinel = Sentinel.record(["src/"])      # digests; add keep_content=True to restore
manifest = sentinel.save()                # survives the process that broke the tree
...
for drift in Sentinel.load(manifest).verify():
    print(drift)
```

```js
import { guarded, Sentinel, RestoreFailed } from "restore-verified";

await guarded(["a.js", "b.js"], async (g) => {   // one file or many
  g.write("a.js", mutatedText);
});
// RestoreFailed if anything did not come back byte for byte

const sentinel = Sentinel.record(["src/"]);       // add { keepContent: true } to restore
const manifest = sentinel.save();
for (const drift of Sentinel.load(manifest).verify()) console.log(String(drift));
```

Both halves ship the same CLI, with the same flags and the same exit codes: a CI file
should not have to ask which one is installed:

```sh
restore-verified run --paths src/ [--timeout N] [--restore] -- CMD...
restore-verified record --paths src/ --manifest before.json
restore-verified verify --manifest before.json [--restore]
```

`--timeout` is **seconds** in both halves, and the parity suite asserts it through both
real CLIs rather than trusting either to be right about itself: a deadline that meant
seconds to one half and milliseconds to the other would make a CI file depend on which
binary won the PATH.

The deadline kills the command's **process group**, not just the command. Until 0.1.4 it
killed only the process named after `--`, so `-- ./harness.sh` returned 124 on time while
the workers that harness had started carried on — still holding the stdout this tool
inherited, so anything capturing the output waited for them anyway. The command therefore
runs in its own session, and SIGINT, SIGTERM and SIGHUP are forwarded to it so a Ctrl-C
still arrives; a second one escalates to SIGKILL, because a background job in a shell has
SIGINT set to ignore and forwarding alone would wait forever.

`--paths` is required by both halves. Until 0.1.4 the npm half accepted its absence,
recorded nothing, ran the command and reported that the tree came back.

Exit code **3** means the tree did not come back; its own code, never folded into the
command's status, because a harness that exits 0 having left a file mutated is the exact
failure this exists to report. **124** is the deadline expiring, as `timeout(1)` reports
it; **2** is this tool failing to run; a command killed by a signal reports **128+n**, as
a shell does. Both halves produce the same number for the same event.

`--restore` still exits 3. That this command could put the tree back makes the run
recoverable, not trustworthy.

## Scope, honestly

- **Mature mutation frameworks do not need this.** mutmut 3 copies `source_paths` to a
  `mutants/` directory and mutates the copy; StrykerJS sandboxes likewise. Avoiding
  in-place mutation is a better answer than guarding it, and if you can restructure that
  way, do. This is for the tools that cannot: hand-rolled harnesses, codemods that must
  run against the real tree, anything whose build config points at the original path.
- Zero dependencies, standard library only, Python 3.9+.
- POSIX signals. On Windows there is no SIGTERM in the POSIX sense; the guard covers
  exceptions and `Sentinel` covers the rest.
- It does not lock. Two processes guarding the same file will not see each other.
- `Sentinel.record` on a large tree costs one SHA-256 read per file.

## Tests

```sh
python3 -m unittest discover -s python/tests   # 48, including the cross-half contract
npm test                                       # 33
```

No dependencies in either half. The signal tests spawn a real child and really kill it,
because the question is not "does the handler run" but "what does the file on disk look
like after somebody types `kill`".

Five mutations to the source were applied (with this package's own guard) and all five
were caught by the test that should catch them: removing the signal installation,
removing the verification, dropping the re-delivery, keeping the snapshot beside the
code, and making `verify` always report clean.
