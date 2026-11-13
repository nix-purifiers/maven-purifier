# SPDX-License-Identifier: MIT
"""Unit tests of the lock tool's parsing and path logic (cli/maven_purifier.py).

Run with the module's path in MAVEN_PURIFIER_CLI:

    MAVEN_PURIFIER_CLI=cli/maven_purifier.py python3 -m unittest tests/cli/test_maven_purifier.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(os.environ["MAVEN_PURIFIER_CLI"]).parent))
spec = importlib.util.spec_from_file_location("maven_purifier", os.environ["MAVEN_PURIFIER_CLI"])
assert spec is not None and spec.loader is not None
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

CENTRAL = "https://repo.maven.apache.org/maven2/"
REPO1 = "https://repo1.maven.org/maven2/"


class TemporaryTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


GOOGLE = "https://maven-central.storage-download.googleapis.com/maven2/"


class Mirrors(unittest.TestCase):
    def test_central_files_get_the_canonical_url(self):
        path = "org/example/a/1/a-1.jar"
        for host in (REPO1, CENTRAL, GOOGLE):
            entry = mp.file_entry(path, host + path, "sha256-x")
            self.assertEqual(entry, {"path": path, "url": REPO1 + path, "hash": "sha256-x"})

    def test_other_repositories_keep_their_url(self):
        entry = mp.file_entry("a/b.jar", "https://example.org/a/b.jar", "sha256-x")
        self.assertEqual(entry, {"path": "a/b.jar", "url": "https://example.org/a/b.jar", "hash": "sha256-x"})

    def test_fetch_url_takes_the_central_mirror(self):
        path = "org/example/a/1/a-1.jar"
        with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_CENTRAL_MIRROR": GOOGLE}, clear=True):
            self.assertEqual(mp.fetch_url(CENTRAL + path), GOOGLE + path)
            self.assertEqual(mp.fetch_url("https://example.org/x"), "https://example.org/x")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mp.fetch_url(CENTRAL + path), CENTRAL + path)

    def test_lock_run_mirror_of_central(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run.xml"
            with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_CENTRAL_MIRROR": GOOGLE}, clear=True):
                mp.write_run_settings(ET.Element("settings"), run)
            mirror = ET.parse(run).getroot().find("mirrors/mirror")
            assert mirror is not None
            self.assertEqual(mirror.findtext("id"), "central")
            self.assertEqual(mirror.findtext("url"), GOOGLE)

    def test_settings_mirror_of_central_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run.xml"
            settings = ET.fromstring(
                "<settings><mirrors><mirror><id>corp</id><mirrorOf>*</mirrorOf>"
                "<url>https://repo.example</url></mirror></mirrors></settings>"
            )
            with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_CENTRAL_MIRROR": GOOGLE}, clear=True):
                mp.write_run_settings(settings, run)
            self.assertNotIn(GOOGLE, run.read_text())


class DownloadPath(unittest.TestCase):
    def test_maven_layout_url(self):
        url = CENTRAL + "org/apache/maven/apache-maven/3.9.11/apache-maven-3.9.11-bin.zip"
        self.assertEqual(mp.download_path(url), "org/apache/maven/apache-maven/3.9.11/apache-maven-3.9.11-bin.zip")

    def test_other_url(self):
        self.assertEqual(mp.download_path("https://example.org/dist/maven.zip"), "downloads/example.org/dist/maven.zip")


class GeneratedObr(TemporaryTree):
    def index(self, uri):
        return self.write("repository.xml", '<repository><resource uri="' + uri + '"/></repository>')

    def test_installed_bundle_index_is_generated(self):
        artifact = self.write("example/bundle/1/bundle-1.jar", "jar")
        index = self.index(artifact.as_uri())
        self.assertTrue(mp.generated_obr_index(index, self.root, {"example/bundle/1/bundle-1.jar"}))

    def test_relative_installed_bundle_uri(self):
        self.write("example/bundle/1/bundle-1.jar", "jar")
        self.assertTrue(mp.generated_obr_index(self.index("example/bundle/1/bundle-1.jar"), self.root,
                                               {"example/bundle/1/bundle-1.jar"}))

    def test_download_and_external_uri_are_not_generated(self):
        for uri in ("example/download/1/download-1.jar", "https://example.org/bundle.jar",
                    "../outside.jar", "file:///outside.jar"):
            self.assertFalse(mp.generated_obr_index(self.index(uri), self.root, set()))

    def test_name_alone_does_not_exempt_an_unrecorded_file(self):
        for text in ("not XML", "<repository/>", "<other/>",
                     '<repository><referral url="https://example.org/"/></repository>'):
            path = self.write("repository.xml", text)
            self.assertFalse(mp.generated_obr_index(path, self.root, set()))


class LocalPrefetchState(TemporaryTree):
    def test_projects_get_distinct_stable_owned_directories_outside_source(self):
        first = self.root / "one"
        second = self.root / "two"
        first.mkdir()
        second.mkdir()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "user state")}):
            state = mp.local_prefetch_state(first)
            self.assertEqual(state, mp.local_prefetch_state(first / "."))
            self.assertNotEqual(state, mp.local_prefetch_state(second))
        self.assertFalse(state.is_relative_to(first))
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_project_local_state_and_symlinked_managed_state_are_rejected(self):
        project = self.root / "project"
        project.mkdir()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(project / "state")}):
            with self.assertRaises(SystemExit):
                mp.local_prefetch_state(project)
        base = self.root / "state"
        base.mkdir()
        (base / "maven-purifier").symlink_to(project)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(base)}):
            with self.assertRaises(SystemExit):
                mp.local_prefetch_state(project)
        self.assertEqual(list(project.iterdir()), [])


class PrefetchedToolchains(TemporaryTree):
    def test_native_launcher_passes_configured_toolchains_as_one_path_argument(self):
        prefetched = self.root / "prefetched SDKs"
        (prefetched / "repository").mkdir(parents=True)
        (prefetched / "jdk").mkdir()
        backend = self.write("prefetched SDKs/maven/bin/mvn",
                             f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        backend.chmod(0o755)
        self.write("prefetched SDKs/toolchains.xml", "<toolchains/>")
        lock = {"javaMajor": "21", "maven": {"version": "3.9.11"}}
        def settings(_lock, _repo, target):
            target.write_text("<settings/>")
        state = self.root / mp.STATE_DIR
        state.mkdir()
        (state / "prefetch").symlink_to(prefetched)
        (state / "jdk").symlink_to(prefetched / "jdk")
        with mock.patch.object(mp, "build_root", return_value=self.root), \
             mock.patch.object(mp, "java_home", return_value="/existing/host-java"), \
             mock.patch.object(mp, "read_lock", return_value=lock), \
             mock.patch.object(mp, "check_fresh"), \
             mock.patch.object(mp, "offline_settings", side_effect=settings), \
             mock.patch.object(mp, "local_prefetch_state", return_value=self.root / "external-state"), \
             mock.patch.dict(os.environ, {"MAVEN_PURIFIER_EXTENSION": "/recording-extension.jar"}):
            (self.root / "external-state").mkdir()
            mp.prefetch(["--prefetched", str(prefetched)])
            with mock.patch.object(mp, "nix_build", return_value=str(prefetched)) as build:
                mp.prefetch([])
                self.assertEqual(build.call_args.args[-1], self.root / "external-state/prefetch")
        state = self.root / mp.STATE_DIR
        argv = json.loads(subprocess.check_output([str(state / "bin/mvn"), "verify"], text=True))
        self.assertEqual(argv[-3:], ["--toolchains", str(state / "toolchains.xml"), "verify"])
        self.assertIn(f"--toolchains={state / 'toolchains.xml'}", (self.root / ".mvn/maven.config").read_text())
        self.assertFalse((state / "jdk").is_symlink())
        self.assertFalse((state / "prefetch").is_symlink())
        self.assertEqual((self.root / "external-state/jdk").readlink(), Path("/existing/host-java"))

    def test_links_the_sdk_configuration_without_copying_store_paths_into_source(self):
        prefetched = self.root / "prefetched"
        state = self.root / "state with spaces"
        state.mkdir()
        xml = self.write("prefetched/toolchains.xml", "<toolchains/>")
        target = mp.link_prefetched_toolchains(prefetched, state)
        self.assertEqual(target, state / "toolchains.xml")
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), xml)

    def test_reversing_configuration_removes_a_stale_sdk_binding(self):
        prefetched = self.root / "prefetched"
        prefetched.mkdir()
        state = self.root / "state"
        state.mkdir()
        stale = state / "toolchains.xml"
        stale.symlink_to(self.root / "missing-old-prefetch")
        self.assertIsNone(mp.link_prefetched_toolchains(prefetched, state))
        self.assertFalse(stale.is_symlink())


class TrackedRepository(TemporaryTree):
    def test_resolver_2_key_with_digest(self):
        self.write(
            "a/1/_remote.repositories",
            "#NOTE: internal file\n#Sun Nov 01 2026\n"
            "a-1.jar>central-mirror-98c55166e0603ef6a71b8173dda7810dbd009613=\n",
        )
        self.assertEqual(mp.tracked_repository(self.root / "a/1/a-1.jar"), "central-mirror")

    def test_resolver_1_key(self):
        self.write("a/1/_remote.repositories", "a-1.pom>central=\n")
        self.assertEqual(mp.tracked_repository(self.root / "a/1/a-1.pom"), "central")

    def test_untracked_file(self):
        self.write("a/1/_remote.repositories", "a-1.pom>central=\n")
        self.assertIsNone(mp.tracked_repository(self.root / "a/1/a-1.jar"))
        self.assertIsNone(mp.tracked_repository(self.root / "b/1/b-1.jar"))


class Settings(TemporaryTree):
    SETTINGS = """<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <localRepository>/somewhere</localRepository>
  <offline>true</offline>
  <servers><server><id>corp</id><password>secret</password></server></servers>
  <proxies><proxy><id>p</id><host>proxy.example</host></proxy></proxies>
  <mirrors><mirror><id>corp</id><mirrorOf>*</mirrorOf><url>https://repo.example/maven</url></mirror></mirrors>
</settings>
"""

    def test_record_strips_credentials_and_local_state(self):
        settings, recorded = mp.read_settings(self.write("settings.xml", self.SETTINGS))
        self.assertNotIn("secret", recorded)
        self.assertNotIn("proxy.example", recorded)
        self.assertNotIn("/somewhere", recorded)
        self.assertNotIn("offline", recorded)
        self.assertIn("https://repo.example/maven", recorded)
        self.assertTrue(recorded.startswith('<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">'))
        # The lock run itself keeps the servers and proxies.
        names = [mp.local_name(c.tag) for c in settings]
        self.assertIn("servers", names)
        self.assertIn("proxies", names)
        self.assertNotIn("localRepository", names)

    def test_no_settings(self):
        with mock.patch.object(Path, "home", return_value=self.root):
            settings, recorded = mp.read_settings(None)
        self.assertIsNone(recorded)
        self.assertEqual(settings.tag, "settings")

    def test_default_is_user_settings(self):
        self.write(".m2/settings.xml", "<settings><mirrors/></settings>")
        with mock.patch.object(Path, "home", return_value=self.root):
            _, recorded = mp.read_settings(None)
        self.assertIn("<mirrors", recorded)

    def test_malformed_settings_fail(self):
        with self.assertRaises(SystemExit):
            mp.read_settings(self.write("settings.xml", "<settings>"))

    def test_run_settings_take_the_environment_proxy(self):
        # A made-up proxy URL. gitleaks:allow
        env = {"https_proxy": "http://user%40x:p%3Ass@proxy.local:3128", "no_proxy": "localhost,127.0.0.1"}  # gitleaks:allow
        with mock.patch.dict(os.environ, env, clear=True):
            mp.write_run_settings(ET.Element("settings"), self.root / "run.xml")
        proxy = ET.parse(self.root / "run.xml").getroot().find("proxies/proxy")
        assert proxy is not None
        self.assertEqual(proxy.findtext("host"), "proxy.local")
        self.assertEqual(proxy.findtext("port"), "3128")
        self.assertEqual(proxy.findtext("username"), "user@x")
        self.assertEqual(proxy.findtext("password"), "p:ss")
        self.assertEqual(proxy.findtext("nonProxyHosts"), "localhost|127.0.0.1")

    def test_run_settings_keep_their_own_proxy(self):
        settings, _ = mp.read_settings(self.write("settings.xml", self.SETTINGS))
        with mock.patch.dict(os.environ, {"https_proxy": "http://other:1"}, clear=True):
            mp.write_run_settings(settings, self.root / "run.xml")
        text = (self.root / "run.xml").read_text()
        self.assertIn("proxy.example", text)
        self.assertNotIn("other", text)

    def test_proxy_with_credentials(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://u:p@h:1"}, clear=True):
            self.assertTrue(mp.proxy_with_credentials())
        with mock.patch.dict(os.environ, {"https_proxy": "http://h:1"}, clear=True):
            self.assertFalse(mp.proxy_with_credentials())


class OfflineSettings(TemporaryTree):
    def test_offline_on_the_locked_repository(self):
        lock = {"settings": '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"><mirrors/></settings>\n'}
        mp.offline_settings(lock, self.root / "repository", self.root / "ide.xml")
        settings = ET.parse(self.root / "ide.xml").getroot()
        ns = "{http://maven.apache.org/SETTINGS/1.2.0}"
        self.assertEqual(settings.findtext(f"{ns}localRepository"), str(self.root / "repository"))
        self.assertEqual(settings.findtext(f"{ns}offline"), "true")
        self.assertIsNotNone(settings.find(f"{ns}mirrors"))

    def test_without_recorded_settings(self):
        mp.offline_settings({"settings": None}, self.root / "repository", self.root / "ide.xml")
        settings = ET.parse(self.root / "ide.xml").getroot()
        self.assertEqual(settings.findtext("offline"), "true")


class LocalJava(unittest.TestCase):
    def test_explicit_runtime_wins_over_bundled_default(self):
        with (
            mock.patch.dict(os.environ, {"JAVA_HOME": "/host/sdk", "MAVEN_PURIFIER_DEFAULT_JAVA_HOME": "/bundled/sdk"}, clear=True),
            mock.patch.object(mp, "java_properties", return_value={"java.version": "99", "java.home": "/host/sdk"}) as probe,
        ):
            self.assertEqual(mp.java_home(), "/host/sdk")
            probe.assert_called_once_with("/host/sdk/bin/java")

    def test_path_runtime_is_used_without_java_home(self):
        with (
            mock.patch.dict(os.environ, {"MAVEN_PURIFIER_DEFAULT_JAVA_HOME": "/bundled/sdk"}, clear=True),
            mock.patch.object(mp.shutil, "which", return_value="/host/bin/java"),
            mock.patch.object(mp, "java_properties", return_value={"java.version": "99", "java.home": "/host/sdk"}),
        ):
            self.assertEqual(mp.java_home(), "/host/sdk")

    def test_missing_local_runtime_does_not_select_bundled_sdk(self):
        with (
            mock.patch.dict(os.environ, {"MAVEN_PURIFIER_DEFAULT_JAVA_HOME": "/bundled/sdk"}, clear=True),
            mock.patch.object(mp.shutil, "which", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                mp.java_home()


class ArtifactKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(mp.artifact_key({"artifactId": "core", "classifier": "", "extension": "jar"}), "core.jar")
        self.assertEqual(
            mp.artifact_key({"artifactId": "core", "classifier": "sources", "extension": "jar"}), "core-sources.jar"
        )


class DefinitionHash(TemporaryTree):
    def setUp(self):
        super().setUp()
        self.write("pom.xml", "<project/>")
        self.write("core/pom.xml", "<project><artifactId>core</artifactId></project>")
        self.write(".mvn/extensions.xml", "<extensions/>")
        self.write("core/src/Main.java", "class Main {}")

    def test_files(self):
        self.assertEqual(mp.definition_files(self.root), [".mvn/extensions.xml", "core/pom.xml", "pom.xml"])

    def test_skips_build_outputs(self):
        before = mp.definition_hash(self.root, ["install"])
        self.write("target/pom.xml", "<generated/>")
        self.write("core/target/classes/pom.xml", "<generated/>")
        (self.root / "result").symlink_to(self.root / "target", target_is_directory=True)
        self.write("core/src/Main.java", "class Main { int x; }")
        self.assertEqual(mp.definition_hash(self.root, ["install"]), before)

    def test_results_module_is_part_of_the_definition(self):
        before = mp.definition_hash(self.root, ["install"])
        self.write("results/pom.xml", "<project><artifactId>results</artifactId></project>")
        self.assertIn("results/pom.xml", mp.definition_files(self.root))
        self.assertNotEqual(mp.definition_hash(self.root, ["install"]), before)


    def test_changes_with_definition_and_goals(self):
        before = mp.definition_hash(self.root, ["install"])
        self.assertNotEqual(mp.definition_hash(self.root, ["clean", "install"]), before)
        self.write("core/pom.xml", "<project><artifactId>core2</artifactId></project>")
        self.assertNotEqual(mp.definition_hash(self.root, ["install"]), before)

    def test_known_value(self):
        # The text it hashes, which lib-overlays/default/definition-hash.nix builds too.
        import hashlib

        def h(text):
            return hashlib.sha256(text.encode()).hexdigest()

        text = (
            "goals clean install\n"
            f".mvn/extensions.xml {h('<extensions/>')}\n"
            f"core/pom.xml {h('<project><artifactId>core</artifactId></project>')}\n"
            f"pom.xml {h('<project/>')}\n"
        )
        self.assertEqual(mp.definition_hash(self.root, ["clean", "install"]), "sha256:" + h(text))


class CopyBuild(TemporaryTree):
    def test_keeps_result_sources_and_omits_root_nix_output_links(self):
        self.write("pom.xml", "<project/>")
        self.write("results/pom.xml", "<project/>")
        self.write("src/main/java/example/results/Result.java", "package example.results; class Result {}")
        self.write("result-data.txt", "source data")
        self.write("src/main/resources/value", "resource")
        (self.root / "src/main/resources/results").symlink_to("value")
        self.write("target/pom.xml", "<generated/>")
        (self.root / "result").symlink_to(self.root / "target", target_is_directory=True)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source"
            mp.copy_build(self.root, destination)
            self.assertTrue((destination / "results/pom.xml").is_file())
            self.assertTrue((destination / "src/main/java/example/results/Result.java").is_file())
            self.assertEqual((destination / "result-data.txt").read_text(), "source data")
            self.assertEqual((destination / "src/main/resources/results").read_text(), "resource")
            self.assertFalse((destination / "result").is_symlink())
            self.assertFalse((destination / "target").exists())


class DistributionUrl(TemporaryTree):
    def test_wrapper_properties(self):
        self.write(
            ".mvn/wrapper/maven-wrapper.properties",
            "wrapperVersion=3.3.2\ndistributionUrl=https\\://repo.maven.apache.org/maven2/org/apache/maven/"
            "apache-maven/3.9.11/apache-maven-3.9.11-bin.zip\n",
        )
        self.assertEqual(
            mp.distribution_url(self.root),
            CENTRAL + "org/apache/maven/apache-maven/3.9.11/apache-maven-3.9.11-bin.zip",
        )

    def test_default(self):
        with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_DEFAULT_MAVEN": "3.10.0"}):
            self.assertEqual(
                mp.distribution_url(self.root),
                CENTRAL + "org/apache/maven/apache-maven/3.10.0/apache-maven-3.10.0-bin.tar.gz",
            )


class DeclaredDownloads(TemporaryTree):
    def declare(self, downloads):
        self.write(".mvn/maven-purifier.json", json.dumps({"downloads": downloads}))

    def test_none(self):
        self.assertEqual(mp.declared_downloads(self.root), [])

    def test_valid(self):
        self.declare([{"url": "https://example.org/x.tgz", "path": "a/b/x.tgz"}])
        self.assertEqual(mp.declared_downloads(self.root), [{"url": "https://example.org/x.tgz", "path": "a/b/x.tgz"}])

    def test_invalid(self):
        for downloads in (
            [{"url": "https://example.org/x.tgz"}],
            [{"url": "https://example.org/x.tgz", "path": "/abs/x.tgz"}],
            [{"url": "https://example.org/x.tgz", "path": "../x.tgz"}],
        ):
            self.declare(downloads)
            with self.assertRaises(SystemExit):
                mp.declared_downloads(self.root)


class DistributionTarget(TemporaryTree):
    POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <distributionManagement>
    <repository><id>releases</id><url>https://repo.example/releases</url></repository>
    <snapshotRepository><id>snapshots</id><url>https://repo.example/snapshots</url></snapshotRepository>
  </distributionManagement>
</project>
"""

    def test_release_and_snapshot(self):
        self.write("pom.xml", self.POM)
        release = {"projects": [{"version": "1.0"}]}
        snapshot = {"projects": [{"version": "1.0-SNAPSHOT"}]}
        self.assertEqual(mp.distribution_target(self.root, release), ("releases", "https://repo.example/releases"))
        self.assertEqual(mp.distribution_target(self.root, snapshot), ("snapshots", "https://repo.example/snapshots"))

    def test_missing(self):
        self.write("pom.xml", "<project/>")
        with self.assertRaises(SystemExit):
            mp.distribution_target(self.root, {"projects": [{"version": "1.0"}]})


class ReadLock(TemporaryTree):
    def lock(self, data):
        return self.write("maven-purifier.lock.json", json.dumps(data))

    def test_valid_lock(self):
        data = {"version": mp.FORMAT_VERSION, "downloads": [], "note": "é ✓"}
        data["lockHash"] = mp.lock_hash(data)
        self.assertEqual(mp.read_lock(self.lock(data)), data)

    def test_malformed_lock(self):
        with self.assertRaises(SystemExit):
            mp.read_lock(self.write("maven-purifier.lock.json", "{ not json"))

    def test_not_an_object(self):
        with self.assertRaises(SystemExit):
            mp.read_lock(self.lock([1]))

    def test_wrong_version(self):
        data = {"version": 999}
        data["lockHash"] = mp.lock_hash(data)
        with self.assertRaises(SystemExit):
            mp.read_lock(self.lock(data))

    def test_tampered_lock(self):
        data = {"version": mp.FORMAT_VERSION, "downloads": [{"path": "a", "url": "u", "hash": "h"}]}
        data["lockHash"] = mp.lock_hash(data)
        data["downloads"][0]["hash"] = "other"
        with self.assertRaises(SystemExit):
            mp.read_lock(self.lock(data))

    def test_missing_lock(self):
        with self.assertRaises(SystemExit):
            mp.read_lock(self.root / "maven-purifier.lock.json")


class LockHash(unittest.TestCase):
    def test_canonical_json(self):
        # The text that Nix's builtins.toJSON writes for the same value.
        value = {"b": [1, None, True, "tab\there", "é ✓"], "a": {"z": "", "y": []}}
        self.assertEqual(mp.canonical_json(value), '{"a":{"y":[],"z":""},"b":[1,null,true,"tab\\there","é ✓"]}')

    def test_ignores_its_own_field(self):
        data = {"version": 2, "goals": ["install"]}
        self.assertEqual(mp.lock_hash(data), mp.lock_hash(dict(data, lockHash="anything")))


class PrefetchLines(TemporaryTree):
    def test_written_after_the_build_lines(self):
        self.write(".mvn/maven.config", "-Pfast\n-T4")
        mp.write_maven_config(self.root, ["--settings=/s.xml"])
        mp.write_maven_config(self.root, ["--settings=/t.xml"])
        text = (self.root / ".mvn/maven.config").read_text()
        self.assertEqual(text, "-Pfast\n-T4\n" + mp.PREFETCH_MARKER + "\n--settings=/t.xml\n")
        self.assertEqual(mp.without_prefetch_lines(text), "-Pfast\n-T4\n")
        self.assertEqual(mp.build_config_lines(text), ["-Pfast", "-T4"])

    def test_left_out_of_the_definition_hash(self):
        self.write("pom.xml", "<project/>")
        self.write(".mvn/maven.config", "-Pfast\n")
        before = mp.definition_hash(self.root, ["install"])
        mp.write_maven_config(self.root, ["--settings=/s.xml"])
        self.assertEqual(mp.definition_hash(self.root, ["install"]), before)

    def test_left_out_of_a_lock_run_copy(self):
        self.write("pom.xml", "<project/>")
        mp.write_maven_config(self.root, ["--settings=/s.xml"])
        mp.copy_build(self.root, self.root.parent / (self.root.name + "-copy"))
        copied = (self.root.parent / (self.root.name + "-copy") / ".mvn/maven.config").read_text()
        self.assertNotIn("--settings", copied)


class PlatformCounterparts(unittest.TestCase):
    def test_native_binaries(self):
        path = "com/google/protobuf/protoc/4.35.1/protoc-4.35.1-linux-x86_64.exe"
        self.assertEqual(
            mp.platform_counterparts(path, "x86_64-linux"),
            {
                "aarch64-linux": "com/google/protobuf/protoc/4.35.1/protoc-4.35.1-linux-aarch_64.exe",
                "aarch64-darwin": "com/google/protobuf/protoc/4.35.1/protoc-4.35.1-osx-aarch_64.exe",
            },
        )
        self.assertEqual(
            mp.platform_counterparts(path.replace("linux-x86_64", "osx-aarch_64"), "aarch64-darwin"),
            {"x86_64-linux": path, "aarch64-linux": path.replace("linux-x86_64", "linux-aarch_64")},
        )
        jar = "io/netty/netty-transport-native-epoll/4.1.0/netty-transport-native-epoll-4.1.0-linux-aarch_64.jar"
        self.assertEqual(
            mp.platform_counterparts(jar, "aarch64-linux"),
            {
                "x86_64-linux": jar.replace("linux-aarch_64", "linux-x86_64"),
                "aarch64-darwin": jar.replace("linux-aarch_64", "osx-aarch_64"),
            },
        )

    def test_other_files(self):
        self.assertEqual(mp.platform_counterparts("a/b/1/b-1.jar", "x86_64-linux"), {})
        self.assertEqual(mp.platform_counterparts("a/b/1/b-1-linux-aarch_64.jar", "x86_64-linux"), {})


class SeedMetadata(TemporaryTree):
    PREVIOUS = {
        "downloads": [
            {"path": "org/slf4j/slf4j-api/maven-metadata-central.xml", "contents": "<metadata>slf4j</metadata>"},
            {"path": "example/snaplib/1.0-SNAPSHOT/maven-metadata-snaps.xml", "contents": "<metadata>snap</metadata>"},
            {"path": "org/slf4j/slf4j-api/2.0.16/slf4j-api-2.0.16.jar", "url": "u", "hash": "h"},
        ]
    }

    def test_seeds_every_metadata_file(self):
        seeded = mp.seed_metadata(self.root, self.PREVIOUS, None)
        self.assertEqual(
            seeded,
            {"org/slf4j/slf4j-api/maven-metadata-central.xml", "example/snaplib/1.0-SNAPSHOT/maven-metadata-snaps.xml"},
        )
        self.assertEqual((self.root / "org/slf4j/slf4j-api/maven-metadata-central.xml").read_text(), "<metadata>slf4j</metadata>")
        status = (self.root / "org/slf4j/slf4j-api/resolver-status.properties").read_text()
        self.assertRegex(status, r"^maven-metadata-central\.xml\.lastUpdated=\d+\n$")
        self.assertFalse((self.root / "org/slf4j/slf4j-api/2.0.16").exists())

    def test_update_seeds_none(self):
        self.assertEqual(mp.seed_metadata(self.root, self.PREVIOUS, "all"), set())
        self.assertEqual(mp.seed_metadata(self.root, None, None), set())

    def test_update_of_named_packages(self):
        seeded = mp.seed_metadata(self.root, self.PREVIOUS, ["org.slf4j:slf4j-api"])
        self.assertEqual(seeded, {"example/snaplib/1.0-SNAPSHOT/maven-metadata-snaps.xml"})

    def test_package_names(self):
        self.assertEqual(mp.package_prefix("org.slf4j:slf4j-api"), "org/slf4j/slf4j-api/")
        for bad in ("slf4j-api", "org.slf4j:", ":a", "a:b:c"):
            with self.assertRaises(SystemExit):
                mp.package_prefix(bad)


class Options(unittest.TestCase):
    def test_lockfile_and_values(self):
        o = mp.Options(["--lockfile", "x.json", "--force", "--settings", "s.xml", "clean", "-Pa"], flags=("--force",), values=("--settings",))
        self.assertEqual(o.lockfile, Path("x.json").resolve())
        self.assertEqual(o.flags, {"--force"})
        self.assertEqual(o.values, {"--settings": "s.xml"})
        self.assertEqual(o.rest, ["clean", "-Pa"])
        with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_LOCKFILE": ""}):
            self.assertEqual(mp.Options([]).lock_path(Path("/b")), Path("/b/maven-purifier.lock.json"))

    def test_lockfile_variable(self):
        with mock.patch.dict(os.environ, {"MAVEN_PURIFIER_LOCKFILE": "/l/external.json"}):
            self.assertEqual(mp.Options([]).lock_path(Path("/b")), Path("/l/external.json"))
            self.assertEqual(mp.Options(["--lockfile", "/f/x.json"]).lock_path(Path("/b")), Path("/f/x.json"))



class Compatibility(TemporaryTree):
    def test_java_versions(self):
        for version, major in [("1.8.0_504", "8"), ("17.0.20", "17"), ("26-ea", "26")]:
            self.assertEqual(mp.java_major(version), major)

    def test_maven_versions(self):
        for version, numbers in [("2.2.1", (2,2,1)), ("3.0.5", (3,0,5)), ("4.0.0-rc-7", (4,0,0))]:
            self.assertEqual(mp.maven_version_tuple(Path("apache-maven-" + version) / "bin/mvn"), numbers)

    def test_prefetch_does_not_change_absent_config_definition(self):
        self.write("pom.xml", "<project/>")
        before = mp.definition_hash(self.root, ["install"])
        mp.write_maven_config(self.root, ["--settings=/locked/settings.xml"])
        self.assertEqual(mp.definition_hash(self.root, ["install"]), before)

    def test_legacy_snapshot_origins(self):
        rel = "example/tool/1-SNAPSHOT/tool-1-SNAPSHOT.jar"
        self.write("example/tool/1-SNAPSHOT/maven-metadata-central.xml", "<metadata><versioning><snapshot><timestamp>20261101.000001</timestamp><buildNumber>4</buildNumber></snapshot></versioning></metadata>")
        self.assertEqual(mp.legacy_remote_paths(rel,self.root), [rel,"example/tool/1-SNAPSHOT/tool-1-20261101.000001-4.jar"])

    def test_legacy_verification_rejects_different_remote_bytes(self):
        file = self.write("repository/example/a/1/a-1.jar", "original")
        record = self.write("events", json.dumps({"event":"repository","repository":"central","repositoryUrl":"https://example.org/"}) + "\n")
        import io
        with mock.patch.object(mp.urllib.request, "urlopen", return_value=io.BytesIO(b"different")):
            with self.assertRaises(SystemExit):
                mp.record_legacy_downloads(self.root / "repository",record)
        self.assertEqual(len(mp.read_record(record)), 1)

    def test_legacy_preserved_metadata_does_not_contact_remote(self):
        rel = "example/a/maven-metadata-central.xml"
        self.write("repository/"+rel, "<metadata/>")
        record = self.write("events", "")
        with mock.patch.object(mp.urllib.request,"urlopen",side_effect=AssertionError("network")):
            mp.record_legacy_downloads(self.root/"repository",record,[rel])
        self.assertEqual(mp.read_record(record)[0]["event"], "resolved")

    def test_legacy_central_uses_https_without_a_mirror(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            path = self.root / "settings.xml"
            mp.write_run_settings(ET.Element("settings"), path, legacy=True)
            self.assertEqual(ET.parse(path).findtext("mirrors/mirror/url"), CENTRAL.rstrip("/"))

    def test_legacy_http_central_without_trailing_slash_is_verified_over_https(self):
        self.write("repository/example/a/1/a-1.jar", "original")
        record = self.write("events", json.dumps({"event":"repository","repository":"central","repositoryUrl":"http://repo1.maven.org/maven2"}) + "\n")
        import io
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(mp.urllib.request,"urlopen",return_value=io.BytesIO(b"original")) as fetch:
                mp.record_legacy_downloads(self.root/"repository",record)
                self.assertEqual(fetch.call_args.args[0], CENTRAL+"example/a/1/a-1.jar")
        self.assertEqual(mp.read_record(record)[-1]["event"],"download")


class JavadocResourcesTests(TemporaryTree):
    def event(self, options, project=".", execution="default-cli"):
        return {"event": "javadoc", "project": project, "execution": execution, "options": options}

    def test_only_executed_javadoc_contributes_snapshots(self):
        with mock.patch.object(mp.javadoc_resources, "metadata") as fetch:
            self.assertEqual(mp.javadoc_resources.capture([{"event": "install"}], self.root), [])
            fetch.assert_not_called()

    def test_legacy_package_list_survives_an_html_element_endpoint(self):
        import io
        responses = [io.BytesIO(b"<!DOCTYPE html><html>not found</html>"), io.BytesIO(b"example.api\n")]
        with mock.patch.object(mp.javadoc_resources.urllib.request, "urlopen", side_effect=responses):
            self.assertEqual(mp.javadoc_resources.metadata("https://docs.example/api"), [
                ("package-list", "https://docs.example/api/package-list", "example.api\n")])

    def test_snapshots_are_shared_but_execution_scopes_remain_separate(self):
        events = [self.event("-link\n'https://docs.example/api/'", "a", "docs"),
                  self.event("-link\n'https://docs.example/api'", "b", "test-docs")]
        with mock.patch.object(mp.javadoc_resources, "metadata", return_value=[
            ("element-list", "https://docs.example/api/element-list", "module:example\nexample.api\n")
        ]) as fetch:
            entries = mp.javadoc_resources.capture(events, self.root)
        self.assertEqual(fetch.call_count, 1)
        document = mp.javadoc_resources.index(entries)
        self.assertEqual([(p.get("id"), p.find("execution").get("id")) for p in document],
                         [("a", "docs"), ("b", "test-docs")])
        self.assertEqual(len(entries), 2)

    def test_relock_reuses_snapshots_without_network(self):
        event = self.event("-link\n'https://docs.example/api'")
        with mock.patch.object(mp.javadoc_resources, "metadata", return_value=[
            ("package-list", "https://docs.example/api/package-list", "example.api\n")
        ]):
            original = mp.javadoc_resources.capture([event], self.root)
        previous = {"downloads": original}
        mp.javadoc_resources.seed(previous, self.root)
        link = mp.javadoc_resources.index(original).find("project/execution/link")
        replay = self.event("-linkoffline\n'https://docs.example/api' '" + str(self.root / link.get("directory")) + "'")
        with mock.patch.object(mp.javadoc_resources, "metadata", side_effect=AssertionError("unexpected network")):
            self.assertEqual(mp.javadoc_resources.capture([replay], self.root, previous), original)

    def test_platform_url_comes_from_actual_doclet_output(self):
        self.write("docs/api.html", '<a class="external-link" href="https://custom.example/sdk/api/java.base/java/lang/Object.html">Object</a>')
        self.assertEqual(mp.javadoc_resources.platform_links(self.root / "docs"), {"https://custom.example/sdk/api"})
        self.write("docs/api.html", '<a href="https://custom.example/sdk/api/java/lang/Object.html">ordinary manual link</a>')
        self.assertEqual(mp.javadoc_resources.platform_links(self.root / "docs"), set())

    def test_bundled_platform_metadata_is_not_fetched_again(self):
        self.write("docs/api.html", '<a class="external-link" href="https://java.example/api/java/lang/Object.html">Object</a>')
        event = self.event("-linkoffline\n'https://java.example/api' '/plugin/bundled-metadata'\n-d\n'" + str(self.root / "docs") + "'")
        with mock.patch.object(mp.javadoc_resources, "metadata", side_effect=AssertionError("unexpected network")):
            self.assertEqual(mp.javadoc_resources.capture([event], self.root), [])

    def test_public_href_and_metadata_location_can_differ(self):
        event = self.event("-linkoffline\n'https://public.example/api' 'https://cache.example/versioned'")
        with mock.patch.object(mp.javadoc_resources, "metadata", return_value=[
            ("package-list", "https://cache.example/versioned/package-list", "example.api\n")
        ]):
            entries = mp.javadoc_resources.capture([event], self.root)
        link = mp.javadoc_resources.index(entries).find("project/execution/link")
        self.assertEqual(link.get("url"), "https://public.example/api")
        self.assertEqual(link.get("metadata"), "https://cache.example/versioned")


class PrimaryPublications(TemporaryTree):
    def test_invoker_staging_copy_is_not_an_additional_publication(self):
        repo = self.root / "repository"
        main = {"event": "install", "file": str(repo / "example/plugin/1/plugin-1.jar")}
        staged = {"event": "install", "file": str(self.root / "project/target/local-repo/example/plugin/1/plugin-1.jar")}
        downloaded = {"event": "download", "file": main["file"]}
        events = [staged, downloaded, main]
        self.assertEqual(mp.primary_installs(events, repo), [main])
        self.assertEqual(events, [staged, downloaded, main])

    def test_repository_boundary_and_parent_components_are_respected(self):
        repo = self.root / "repository"
        events = [
            {"event": "install", "file": str(self.root / "repository-other/a.jar")},
            {"event": "install", "file": str(repo / "../secondary/a.jar")},
            {"event": "install", "file": str(repo / "group/a.jar")},
            {"event": "install", "file": str(self.root / "other/../repository/group/b.jar")},
        ]
        self.assertEqual(mp.primary_installs(events, repo), [events[2], {"event": "install", "file": str(repo / "group/b.jar")}])


class DownloadSnapshots(TemporaryTree):
    def transfer(self, repository, contents, origin="https://origin.example/"):
        relative = "example/input/1/input-1.jar"
        file = self.write(repository + "/" + relative, contents)
        snapshot = self.write("record.jsonl.downloads/" + repository.replace("/", "-") + ".bin", contents)
        return {"event": "download", "file": str(file), "localRepository": str(self.root / repository),
                "snapshot": str(snapshot), "repository": "shared-id", "repositoryUrl": origin,
                "url": origin + relative}

    def test_conflicting_child_repository_inputs_cannot_share_one_lock_path(self):
        first = self.transfer("child-a", "first input")
        second = self.transfer("child-b", "different input")
        for events in [[first, second], [second, first]]:
            with self.assertRaises(SystemExit):
                mp.captured_downloads(events, self.root / "primary", self.root / "record.jsonl", set(), 2)

    def test_identical_child_inputs_have_order_independent_origin_and_build_scope(self):
        first = self.transfer("child-a", "same input", "https://z.example/")
        second = self.transfer("child-b", "same input", "https://a.example/")
        for events in [[first, second], [second, first]]:
            downloads, sources, development, origins = mp.captured_downloads(
                events, self.root / "primary", self.root / "record.jsonl", set(), 1)
            relative = "example/input/1/input-1.jar"
            self.assertEqual(downloads[relative], second["url"])
            self.assertEqual(sources[relative].read_text(), "same input")
            self.assertEqual(development, set())
            self.assertEqual(origins, {})

    def test_child_repository_id_does_not_supply_primary_bootstrap_origin(self):
        child = self.transfer("child", "same input", "https://child.example/")
        parent = self.transfer("primary", "same input", "https://parent.example/")
        *_, origins = mp.captured_downloads(
            [child, parent], self.root / "primary", self.root / "record.jsonl", set(), 2)
        self.assertEqual(origins, {"shared-id": "https://parent.example/"})

    def test_child_input_cannot_replace_a_different_nix_provider(self):
        child = self.transfer("child", "different child bytes")
        relative = "example/input/1/input-1.jar"
        self.write("primary/" + relative, "Nix-provided bytes")
        with self.assertRaises(SystemExit):
            mp.captured_downloads([child], self.root / "primary", self.root / "record.jsonl",
                                 {relative: self.root / "primary" / relative}, 1)

    def test_provider_identity_uses_immutable_source_after_reactor_overwrite(self):
        child = self.transfer("child", "Nix-provided bytes")
        relative = "example/input/1/input-1.jar"
        provider = self.write("provider/" + relative, "Nix-provided bytes")
        self.write("primary/" + relative, "locally installed output")
        downloads, *_ = mp.captured_downloads(
            [child], self.root / "primary", self.root / "record.jsonl", {relative: provider}, 1)
        self.assertEqual(downloads, {})

    def test_child_file_is_relative_to_its_own_resolver_repository(self):
        primary = self.root / "primary"
        child = self.root / "project/target/child-repository"
        relative = "example/input/1/input-1.jar"
        event = {"file": str(child / relative), "localRepository": str(child)}
        self.assertEqual(mp.repository_path(event, primary), relative)
        self.assertEqual(mp.repository_path({"file": str(primary / relative)}, primary), relative)

    def test_event_repository_cannot_claim_an_outside_file(self):
        primary = self.root / "primary"
        for file in [str(primary / "../foreign/input.jar"), "relative/input.jar"]:
            with self.assertRaises(SystemExit):
                mp.repository_path({"file": file, "localRepository": str(primary)}, primary)

    def test_child_records_keep_build_prefix_when_development_is_added(self):
        record = self.write("record.jsonl", '{"event":"parent"}\n')
        self.write("record.jsonl.parts/build/record-b.jsonl", '{"event":"child-b"}\n')
        self.write("record.jsonl.parts/build/record-a.jsonl", '{"event":"child-a"}\n')
        build = mp.read_record(record)
        self.write("record.jsonl.parts/development/record-a.jsonl", '{"event":"development"}\n')
        self.assertEqual(mp.read_record(record), build + [{"event": "development"}])
        self.assertEqual([e["event"] for e in build], ["parent", "child-a", "child-b"])

    def test_child_snapshot_error_rejects_capture(self):
        record = self.write("record.jsonl", "")
        self.write("record.jsonl.parts/build/record-child.jsonl",
                   '{"event":"download-error","url":"https://example.invalid/a.jar","message":"copy failed"}\n')
        with self.assertRaises(SystemExit):
            mp.read_record(record)

    def test_observed_bytes_survive_repository_install(self):
        record = self.root / "record.jsonl"
        downloaded = self.write("record.jsonl.downloads/native.bin", "published bytes")
        final = self.write("repository/example/same/1/same-1-tests.jar", "locally installed bytes")
        event = {"event": "download", "file": str(final), "snapshot": str(downloaded), "url": "https://origin.example/same.jar"}
        source = mp.download_source(event, record)
        self.assertEqual(source.read_text(), "published bytes")
        self.assertNotEqual(mp.sri(source), mp.sri(final))

    def test_legacy_verified_file_stays_supported(self):
        source = self.write("legacy.jar", "verified legacy bytes")
        self.assertEqual(mp.download_source({"file": str(source)}, self.root / "record.jsonl"), source)

    def test_missing_or_foreign_snapshot_fails_closed(self):
        record = self.root / "record.jsonl"
        for source in [self.root / "record.jsonl.downloads/missing", self.write("foreign.bin", "foreign")]:
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                mp.download_source({"file": str(source), "snapshot": str(source), "url": "https://origin.example/a"}, record)

    def test_failed_snapshot_cannot_silently_drop_the_download(self):
        record = self.write("record.jsonl", json.dumps({"event": "download-error", "url": "https://origin.example/a", "message": "copy failed"}) + "\n")
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            mp.read_record(record)


if __name__ == "__main__":
    unittest.main()
