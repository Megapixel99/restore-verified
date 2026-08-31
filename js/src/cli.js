#!/usr/bin/env node
/**
 * `restore-verified` — run something that edits files, and check the tree came back.
 *
 *     restore-verified run --paths src/ -- npx jscodeshift -t codemod.js src/
 *     restore-verified run --paths src/ --timeout 600 --restore -- ./harness.sh
 *
 *     restore-verified record --paths src/ --manifest /tmp/before.json
 *     restore-verified verify --manifest /tmp/before.json
 *
 * `run` is the one that matters, and `--timeout` is why. A child that overruns its
 * deadline is SIGKILLed, which is the commonest way in practice for a file-editing tool
 * to be killed at exactly the moment its file is broken. This command both imposes that
 * deadline and catches what it does.
 *
 * The commands, the flags and the exit codes are the Python half's, deliberately. Two
 * halves that disagreed about `--restore` or about exit 3 would make a CI file depend
 * on which one happened to be installed.
 */

import { spawn } from "node:child_process";
import process from "node:process";

import { Sentinel, driftReport } from "./sentinel.js";
import { EXIT_DRIFT } from "./index.js";

function usage() {
  process.stderr.write(`restore-verified — run something that edits files, and prove the tree came back.

  restore-verified run    --paths P... [--timeout MS] [--restore] [-v] -- COMMAND...
  restore-verified record --paths P... [--manifest FILE] [--keep-content]
  restore-verified verify --manifest FILE [--restore]

  --paths P...       files or directories to watch
  --pattern GLOB     only watch files matching this glob (repeatable)
  --keep-content     copy the files too, so they can be restored and not merely
                     checked (implied by --restore)
  --timeout MS       SIGKILL the command after MS and verify anyway — the case
                     this tool exists for
  --restore          put drifted files back from the snapshot (still exits ${EXIT_DRIFT})

Exit: 0 the tree came back · ${EXIT_DRIFT} it did not · 2 this tool could not run ·
      otherwise the command's own status.
`);
}

function parse(argv) {
  const sep = argv.indexOf("--");
  const flags = sep === -1 ? argv : argv.slice(0, sep);
  const command = sep === -1 ? [] : argv.slice(sep + 1);
  const opts = { paths: [], pattern: [], keepContent: false, restore: false, verbose: false };
  for (let i = 0; i < flags.length; i++) {
    const f = flags[i];
    const value = () => {
      const v = flags[++i];
      if (v === undefined) throw new Error(`${f} needs a value`);
      return v;
    };
    if (f === "--paths") {
      while (flags[i + 1] !== undefined && !flags[i + 1].startsWith("--")) opts.paths.push(flags[++i]);
    } else if (f === "--pattern") opts.pattern.push(value());
    else if (f === "--manifest") opts.manifest = value();
    else if (f === "--timeout") opts.timeout = Number(value());
    else if (f === "--keep-content") opts.keepContent = true;
    else if (f === "--restore") opts.restore = true;
    else if (f === "-v" || f === "--verbose") opts.verbose = true;
    else if (f === "-h" || f === "--help") opts.help = true;
    else if (i > 0 || !["run", "record", "verify"].includes(f)) {
      throw new Error(`unknown option ${f}`);
    }
  }
  return { cmd: flags[0], opts, command };
}

function runCommand(command, timeout) {
  return new Promise((resolve) => {
    const child = spawn(command[0], command.slice(1), {
      stdio: "inherit",
      // SIGKILL rather than the default SIGTERM, to mirror what a real runner's
      // deadline does — and because a SIGTERM the child could catch would not
      // exercise the case this tool exists for.
      killSignal: "SIGKILL",
      ...(timeout ? { timeout } : {}),
    });
    child.on("error", (err) => resolve({ code: 2, error: err }));
    child.on("close", (code, signal) => resolve({ code: code === null ? 137 : code, signal }));
  });
}

async function cmdRun(opts, command) {
  if (!command.length) {
    process.stderr.write("restore-verified run: nothing to run after `--`\n");
    return 2;
  }
  const sentinel = Sentinel.record(opts.paths, {
    keepContent: opts.restore || opts.keepContent,
    patterns: opts.pattern.length ? opts.pattern : null,
  });
  if (opts.verbose) {
    process.stderr.write(`[restore-verified] recorded ${sentinel.size} file(s)\n`);
  }

  const { code, signal, error } = await runCommand(command, opts.timeout);
  if (error) {
    process.stderr.write(`restore-verified: cannot run ${command[0]} (${error.message})\n`);
    sentinel.discard();
    return 2;
  }
  if (signal === "SIGKILL" && opts.timeout) {
    process.stderr.write(
      `[restore-verified] the command exceeded ${opts.timeout}ms and was SIGKILLed — ` +
        `no handler, no \`finally\`, no cleanup ran\n`
    );
  }

  const drift = sentinel.verify();
  if (drift.length && opts.restore) {
    const still = sentinel.restore();
    if (still.length) {
      process.stderr.write(driftReport(still) + "\n");
      process.stderr.write(
        `[restore-verified] restored from the snapshot and ${still.length} file(s) are STILL wrong\n`
      );
      sentinel.discard();
      return EXIT_DRIFT;
    }
    process.stderr.write(
      `[restore-verified] ${drift.length} file(s) did not come back; restored from the ` +
        `snapshot and verified\n`
    );
    for (const d of drift) process.stderr.write(`    ${d}\n`);
    sentinel.discard();
    // STILL A FAILURE. That this command could put the tree back does not make the run
    // trustworthy, it makes it recoverable.
    return EXIT_DRIFT;
  }

  sentinel.discard();
  if (drift.length) {
    process.stderr.write(driftReport(drift) + "\n");
    if (!opts.restore) {
      process.stderr.write("  Re-run with --restore to put them back from the snapshot.\n");
    }
    return EXIT_DRIFT;
  }
  if (opts.verbose) process.stderr.write(`[restore-verified] ${driftReport(drift)}\n`);
  return code;
}

function cmdRecord(opts) {
  const sentinel = Sentinel.record(opts.paths, {
    keepContent: opts.keepContent,
    patterns: opts.pattern.length ? opts.pattern : null,
  });
  const target = sentinel.save(opts.manifest ?? null);
  process.stdout.write(target + "\n");
  process.stderr.write(
    `[restore-verified] recorded ${sentinel.size} file(s)` +
      `${sentinel.keepContent ? " with content" : " (digests only)"}\n`
  );
  return 0;
}

function cmdVerify(opts) {
  if (!opts.manifest) {
    process.stderr.write("restore-verified verify: --manifest is required\n");
    return 2;
  }
  const sentinel = Sentinel.load(opts.manifest);
  const drift = sentinel.verify();
  if (drift.length && opts.restore) {
    const still = sentinel.restore();
    process.stderr.write(driftReport(drift) + "\n");
    if (still.length) {
      process.stderr.write(`[restore-verified] ${still.length} still wrong after restore\n`);
    }
    return EXIT_DRIFT;
  }
  process.stdout.write(driftReport(drift) + "\n");
  return drift.length ? EXIT_DRIFT : 0;
}

export async function main(argv) {
  let parsed;
  try {
    parsed = parse(argv);
  } catch (err) {
    process.stderr.write(`restore-verified: ${err.message}\n`);
    return 2;
  }
  const { cmd, opts, command } = parsed;
  if (opts.help || !cmd) {
    usage();
    return cmd ? 0 : 2;
  }
  if (cmd === "run") return cmdRun(opts, command);
  if (cmd === "record") return cmdRecord(opts);
  if (cmd === "verify") return cmdVerify(opts);
  usage();
  return 2;
}

const invokedDirectly =
  process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href;
if (invokedDirectly) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
