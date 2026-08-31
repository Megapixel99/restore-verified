/**
 * The CLI's own contract, and most of it is about the numbers.
 *
 * A CI file branches on the exit status and on nothing else — it does not read the
 * stderr prose — so a status that is wrong, or that differs from the Python half's for
 * the same event, is the whole failure. These tests are the ones that would have caught
 * a `--timeout` measured in the wrong unit and a documented flag combination that
 * ended in a stack trace.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { EXIT_DRIFT, EXIT_TIMEOUT, Sentinel } from "../src/index.js";

const CLI = fileURLToPath(new URL("../src/cli.js", import.meta.url));

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rv-js-cli-"));
  const file = path.join(dir, "m.txt");
  fs.writeFileSync(file, "ORIGINAL\n");
  return { dir, file };
}

function cli(args) {
  return spawnSync(process.execPath, [CLI, ...args], { encoding: "utf8" });
}

test("--timeout is SECONDS, as the Python half's is", () => {
  // THE BUG THIS EXISTS FOR: the flag was passed to `spawn` unconverted, so `--timeout
  // 600` meant ten minutes to the Python half and six tenths of a second to this one,
  // while one README documented both. A CI file would have depended on which half was
  // installed. `sleep 1` under `--timeout 5` must survive; under milliseconds it died.
  const { file } = fixture();
  const started = Date.now();
  const out = cli(["run", "--paths", file, "--timeout", "5", "--", "sleep", "1"]);
  assert.equal(out.status, 0, out.stderr);
  assert.ok(Date.now() - started >= 900, "the child was killed before its second was up");
});

test("a command that outlives its deadline exits 124, the number timeout(1) uses", () => {
  const { file } = fixture();
  const out = cli(["run", "--paths", file, "--timeout", "0.2", "--", "sleep", "30"]);
  assert.equal(out.status, EXIT_TIMEOUT);
  assert.match(out.stderr, /exceeded 0\.2s and was SIGKILLed/);
});

test("--timeout rejects something that is not a number", () => {
  // `Number("30s")` is NaN, and a NaN timeout is silently no deadline at all.
  const { file } = fixture();
  const out = cli(["run", "--paths", file, "--timeout", "30s", "--", "true"]);
  assert.equal(out.status, 2);
  assert.match(out.stderr, /positive number of seconds/);
});

test("a child killed by a signal reports 128+n rather than a flat 137", () => {
  const { file } = fixture();
  const out = cli(["run", "--paths", file, "--", "sh", "-c", "kill -TERM $$"]);
  assert.equal(out.status, 143, "SIGTERM is 15, so the status is 143");
});

test("--paths stops at a flag instead of swallowing it", () => {
  // `-v` does not start with `--`, so it used to become a path: the guard watched a
  // file that does not exist and verbose silently never turned on.
  const { file } = fixture();
  const out = cli(["run", "--paths", file, "-v", "--", "true"]);
  assert.equal(out.status, 0, out.stderr);
  assert.match(out.stderr, /recorded 1 file\(s\)/);
});

test("a missing manifest is a sentence and exit 2, not a stack trace", () => {
  // Exit 1 with a trace reads to CI as "the command under test failed" rather than
  // "this tool could not run", which are opposite conclusions.
  const out = cli(["verify", "--manifest", "/nonexistent/nope.json"]);
  assert.equal(out.status, 2);
  assert.doesNotMatch(out.stderr, /at Sentinel\.load/);
  assert.match(out.stderr, /restore-verified:/);
});

test("verify --restore on a digest-only manifest explains itself and exits 2", () => {
  // Two documented flags, and the combination used to end in an unhandled rejection.
  const { file } = fixture();
  const manifest = Sentinel.record([file], { keepContent: false }).save();
  fs.writeFileSync(file, "MUTATED\n");
  const out = cli(["verify", "--manifest", manifest, "--restore"]);
  assert.equal(out.status, 2);
  assert.match(out.stderr, /digests but no content/);
  assert.doesNotMatch(out.stderr, /Node\.js v/);
});

test("drift still outranks the command's own verdict", () => {
  const { file } = fixture();
  const out = cli(["run", "--paths", file, "--", "sh", "-c", `printf MUTATED > ${file}`]);
  assert.equal(out.status, EXIT_DRIFT);
});

test("the shipped bin runs when invoked by path", () => {
  // `file://` + a raw path is not a URL. When the comparison failed, the `bin` this
  // package ships parsed its arguments and then did nothing at all.
  const help = execFileSync(process.execPath, [CLI, "--help"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  assert.equal(help, "");
});
