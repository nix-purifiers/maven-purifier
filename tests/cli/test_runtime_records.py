# SPDX-License-Identifier: MIT
"""Collector contract tests; the backend stub records roots, not a runtime-closure proof."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RuntimeRecords(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.main = self.root / "runtime.jsonl"
        self.main.touch()
        self.parts = self.root / "runtime.jsonl.parts"
        self.backend = self.root / "backend"
        self.backend.mkdir()
        (self.backend / "coursier_runtime.py").write_text(
            "import json\n"
            "def export(repository, paths, catalog, destination):\n"
            " destination.write_text(json.dumps(paths))\n")
        self.projects = self.root / "projects.json"
        self.projects.write_text('[{"groupId":"published","artifactId":"app"}]')
        self.destination = self.root / "export.json"
        default = Path(os.environ["MAVEN_PURIFIER_CLI"]).resolve().parent.parent / "pkgs/build-maven-project/export-runtime.py"
        self.script = Path(os.environ.get("MAVEN_PURIFIER_RUNTIME_EXPORT", str(default)))

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, path=None, **changes):
        event = dict(event="runtime", projectGroupId="published", projectArtifactId="app",
                     groupId="dependency", artifactId="library", version="1",
                     file=str(path or self.root / "repository/dependency/library/1/library-1.jar"))
        return dict(event, **changes)

    def shard(self, name, data, phase="build"):
        path = self.parts / phase / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def line(self, event):
        return (json.dumps(event) + "\n").encode()

    def run_collector(self):
        return subprocess.run([sys.executable, str(self.script), str(self.backend), str(self.main),
                               str(self.root / "repository"), str(self.root / "catalog"),
                               str(self.destination), str(self.projects)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_empty_main_merges_phase_shards_and_keeps_distinct_classifiers(self):
        base = self.event()
        classifier = self.event(self.root / "repository/dependency/library/1/library-1-tests.jar")
        self.shard("record-a.jsonl", self.line(base) + self.line(dict(event="repository", repository="central")))
        self.shard("record-b.jsonl", self.line(base) + self.line(classifier), "development")
        self.shard("record-c.jsonl", self.line(self.event(projectArtifactId="unpublished-tool")))
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.destination.read_text()), sorted([base["file"], classifier["file"]]))

    def test_same_file_with_conflicting_coordinates_fails_before_export(self):
        self.main.write_bytes(self.line(self.event()))
        self.shard("record-conflict.jsonl", self.line(self.event(groupId="different")))
        result = self.run_collector()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"conflicting Maven runtime coordinates", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_corrupt_or_unfinished_shards_are_not_filtered_as_irrelevant_events(self):
        for data in [b'{"event":"repository"}', b'{broken}\n', b'[]\n', b'\xff\n']:
            with self.subTest(data=data):
                self.shard("record-invalid.jsonl", data)
                self.assertNotEqual(self.run_collector().returncode, 0)
                self.assertFalse(self.destination.exists())

    def test_empty_files_are_valid_and_relative_runtime_paths_are_rejected(self):
        self.shard("record-empty.jsonl", b"")
        self.assertEqual(self.run_collector().returncode, 0)
        self.assertEqual(json.loads(self.destination.read_text()), [])
        self.destination.unlink()
        self.shard("record-relative.jsonl", self.line(self.event(file="relative.jar")))
        self.assertNotEqual(self.run_collector().returncode, 0)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
