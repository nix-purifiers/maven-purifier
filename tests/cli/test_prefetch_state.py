# SPDX-License-Identifier: MIT
import json
import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ["MAVEN_PURIFIER_CLI"]).parent))
import prefetch_state as state


class ExternalState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.environment = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "xdg")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def view(self):
        generation = state.operation(self.project, {"lockHash": "selected-lock"})
        (generation / "repository").mkdir()
        paths = {"repository": str(generation / "repository")}
        files = {}
        for key, name in [("settings", "settings.xml"), ("launcher", "mvn"), ("projectInputs", "inputs.properties")]:
            path = generation / name
            state.atomic_write(path, b"owned configuration")
            paths[key] = str(path)
            files[name] = state.file_digest(path)
        manifest = {"projectRoot": str(self.project), "paths": paths, "files": files,
                    "managedConfigHash": state.digest(b"selected suffix")}
        state.complete(generation, manifest)
        return generation

    def test_each_operation_has_its_own_persistent_final_path(self):
        first = state.operation(self.project, {"lockHash": "A"})
        second = state.operation(self.project, {"lockHash": "A"})
        third = state.operation(self.project, {"lockHash": "B"})
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.parents[1], third.parents[1])
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        alias = self.base / "project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(state.namespace(alias), state.namespace(self.project))
        state.atomic_write(first / "manifest.json", b"first operation")
        self.assertEqual((first / "manifest.json").read_bytes(), b"first operation")
        self.assertTrue(second.is_dir())

    def test_canonical_state_cannot_enter_the_source_tree(self):
        link = self.base / "state-link"
        link.symlink_to(self.project, target_is_directory=True)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(link)}):
            with self.assertRaises(state.StateError):
                state.namespace(self.project)

    def test_relative_xdg_uses_home_fallback(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative", "HOME": str(self.base / "home")}):
            path = state.namespace(self.project)
            self.assertTrue(path.is_relative_to(self.base / "home/.local/state"))

    def test_unsafe_namespace_permissions_are_not_repaired(self):
        path = state.namespace(self.project)
        path.chmod(0o777)
        with self.assertRaises(state.StateError):
            state.namespace(self.project)
        self.assertEqual(path.stat().st_mode & 0o777, 0o777)

    def test_manifest_drift_and_cross_project_association_fail(self):
        generation = self.view()
        state.validate(generation, self.project, b"selected suffix")
        with self.assertRaises(state.StateError):
            state.validate(generation, self.project, b"different suffix")
        (generation / "settings.xml").write_text("changed settings")
        with self.assertRaises(state.StateError):
            state.validate(generation, self.project)
        other = self.base / "other"
        other.mkdir()
        with self.assertRaises(state.StateError):
            state.validate(generation, other)

    def test_atomic_config_preserves_original_prefix_bytes(self):
        prefix = b"--batch-mode\r\n-Duser.setting=retained"
        content, suffix = state.config_bytes(prefix, ["-Dmaven-purifier.state=/external/view"])
        self.assertTrue(content.startswith(prefix))
        self.assertEqual(state.split_config(content)[1], suffix)
        path = self.project / ".mvn/maven.config"
        state.atomic_write(path, content)
        generation, observed = state.active_generation(self.project)
        self.assertEqual(generation, Path("/external/view"))
        self.assertEqual(observed, suffix)

    def test_legacy_generated_state_is_not_deleted_or_migrated(self):
        old = self.project / ".maven-purifier/repository"
        old.mkdir(parents=True)
        sentinel = old / "authored-or-legacy"
        sentinel.write_text("retained")
        with self.assertRaises(state.StateError):
            state.reject_legacy(self.project)
        self.assertEqual(sentinel.read_text(), "retained")

    def test_completed_view_is_not_a_mutable_current_alias(self):
        generation = self.view()
        alias = self.base / "current"
        alias.symlink_to(generation, target_is_directory=True)
        with self.assertRaises(state.StateError):
            state.validate(alias, self.project)
        self.assertEqual(state.validate(generation, self.project)["generation"], str(generation))

    def test_provider_owned_input_has_explicit_lifetime(self):
        generation = state.operation(self.project, {})
        source = self.base / "provider-owned"
        source.mkdir()
        record = state.anchor(generation, source, "provided-prefetch")
        self.assertEqual(record["lifetime"], "provider-owned")
        self.assertNotIn("root", record)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux descendant state observation")
    def test_cancellation_stops_a_term_ignoring_descendant_before_publication(self):
        generation = self.view()
        ready = self.base / "descendant.json"
        heartbeat = self.base / "heartbeat"
        child = ("import os,signal,time,json\nfrom pathlib import Path\n"
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                 f"Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid(),'group':os.getpgrp()}}))\n"
                 f"while True: Path({str(heartbeat)!r}).write_text(str(time.monotonic()));time.sleep(.02)\n")
        launcher = generation / "mvn"
        program = (f"#!{sys.executable}\nimport subprocess,sys,time\n"
                   f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                   "while True:time.sleep(1)\n")
        state.atomic_write(launcher, program.encode(), 0o700)
        content, suffix = state.config_bytes(b"", [f"-Dmaven-purifier.state={generation}"])
        manifest = json.loads((generation / "manifest.json").read_text())
        manifest["files"]["mvn"] = state.file_digest(launcher)
        manifest["managedConfigHash"] = state.digest(suffix)
        manifest["publishedPrefixHash"] = state.digest(b"")
        state.complete(generation, manifest)
        state.atomic_write(self.project / ".mvn/maven.config", content)
        helper = Path(os.environ["MAVEN_PURIFIER_CLI"]).parent / "local_maven.py"
        process = subprocess.Popen([sys.executable, str(helper), str(self.project)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        publisher = None
        descendant = None
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), "Maven-like descendant did not start")
            descendant = json.loads(ready.read_text())
            publish = ("import os,fcntl,json,sys\nfrom pathlib import Path\n"
                       "fd=os.open(sys.argv[1],os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_EX)\n"
                       "p=Path('/proc')/sys.argv[2]/'stat'\n"
                       "print(json.dumps({'state':p.read_text().split(')',1)[1].split()[0] if p.exists() else 'gone'}))\n")
            publisher = subprocess.Popen([sys.executable, "-c", publish, str(self.project), str(descendant["pid"])],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(.05)
            self.assertIsNone(publisher.poll(), "publication did not wait for the Maven view reader")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            output, error = publisher.communicate(timeout=5)
            self.assertEqual(publisher.returncode, 0, error)
            self.assertIn(json.loads(output)["state"], ["gone", "Z"],
                          "a live descendant survived when the publication lock was released")
            self.assertNotEqual(process.returncode, 0)
        finally:
            if descendant is not None:
                try:
                    os.killpg(descendant["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for target in [process, publisher]:
                if target is not None and target.poll() is None:
                    target.kill()
                    target.communicate(timeout=5)

    def test_successful_parent_keeps_lock_until_its_descendant_finishes(self):
        ready = self.base / "ready"
        finish = self.base / "finish"
        child = ("import time\nfrom pathlib import Path\n"
                 f"Path({str(ready)!r}).touch()\n"
                 f"while not Path({str(finish)!r}).exists():time.sleep(.01)\n")
        parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
        runner = ("import sys\nfrom pathlib import Path\n"
                  f"sys.path.insert(0,{str(Path(os.environ['MAVEN_PURIFIER_CLI']).resolve().parent)!r})\n"
                  "import local_maven,prefetch_state\n"
                  f"with prefetch_state.project_lock(Path({str(self.project)!r}),False):\n"
                  f" raise SystemExit(local_maven.run_maven([sys.executable,'-c',{parent!r}]))\n")
        process = subprocess.Popen([sys.executable, "-c", runner],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        descriptor = os.open(self.project, os.O_RDONLY)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            time.sleep(.05)
            self.assertIsNone(process.poll())
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finish.touch()
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            finish.touch()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            os.close(descriptor)

    def test_directory_lock_serializes_callers_with_different_state_homes(self):
        acquired = self.base / "acquired"
        program = ("from pathlib import Path\nimport sys\n"
                   f"sys.path.insert(0,{str(Path(os.environ['MAVEN_PURIFIER_CLI']).resolve().parent)!r})\n"
                   "import prefetch_state\n"
                   f"with prefetch_state.project_lock(Path({str(self.project)!r}),True):\n"
                   f" Path({str(acquired)!r}).touch()\n")
        environment = dict(os.environ, XDG_STATE_HOME=str(self.base / "other-xdg"))
        with state.project_lock(self.project, exclusive=False):
            process = subprocess.Popen([sys.executable, "-c", program], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                time.sleep(.1)
                self.assertIsNone(process.poll())
                self.assertFalse(acquired.exists())
            except BaseException:
                process.kill()
                process.communicate(timeout=5)
                raise
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertTrue(acquired.exists())

    def test_replaced_project_directory_fails_before_activation(self):
        with state.project_lock(self.project, exclusive=True) as descriptor:
            self.project.rename(self.base / "original-project")
            self.project.mkdir()
            with self.assertRaisesRegex(state.StateError, "project directory changed"):
                state.check_project_directory(self.project, descriptor)


if __name__ == "__main__":
    unittest.main()
