import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { Sentinel, driftReport } from "../src/index.js";

function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rv-js-sent-"));
  const file = path.join(dir, "m.js");
  fs.writeFileSync(file, "export const x = 1;\n");
  return { dir, file };
}

test("a clean run reports no drift", () => {
  const { dir } = fixture();
  assert.deepEqual(Sentinel.record([dir]).verify(), []);
});

test("it distinguishes changed, missing and created", () => {
  // Three outcomes rather than "not clean": a missing file and a changed one send you
  // to opposite ends of the problem.
  const { dir, file } = fixture();
  const gone = path.join(dir, "gone.js");
  const created = path.join(dir, "new.js");
  fs.writeFileSync(gone, "bye\n");

  const sentinel = Sentinel.record([dir, created]);
  fs.writeFileSync(file, "MUTATED\n");
  fs.rmSync(gone);
  fs.writeFileSync(created, "hello\n");

  const kinds = Object.fromEntries(sentinel.verify().map((d) => [d.kind, d.path]));
  assert.deepEqual(new Set(Object.keys(kinds)), new Set(["changed", "missing", "created"]));
  assert.equal(kinds.changed, file);
});

test("a manifest survives the process that wrote it", () => {
  const { file } = fixture();
  const sentinel = Sentinel.record([file], { keepContent: true });
  const manifest = sentinel.save();
  fs.writeFileSync(file, "MUTATED\n");

  const reloaded = Sentinel.load(manifest);
  assert.equal(reloaded.verify().length, 1);
  assert.deepEqual(reloaded.restore(), []);
  assert.equal(fs.readFileSync(file, "utf8"), "export const x = 1;\n");
});

test("a digest-only manifest refuses to pretend it can restore", () => {
  const { dir, file } = fixture();
  const sentinel = Sentinel.record([dir]); // a directory: digests only
  assert.equal(sentinel.keepContent, false);
  fs.writeFileSync(file, "MUTATED\n");
  assert.equal(sentinel.verify().length, 1);
  assert.throws(() => sentinel.restore(), /cannot restore/);
});

test("a manifest from another version is refused", () => {
  const { file } = fixture();
  const manifest = Sentinel.record([file], { keepContent: true }).save();
  const payload = JSON.parse(fs.readFileSync(manifest, "utf8"));
  payload.manifest = 999;
  fs.writeFileSync(manifest, JSON.stringify(payload));
  assert.throws(() => Sentinel.load(manifest), /do not mean the same thing/);
});

test("node_modules is not walked", () => {
  // Not a nicety: recording a dependency tree would make every install look like drift.
  const { dir } = fixture();
  fs.mkdirSync(path.join(dir, "node_modules"));
  fs.writeFileSync(path.join(dir, "node_modules", "dep.js"), "x\n");
  const sentinel = Sentinel.record([dir]);
  assert.ok(!Object.keys(sentinel.entries).some((p) => p.includes("node_modules")));
});

test("the report says the denominator even when it is zero", () => {
  assert.match(driftReport([]), /every file matches the digest/);
});
