/**
 * The in-process half, in JavaScript.
 *
 * The three failures are the same ones the Python half exists for, and the middle one
 * was verified in Node rather than assumed: a `try`/`finally` around a file edit, sent
 * SIGTERM, leaves the file broken. Node's default disposition terminates the process
 * without unwinding, exactly as Python's does.
 *
 * TWO THINGS ARE DIFFERENT HERE, AND BOTH MATTER.
 *
 * 1. A SIGNAL HANDLER CANNOT UNWIND AN AWAITED BODY. In Python the handler raises, the
 *    `with` block unwinds, and the restore happens on the ordinary path. Node has no
 *    such path: a handler runs as its own event-loop task and cannot inject an
 *    exception into whatever the body is awaiting. So the handler performs the restore
 *    ITSELF, synchronously, and the normal path is guarded against restoring twice.
 *    That is why every filesystem call in here is the `...Sync` one — a handler that
 *    awaited anything would be racing the process's own death.
 *
 * 2. REGISTERING A HANDLER PREVENTS THE DEFAULT TERMINATION. In Node, `process.on
 *    ("SIGTERM", ...)` means the process no longer dies on SIGTERM. A guard that
 *    catches, restores and returns has converted `kill` into "nothing happened", which
 *    is a worse bug than the one it fixed — so the handler is removed and the signal is
 *    re-raised at ourselves once the file is back.
 *
 * `process.on("exit")` is also hooked, because `process.exit()` inside the body skips
 * every `finally` in the program. That listener must be synchronous, which it is.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// SIGKILL and SIGSTOP are deliberately absent because they cannot be caught — that is
// what `Sentinel` is for, and pretending otherwise here would be the exact
// "a guard that looks like a guarantee" failure this package is about.
export const DEFAULT_SIGNALS = ["SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"];

/**
 * The file did not come back. The tree is wrong and the run is not trustworthy.
 *
 * This is an ERROR and never a warning. Everything downstream of an unrestored file
 * scores code nobody wrote, so a message on stderr that a caller can miss is not a
 * remedy — it is the failure wearing a nicer coat.
 */
export class RestoreFailed extends Error {
  constructor(message) {
    super(message);
    this.name = "RestoreFailed";
  }
}

function digest(data) {
  return crypto.createHash("sha256").update(data).digest("hex");
}

export class Guard {
  constructor(paths, options = {}) {
    const {
      signals = DEFAULT_SIGNALS,
      catchSignals = true,
      restoreMtime = true,
    } = options;

    this.paths = paths.map((p) => path.resolve(p));
    this.signals = signals;
    this.catchSignals = catchSignals;
    this.restoreMtime = restoreMtime;

    // Why signals could not be caught, or null. REPORTED rather than silently skipped:
    // a guard that quietly declines half its job is worse than no guard, because the
    // caller believes they are covered.
    this.signalNote = null;

    this._snapshots = new Map();
    this._scratch = null;
    this._handlers = new Map();
    this._exitHandler = null;
    this._done = false;
  }

  // -- snapshot --------------------------------------------------------------

  /**
   * What `p` was, kept OUTSIDE the tree.
   *
   * A `foo.js.bak` beside the code under test is scratch state in the directory being
   * measured: it changes what a file walker collects, what a test runner discovers and
   * what a coverage denominator counts. A clean target is not a clean tree.
   */
  _snapshot(p) {
    if (!fs.existsSync(p)) return { existed: false };
    const data = fs.readFileSync(p);
    const stat = fs.statSync(p);
    const copy = path.join(this._scratch, digest(Buffer.from(p)) + ".snapshot");
    fs.writeFileSync(copy, data);
    return {
      existed: true,
      digest: digest(data),
      size: data.length,
      copy,
      mode: stat.mode,
      times: [stat.atime, stat.mtime],
    };
  }

  enter() {
    this._scratch = fs.mkdtempSync(path.join(os.tmpdir(), "restore-verified-"));
    try {
      for (const p of this.paths) this._snapshots.set(p, this._snapshot(p));
    } catch (err) {
      fs.rmSync(this._scratch, { recursive: true, force: true });
      this._scratch = null;
      throw err;
    }
    this._installSignals();
    return this;
  }

  // -- signals ---------------------------------------------------------------

  _installSignals() {
    if (!this.catchSignals) {
      this.signalNote = "signal handling was switched off by the caller";
      return;
    }
    for (const sig of this.signals) {
      const handler = () => this._onSignal(sig);
      try {
        process.on(sig, handler);
        this._handlers.set(sig, handler);
      } catch {
        // Windows has no SIGHUP/SIGQUIT. A signal that cannot be listened for is not
        // an error; it is a smaller guard, and the note says so.
        this.signalNote = `could not listen for ${sig} on this platform`;
      }
    }
    this._exitHandler = () => {
      // `process.exit()` in the body skips every `finally` in the program. This is the
      // last synchronous chance to put the file back.
      try {
        this._restoreQuietly();
      } catch {
        /* an exit handler that throws replaces the exit code with a crash */
      }
    };
    process.on("exit", this._exitHandler);
  }

  _removeSignals() {
    for (const [sig, handler] of this._handlers) process.removeListener(sig, handler);
    this._handlers.clear();
    if (this._exitHandler) {
      process.removeListener("exit", this._exitHandler);
      this._exitHandler = null;
    }
  }

  /**
   * Restore now, then die the way the sender asked.
   *
   * The handler does the work itself because it cannot hand control back to the body.
   * Then the listener is removed and the signal is re-sent, so the process exits with
   * the status the sender intended rather than silently carrying on.
   */
  _onSignal(sig) {
    try {
      this._restoreQuietly();
    } finally {
      this._removeSignals();
      this._cleanScratch();
      // A REGISTERED SIGNAL LISTENER IS ALSO A HANDLE THAT KEEPS THE EVENT LOOP ALIVE.
      // Removing the last one can leave the loop empty, and Node then drains and exits
      // — with code 13 if a top-level await is pending — BEFORE the signal re-raised
      // on the next line is ever delivered. The process would exit normally after
      // being sent SIGTERM, which is precisely the "kill became a no-op" bug this
      // re-raise exists to prevent. A timer held across the kill keeps the loop alive
      // for the microseconds it takes; if the signal somehow never arrives, it expires
      // and the process exits on its own rather than hanging.
      const keepalive = setTimeout(() => {}, 1000);
      try {
        process.kill(process.pid, sig);
      } catch {
        clearTimeout(keepalive);
      }
    }
  }

  _restoreQuietly() {
    if (this._done) return;
    try {
      this.restore();
    } catch (err) {
      // A signal path cannot throw usefully — nothing is going to catch it — so the
      // failure is written where a person will actually see it.
      process.stderr.write(`[restore-verified] ${err.message}\n`);
    }
  }

  _cleanScratch() {
    if (this._scratch) {
      fs.rmSync(this._scratch, { recursive: true, force: true });
      this._scratch = null;
    }
  }

  // -- restore ---------------------------------------------------------------

  /**
   * Put every file back, then READ IT AGAIN and check.
   *
   * The second half is the one with no incumbent. `atomically` and `write-file-atomic`
   * make a WRITE all-or-nothing; `signal-exit` and `exit-hook` give you the hook, not
   * the contract. None of them re-reads what it wrote. A restore that ran is not a
   * restore that worked, and the ways it can run and not work — a buffer captured after
   * mutating, a differing encoding, one of two files — all leave the restore path
   * looking perfectly healthy.
   */
  restore() {
    if (this._done) return;
    this._done = true;
    const failures = [];
    for (const p of this.paths) {
      const snap = this._snapshots.get(p);
      if (!snap) continue;
      try {
        this._restoreOne(p, snap);
      } catch (err) {
        failures.push(`${p}: could not be written back (${err.message})`);
        continue;
      }
      const problem = this._verifyOne(p, snap);
      if (problem) failures.push(problem);
    }
    if (failures.length) {
      throw new RestoreFailed(
        "the tree did not come back:\n  " + failures.join("\n  ")
      );
    }
  }

  _restoreOne(p, snap) {
    if (!snap.existed) {
      if (fs.existsSync(p)) fs.rmSync(p);
      return;
    }
    fs.copyFileSync(snap.copy, p);
    fs.chmodSync(p, snap.mode);
    if (this.restoreMtime) {
      // A build system, a test cache and a file watcher all key on mtime. A file whose
      // CONTENT came back but whose mtime did not is a file that will be rebuilt,
      // re-linted and re-tested for no reason.
      //
      // BUT: if your tool COMPILES or IMPORTS what it just restored, this is wrong —
      // a cache written from the broken source then looks fresh. And turning it off is
      // not sufficient either, because mtime granularity is one second and an
      // edit/run/restore cycle is milliseconds. Disable the cache rather than trusting
      // the clock.
      fs.utimesSync(p, snap.times[0], snap.times[1]);
    }
  }

  _verifyOne(p, snap) {
    if (!snap.existed) {
      return fs.existsSync(p)
        ? `${p}: was created during the run and could not be removed`
        : null;
    }
    if (!fs.existsSync(p)) return `${p}: is missing after the restore`;
    const data = fs.readFileSync(p);
    const got = digest(data);
    if (got !== snap.digest) {
      return (
        `${p}: restored to ${data.length} bytes with a different digest ` +
        `(expected ${snap.digest.slice(0, 12)}, got ${got.slice(0, 12)})`
      );
    }
    return null;
  }

  exit() {
    this._removeSignals();
    try {
      this.restore();
    } finally {
      this._cleanScratch();
    }
  }

  // -- writing ---------------------------------------------------------------

  /**
   * Replace a guarded file's contents.
   *
   * `g.write(text)` when guarding one path; `g.write(p, text)` otherwise. It refuses a
   * path that was never snapshotted, because writing to an unguarded file inside a
   * block that looks like it covers everything is the failure this package exists to
   * make impossible.
   */
  write(pathOrText, text) {
    let p;
    if (text === undefined) {
      if (this.paths.length !== 1) {
        throw new Error(
          `write(text) needs exactly one guarded path; this guard holds ` +
            `${this.paths.length} — call write(path, text)`
        );
      }
      p = this.paths[0];
      text = pathOrText;
    } else {
      p = path.resolve(pathOrText);
    }
    if (!this._snapshots.has(p)) throw new Error(`${p} is not guarded by this block`);
    fs.writeFileSync(p, text);
  }

  /**
   * The ORIGINAL text, from the snapshot rather than from disk.
   *
   * Reading the file after mutating it and calling the result "the original" is one of
   * the three ways a restore runs and does not work. Taking it from the snapshot makes
   * that mistake unavailable.
   */
  read(p) {
    if (p === undefined) {
      if (this.paths.length !== 1) throw new Error("read() needs exactly one guarded path");
      p = this.paths[0];
    }
    p = path.resolve(p);
    const snap = this._snapshots.get(p);
    if (!snap) throw new Error(`${p} is not guarded by this block`);
    if (!snap.existed) throw new Error(`${p} does not exist`);
    return fs.readFileSync(snap.copy, "utf8");
  }
}

/**
 * Guard one or more paths for the duration of `body`.
 *
 *     await guarded("src/parser.js", async (g) => {
 *       g.write(g.read().replace("<=", "<"));
 *       await runTheSuite();
 *     });
 *     // restored here, and the restore is checked
 *
 * JavaScript has no `with`, so the callback is what brackets the work — and it is not
 * optional sugar: it is the only shape that puts the restore in a `finally` the caller
 * cannot forget to write.
 *
 * Throws `RestoreFailed` if any file did not come back byte for byte.
 */
export async function guarded(paths, body, options = {}) {
  const list = (Array.isArray(paths) ? paths : [paths]).flat();
  if (list.length === 0) throw new Error("guarded() needs at least one path");
  const guard = new Guard(list, options).enter();
  try {
    return await body(guard);
  } finally {
    guard.exit();
  }
}
