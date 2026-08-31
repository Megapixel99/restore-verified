/**
 * restore-verified — temporarily modify a file, survive the signal, and prove it came back.
 *
 * The JavaScript half. It answers the same three questions the Python half answers, and
 * writes the same manifest, so the process that breaks a tree and the process that
 * checks it came back need not be the same language.
 */

export { Guard, RestoreFailed, guarded, DEFAULT_SIGNALS } from "./guard.js";
export {
  Sentinel,
  Drift,
  driftReport,
  MANIFEST_VERSION,
  SKIP_DIRS,
} from "./sentinel.js";

// THE TREE BEING WRONG OUTRANKS THE COMMAND'S OWN VERDICT, so it has an exit code of
// its own rather than reusing 1 — and it is the same 3 the Python half uses, because a
// CI file should not have to ask which half produced the number it is branching on.
export const EXIT_DRIFT = 3;

// The deadline expired. `timeout(1)`'s number, and the Python half's — exported for the
// same reason 3 is: it is a number a CI file branches on, so the two halves are held to
// it by the parity suite rather than by each half being trusted about itself.
export const EXIT_TIMEOUT = 124;
