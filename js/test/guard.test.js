import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { Guard, RestoreFailed, Sentinel, guarded } from "../src/index.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHILD = path.join(HERE, "child.mjs");
const ORIGINAL = "export function f(x) {\n  return x + 1;\n}\n";

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rv-js-test-"));
  const file = path.join(dir, "m.js");
  fs.writeFileSync(file, ORIGINAL);
  return { dir, file, read: () => fs.readFileSync(file, "utf8") };
}

test("it restores, and the write took effect", async () => {
  const { file, read } = fixture();
  await guarded(file, async (g) => {
    g.write("MUTATED\n");
    // The mutation must actually be on disk, or every other assertion here is about a
    // guard that never had anything to restore.
    assert.equal(read(), "MUTATED\n");
  });
  assert.equal(read(), ORIGINAL);
});

test("an exception does not skip the restore", async () => {
  const { file, read } = fixture();
  await assert.rejects(
    () => guarded(file, async (g) => { g.write("MUTATED\n"); throw new Error("boom"); }),
    /boom/
  );
  assert.equal(read(), ORIGINAL);
});

test("read() gives the original even after the write", async () => {
  const { file, read } = fixture();
  await guarded(file, async (g) => {
    g.write("MUTATED\n");
    assert.equal(g.read(), ORIGINAL);
  });
  assert.equal(read(), ORIGINAL);
});

test("nothing is written beside the code under test", async () => {
  const { dir, file } = fixture();
  const before = new Set(fs.readdirSync(dir));
  await guarded(file, async (g) => {
    g.write("MUTATED\n");
    assert.deepEqual(new Set(fs.readdirSync(dir)), before, "the guard left scratch state");
  });
  assert.deepEqual(new Set(fs.readdirSync(dir)), before);
});

test("a file that did not exist is removed again", async () => {
  const { dir } = fixture();
  const created = path.join(dir, "created.js");
  await guarded(created, async (g) => {
    g.write("MUTATED\n");
    assert.ok(fs.existsSync(created));
  });
  assert.equal(fs.existsSync(created), false);
});

test("writing an unguarded path is refused", async () => {
  const { dir, file } = fixture();
  const other = path.join(dir, "other.js");
  fs.writeFileSync(other, "x\n");
  await guarded(file, async (g) => {
    assert.throws(() => g.write(other, "MUTATED\n"), /not guarded/);
  });
});

// The package's own name: a restore that ran is not a restore that worked.
test("a restore that writes the wrong bytes is caught", async () => {
  const { file } = fixture();
  await assert.rejects(
    () =>
      guarded(file, async (g) => {
        g.write("MUTATED\n");
        // Sabotage the snapshot so the restore path runs perfectly and puts back
        // something that is not what was there. Nothing about the control flow is
        // wrong — only the bytes — which is exactly what try/finally cannot see.
        const snap = g._snapshots.get(path.resolve(file));
        fs.writeFileSync(snap.copy, "NOT WHAT WAS THERE\n");
      }),
    /different digest/
  );
});

test("the check can pass", async () => {
  // A verification that always failed would satisfy the test above and be useless.
  const { file, read } = fixture();
  await guarded(file, async (g) => g.write("MUTATED\n"));
  assert.equal(read(), ORIGINAL);
});

test("the mtime comes back too", async () => {
  const { file } = fixture();
  const before = fs.statSync(file).mtimeMs;
  await new Promise((r) => setTimeout(r, 20));
  await guarded(file, async (g) => g.write("MUTATED\n"));
  assert.ok(Math.abs(fs.statSync(file).mtimeMs - before) < 5);
});

// ---- the half try/finally does not cover, tested by killing a real process ---- //

function spawnChild(mode, file, read) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CHILD, mode, file], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (d) => {
      out += d;
      if (out.includes("READY")) {
        try {
          assert.equal(read(), "MUTATED\n", "the child never broke the file");
          resolve(child);
        } catch (err) {
          reject(err);
        }
      }
    });
    child.on("error", reject);
  });
}

function killAndWait(child, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`the child ignored ${signal} — a swallowed signal is a hang`));
    }, 10000);
    child.on("close", (code, sig) => {
      clearTimeout(timer);
      resolve({ code, signal: sig });
    });
    child.kill(signal);
  });
}

test("SIGTERM leaves a try/finally harness broken", async () => {
  // THE CONTROL, and the reason this package exists. If this ever passes, the premise
  // is wrong and the guard is unnecessary.
  const { file, read } = fixture();
  const child = await spawnChild("naive", file, read);
  await killAndWait(child, "SIGTERM");
  assert.equal(
    read(),
    "MUTATED\n",
    "try/finally survived SIGTERM — the premise of this package is wrong"
  );
});

test("SIGTERM does not leave a guarded harness broken", async () => {
  const { file, read } = fixture();
  const child = await spawnChild("guarded", file, read);
  await killAndWait(child, "SIGTERM");
  assert.equal(read(), ORIGINAL);
});

test("SIGINT does not leave a guarded harness broken", async () => {
  const { file, read } = fixture();
  const child = await spawnChild("guarded", file, read);
  await killAndWait(child, "SIGINT");
  assert.equal(read(), ORIGINAL);
});

test("the signal is re-delivered, so a kill still kills", async () => {
  // In Node, registering a handler PREVENTS the default termination. A guard that
  // catches SIGTERM, tidies up and returns has converted `kill` into "nothing
  // happened", which is a worse bug than the one it fixed.
  const { file, read } = fixture();
  const child = await spawnChild("guarded", file, read);
  const { signal } = await killAndWait(child, "SIGTERM");
  assert.equal(
    signal,
    "SIGTERM",
    `the child exited on ${signal}; a guard that returns normally has made kill a no-op`
  );
});

test("SIGKILL defeats the guard, and the sentinel catches it", async () => {
  // The honest limit, asserted rather than described.
  const { file, read } = fixture();
  const sentinel = Sentinel.record([file], { keepContent: true });
  const child = await spawnChild("guarded", file, read);
  await killAndWait(child, "SIGKILL");
  assert.equal(read(), "MUTATED\n", "SIGKILL was survived, so this test proves nothing");
  const drift = sentinel.verify();
  assert.equal(drift.length, 1);
  assert.equal(drift[0].kind, "changed");
  assert.deepEqual(sentinel.restore(), []);
  assert.equal(read(), ORIGINAL);
});

test("catchSignals: false is reported, not silent", async () => {
  const { file, read } = fixture();
  await guarded(
    file,
    async (g) => {
      assert.match(g.signalNote, /switched off/);
      g.write("MUTATED\n");
    },
    { catchSignals: false }
  );
  assert.equal(read(), ORIGINAL, "the restore must still happen");
});
