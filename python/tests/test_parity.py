"""The two halves agree about the manifest, or the manifest is not a contract.

THIS IS THE REASON THE TWO HALVES SHIP FROM ONE REPOSITORY. `Sentinel` exists because
the process that breaks a tree may be gone — killed, timed out, crashed — by the time
anybody asks whether the tree came back. That process is frequently not the one doing
the asking, and it is frequently not the same language: a Node build script invokes a
Python codemod, or a Python CI harness shells out to `jscodeshift`.

So the manifest has to be ONE DOCUMENT, and "one document" is a claim about bytes rather
than about intentions. These tests round-trip a real manifest in both directions and
compare the verdicts, because two implementations that merely resemble each other are
two implementations that will disagree on the day it matters.

The tests skip — loudly, with a reason — when `node` is not on PATH, so a Python-only
contributor can still run the suite. CI asserts they were not skipped, because a skipped
parity test and a passing one look identical in a tally.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # python/
REPO = os.path.dirname(ROOT)
JS = os.path.join(REPO, "js", "src", "index.js")
sys.path.insert(0, ROOT)

from restore_verified import Sentinel  # noqa: E402
from restore_verified.sentinel import MANIFEST_VERSION  # noqa: E402

NODE = shutil.which("node")


def run_node(script):
    """Run `script` with the JS half importable, and return parsed stdout."""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node is not on PATH, so the cross-half contract cannot be checked")
class TheManifestIsOneDocument(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rv-parity-")
        self.file = os.path.join(self.dir, "m.txt")
        with open(self.file, "w") as fh:
            fh.write("ORIGINAL\n")

    def test_the_two_halves_agree_on_the_version_number(self):
        # A version that disagrees is a manifest one half silently refuses.
        js = run_node(
            f"import {{ MANIFEST_VERSION }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify(MANIFEST_VERSION));"
        )
        self.assertEqual(js, MANIFEST_VERSION)

    def test_the_two_halves_agree_on_the_drift_exit_code(self):
        # A CI file branches on this number and must not have to ask which half ran.
        from restore_verified.cli import EXIT_DRIFT

        js = run_node(
            f"import {{ EXIT_DRIFT }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify(EXIT_DRIFT));"
        )
        self.assertEqual(js, EXIT_DRIFT)

    def test_the_two_halves_skip_the_same_directories(self):
        # Two halves that disagreed about what they walked would disagree about whether
        # a tree came back.
        from restore_verified.sentinel import SKIP_DIRS

        js = run_node(
            f"import {{ SKIP_DIRS }} from {json.dumps(JS)};"
            f"console.log(JSON.stringify([...SKIP_DIRS].sort()));"
        )
        self.assertEqual(sorted(js), sorted(SKIP_DIRS))

    def test_python_writes_a_manifest_that_javascript_verifies(self):
        sentinel = Sentinel.record([self.file], keep_content=True)
        manifest = sentinel.save(os.path.join(self.dir, "from-python.json"))

        # Break the tree AFTER recording, exactly as a killed harness would leave it.
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        drift = run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.load({json.dumps(manifest)});"
            f"console.log(JSON.stringify(s.verify().map(d => [d.kind, d.path])));"
        )
        self.assertEqual(len(drift), 1, "the JavaScript half did not see the change")
        self.assertEqual(drift[0][0], "changed")
        self.assertEqual(drift[0][1], os.path.abspath(self.file))

    def test_javascript_writes_a_manifest_that_python_verifies(self):
        manifest = os.path.join(self.dir, "from-js.json")
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: true }});"
            f"console.log(JSON.stringify(s.save({json.dumps(manifest)})));"
        )
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        drift = Sentinel.load(manifest).verify()
        self.assertEqual(len(drift), 1, "the Python half did not see the change")
        self.assertEqual(drift[0].kind, "changed")
        self.assertEqual(drift[0].path, os.path.abspath(self.file))

    def test_a_manifest_written_by_javascript_can_be_RESTORED_by_python(self):
        # Verification is the cheap half. Restoring across the boundary means the
        # content copies referenced by the manifest are found and used by the other
        # runtime — which is the part that actually gets somebody's work back.
        manifest = os.path.join(self.dir, "restorable.json")
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: true }});"
            f"console.log(JSON.stringify(s.save({json.dumps(manifest)})));"
        )
        with open(self.file, "w") as fh:
            fh.write("MUTATED\n")

        still = Sentinel.load(manifest).restore()
        self.assertEqual(still, [], "Python could not restore from a JavaScript manifest")
        with open(self.file) as fh:
            self.assertEqual(fh.read(), "ORIGINAL\n")

    def test_the_two_halves_produce_BYTE_IDENTICAL_manifests_for_one_tree(self):
        """The strongest form of the claim, and the one that catches quiet drift.

        Verdicts agreeing proves the two halves read the document the same way. Bytes
        agreeing proves they WRITE it the same way, which is what makes a manifest
        diffable across the boundary and what stops one half adding a field the other
        will silently ignore.
        """
        py_manifest = os.path.join(self.dir, "py.json")
        js_manifest = os.path.join(self.dir, "js.json")
        Sentinel.record([self.file], keep_content=False).save(py_manifest)
        run_node(
            f"import {{ Sentinel }} from {json.dumps(JS)};"
            f"const s = Sentinel.record([{json.dumps(self.file)}], {{ keepContent: false }});"
            f"console.log(JSON.stringify(s.save({json.dumps(js_manifest)})));"
        )
        with open(py_manifest) as fh:
            py = json.load(fh)
        with open(js_manifest) as fh:
            js = json.load(fh)

        # `root` is the working directory of whichever process recorded, and `scratch`
        # is a temp path. Neither is part of the contract; everything else is.
        for doc in (py, js):
            doc.pop("root", None)
            doc.pop("scratch", None)
        self.assertEqual(py, js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
