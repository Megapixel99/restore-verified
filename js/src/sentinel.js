/**
 * The out-of-process half, in JavaScript — and the manifest is ONE DOCUMENT.
 *
 * `Guard` covers exceptions and every signal that can be caught. It cannot cover
 * SIGKILL, because nothing can. And the ordinary way to be SIGKILLed is not an
 * impatient person — it is a TIMEOUT. Node's `child_process` kills a child that
 * overruns its deadline, and `subprocess.run(..., timeout=...)` in Python calls
 * `Popen.kill()`, which is SIGKILL on POSIX.
 *
 * So a tool carrying a perfect in-process guard, invoked under a timeout it exceeds,
 * leaves the tree exactly as broken as one carrying none. The remedy belongs to
 * WHATEVER INVOKED IT.
 *
 * THE MANIFEST IS DELIBERATELY THE SAME JSON THE PYTHON HALF WRITES, key for key, and
 * that is the point of shipping two halves rather than two packages. The process that
 * breaks a tree and the process that checks it came back are often not the same
 * process, and are frequently not the same language: a Node build script can verify
 * what a Python harness did to the tree, and the other way round. `js/test/parity.test.js`
 * asserts it by round-tripping a real manifest through both.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const MANIFEST_VERSION = 1;

// Directories never worth snapshotting. They are large, they are rebuilt rather than
// authored, and a tool that churns them is not the tool this is watching for. Kept
// identical to the Python half's `SKIP_DIRS`, because two halves that disagree about
// what they walked would disagree about whether a tree came back.
export const SKIP_DIRS = new Set([
  ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
  ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
  ".claude",
]);

/** One file that is not what it was. `kind` is what happened to it. */
export class Drift {
  constructor(filePath, kind, detail) {
    this.path = filePath;
    this.kind = kind; // "changed" | "missing" | "created"
    this.detail = detail;
  }

  toString() {
    return `${this.kind.padEnd(8)} ${this.path} — ${this.detail}`;
  }
}

function digestBytes(data) {
  return crypto.createHash("sha256").update(data).digest("hex");
}

function digestFile(p) {
  const data = fs.readFileSync(p);
  return [digestBytes(data), data.length];
}

function matchesAny(name, patterns) {
  // A tiny glob, matching Python's `fnmatch` closely enough for the `*.py`-shaped
  // patterns this takes. Anything richer would be a second implementation of a thing
  // the two halves have to agree about.
  return patterns.some((pattern) => {
    const rx = new RegExp(
      "^" +
        pattern
          .replace(/[.+^${}()|[\]\\]/g, "\\$&")
          .replace(/\*/g, ".*")
          .replace(/\?/g, ".") +
        "$"
    );
    return rx.test(name);
  });
}

export class Sentinel {
  constructor(entries, root, scratch, keepContent) {
    this.entries = entries;
    this.root = root;
    this.scratch = scratch;
    this.keepContent = keepContent;
  }

  /**
   * Digest everything under `paths`.
   *
   * `keepContent` defaults to true when every path is a file and false when any is a
   * directory — copying a source tree to watch it is usually the wrong trade, and
   * making that choice silently either way would surprise somebody.
   */
  static record(paths, options = {}) {
    const { keepContent = null, patterns = null, root = null } = options;
    const files = [];
    let sawDir = false;

    // A SYMLINK IS NOT A `Dirent` DIRECTORY, AND THAT CRASHED THE WALK. `isDirectory()`
    // on a `Dirent` describes the link itself, so a symlink pointing at a directory fell
    // through to the file branch and `readFileSync` threw EISDIR — a stack trace out of
    // `record --paths .` for a tree that merely contains a symlink. Python's `os.walk`
    // puts a symlinked directory in `dirnames` and, not following links, never digests
    // it; resolving the link here and skipping directories is that same behaviour, which
    // is what keeps the two halves recording the same set of files.
    const isDirectory = (entry, full) => {
      if (entry.isDirectory()) return true;
      if (!entry.isSymbolicLink()) return false;
      try {
        return fs.statSync(full).isDirectory();
      } catch {
        return false; // a broken link is not a directory; it is recorded as missing
      }
    };

    const walk = (dir) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (isDirectory(entry, full)) {
          if (SKIP_DIRS.has(entry.name) || entry.isSymbolicLink()) continue;
          walk(full);
        } else if (!patterns || matchesAny(entry.name, patterns)) {
          files.push(full);
        }
      }
    };

    for (const p of paths) {
      const abs = path.resolve(p);
      if (fs.existsSync(abs) && fs.statSync(abs).isDirectory()) {
        sawDir = true;
        walk(abs);
      } else {
        files.push(abs);
      }
    }

    const keep = keepContent === null ? !sawDir : keepContent;
    const scratch = keep
      ? fs.mkdtempSync(path.join(os.tmpdir(), "restore-verified-sentinel-"))
      : null;

    const entries = {};
    for (const full of [...new Set(files)].sort()) {
      if (!fs.existsSync(full)) {
        entries[full] = { existed: false };
        continue;
      }
      const [d, size] = digestFile(full);
      const entry = { existed: true, digest: d, size, mode: fs.statSync(full).mode };
      if (keep) {
        const copy = path.join(scratch, digestBytes(Buffer.from(full)) + ".snapshot");
        fs.copyFileSync(full, copy);
        entry.copy = copy;
      }
      entries[full] = entry;
    }
    return new Sentinel(entries, root ? path.resolve(root) : process.cwd(), scratch, keep);
  }

  /**
   * Write the manifest so ANOTHER process can do the verifying.
   *
   * The whole point of this half is that the process which broke the tree may be gone —
   * killed, timed out, or crashed — by the time anybody asks. A record that only lived
   * in that process's memory would answer no question at all.
   *
   * `sort_keys` and two-space indent match the Python half byte for byte, so a manifest
   * is diffable across the two.
   */
  save(target = null) {
    if (target === null) {
      target = path.join(
        fs.mkdtempSync(path.join(os.tmpdir(), "restore-verified-")),
        "manifest.json"
      );
    }
    const payload = {
      manifest: MANIFEST_VERSION,
      root: this.root,
      keep_content: this.keepContent,
      scratch: this.scratch,
      entries: this.entries,
    };
    fs.writeFileSync(target, JSON.stringify(sortDeep(payload), null, 2));
    return target;
  }

  static load(target) {
    const payload = JSON.parse(fs.readFileSync(target, "utf8"));
    if (payload.manifest !== MANIFEST_VERSION) {
      throw new Error(
        `${target} was written by manifest version ${JSON.stringify(payload.manifest)} ` +
          `and this is version ${MANIFEST_VERSION} — the two do not mean the same thing ` +
          `by \`entries\``
      );
    }
    return new Sentinel(
      payload.entries,
      payload.root,
      payload.scratch ?? null,
      payload.keep_content ?? false
    );
  }

  /**
   * Did everything come back? A list of what did not, empty when it all did.
   *
   * THREE OUTCOMES, NOT TWO. A file that is *missing* and a file that is *different*
   * send you to opposite ends of the problem, and collapsing them into "not clean"
   * throws away the half of the message that says what to do.
   */
  verify() {
    const drift = [];
    for (const p of Object.keys(this.entries).sort()) {
      const entry = this.entries[p];
      const exists = fs.existsSync(p);
      if (!entry.existed) {
        if (exists) {
          drift.push(new Drift(p, "created", "did not exist before the run and does now"));
        }
        continue;
      }
      if (!exists) {
        drift.push(new Drift(p, "missing", "existed before the run and is gone"));
        continue;
      }
      const [d, size] = digestFile(p);
      if (d !== entry.digest) {
        drift.push(
          new Drift(
            p,
            "changed",
            `${entry.size} bytes -> ${size} bytes, digest ` +
              `${entry.digest.slice(0, 12)} -> ${d.slice(0, 12)}`
          )
        );
      }
    }
    return drift;
  }

  /**
   * Put back everything that drifted, and verify again. Returns what is STILL wrong.
   *
   * It refuses rather than half-works when the manifest holds no content: a digest-only
   * record can tell you the tree is wrong and cannot tell you what it should have been,
   * and pretending otherwise is how a "restore" that silently did nothing gets reported
   * as a fix.
   */
  restore() {
    if (!this.keepContent) {
      throw new Error(
        "this manifest holds digests but no content, so it can verify and cannot " +
          "restore — record with keepContent: true to be able to put files back"
      );
    }
    for (const [p, entry] of Object.entries(this.entries)) {
      if (!entry.existed) {
        if (fs.existsSync(p)) {
          try {
            fs.rmSync(p);
          } catch {
            /* reported by the verify below */
          }
        }
        continue;
      }
      if (!entry.copy || !fs.existsSync(entry.copy)) continue;
      try {
        fs.mkdirSync(path.dirname(p), { recursive: true });
        fs.copyFileSync(entry.copy, p);
        fs.chmodSync(p, entry.mode);
      } catch {
        /* reported by the verify below */
      }
    }
    return this.verify();
  }

  /** Throw the snapshots away. Verification is impossible afterwards. */
  discard() {
    if (this.scratch) {
      fs.rmSync(this.scratch, { recursive: true, force: true });
      this.scratch = null;
    }
  }

  get size() {
    return Object.keys(this.entries).length;
  }
}

/** What to print. Says the denominator even when it is zero. */
export function driftReport(drift) {
  if (!drift.length) {
    return "the tree came back: every file matches the digest recorded before the run";
  }
  const lines = [`THE TREE DID NOT COME BACK — ${drift.length} file(s):`];
  for (const d of drift) lines.push("  " + d.toString());
  lines.push("");
  lines.push("  Everything measured after this point scores code nobody wrote.");
  return lines.join("\n");
}

/**
 * `JSON.stringify` follows insertion order; Python's `json.dump(sort_keys=True)` sorts.
 * Sorting here is what makes the two halves produce the same bytes for the same tree,
 * which is what lets a manifest be diffed rather than merely parsed.
 */
function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortDeep(value[key]);
    return out;
  }
  return value;
}
