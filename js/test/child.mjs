/**
 * A child process that breaks a file and then waits to be killed.
 *
 * It exists so the signal tests kill a REAL process rather than calling a handler
 * directly. A test that invokes the handler by hand proves the handler runs; it says
 * nothing about whether the signal reaches it, or what the file on disk looks like
 * afterwards — which are the two things in question.
 *
 *   node child.mjs guarded PATH   # this package's guard
 *   node child.mjs naive   PATH   # try/finally, which is what everyone writes
 *
 * It prints READY and flushes before waiting, so the parent kills it at a known point
 * rather than racing the runtime's start-up.
 */

import fs from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { guarded } = await import(path.join(HERE, "..", "src", "guard.js"));

const MUTATED = "MUTATED\n";
const [mode, target] = process.argv.slice(2);

// A bare `new Promise(() => {})` holds NO handle, so the event loop would be empty
// and Node would exit on its own. A real harness is running something; the interval
// is the smallest honest stand-in for that.
const forever = () => new Promise(() => { setInterval(() => {}, 1000); });

if (mode === "guarded") {
  await guarded(target, async (g) => {
    g.write(MUTATED);
    process.stdout.write("READY\n");
    await forever();
  });
} else if (mode === "naive") {
  // What a careful person writes. It survives an exception and it does NOT survive
  // SIGTERM, because Node's default disposition terminates the process without
  // unwinding — so the `finally` never runs.
  const original = fs.readFileSync(target, "utf8");
  try {
    fs.writeFileSync(target, MUTATED);
    process.stdout.write("READY\n");
    await forever();
  } finally {
    fs.writeFileSync(target, original);
  }
} else {
  process.stderr.write(`unknown mode ${mode}\n`);
  process.exit(2);
}
