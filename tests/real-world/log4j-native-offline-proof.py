#!/usr/bin/env python3
"""Review-only next hosted proof: fetch validated inputs, then native Maven offline."""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

ARTIFACT = 13963552297
ARTIFACT_SHA = "9b5ead609bcf53c99ec6ec580ddca7e704baade5e4bf4e703ee9923c96f50aec"
LOCK_SHA = "5e3285caf773a8e232499367ed790a87538ef99225b837c3eda81972a4056386"
NATIVE_HEAD = "75c7886b551c9b7c1d0a5c8a5c501b22774833fc"
NATIVE_SCRIPT_SHA = "1b4b98f588e049c65d5c2fbcec2a3463a74b9656a3e5ed23ee90eea425e55027"
SOURCE_REV = "28117e55f69892d3454672f9399e53fb7b74a321"
PURIFIER_REV = "301ee83db0a6ddde7d4ea4f385fa8d33ee4af5be"
PURIFIER_NAR = "sha256-rB8d3s58bog0Q436qLdx9+oe3wrgNjjiUtB8+xbYluM="
GOALS = ["-DtrimStackTrace=false", "-DinstallAtEnd=true", "clean", "install"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch(root):
    """Only this separate process/step receives the read-only artifact token."""
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    endpoint = f"repos/nix-purifiers/maven-purifier/actions/artifacts/{ARTIFACT}"
    metadata = json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, timeout=30))
    assert metadata["id"] == ARTIFACT and not metadata["expired"]
    assert metadata["workflow_run"]["id"] == 43257513258
    assert metadata["workflow_run"]["head_sha"] == NATIVE_HEAD
    assert metadata["digest"] == "sha256:" + ARTIFACT_SHA
    assert metadata["size_in_bytes"] < 10 * 1024**2
    (root / "artifact-api.json").write_text(json.dumps(metadata, indent=2) + "\n")
    archive = root / "native-artifact.zip"
    with archive.open("wb") as output:
        subprocess.run(["gh", "api", "--allow-escape-sequences", endpoint + "/zip"],
                       stdout=output, check=True, timeout=60)
    assert sha(archive) == ARTIFACT_SHA
    with zipfile.ZipFile(archive) as z:
        assert sum(x.file_size for x in z.infolist()) < 30 * 1024**2
        for entry in z.infolist():
            p = Path(entry.filename)
            assert not p.is_absolute() and ".." not in p.parts
            if entry.is_dir():
                continue
            target = root / "native-input" / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(entry))
    validated_bindings(archive, metadata, root / "native-input")
    (root / "fetch-receipt.json").write_text(json.dumps({"artifact": ARTIFACT,
        "archiveSha256": ARTIFACT_SHA, "lockSha256": LOCK_SHA,
        "sourceHead": NATIVE_HEAD, "runtimeExecuted": False}, indent=2) + "\n")



def validated_bindings(archive, metadata, unpacked):
    assert metadata["id"] == ARTIFACT and metadata["workflow_run"]["id"] == 43257513258
    assert metadata["workflow_run"]["head_sha"] == NATIVE_HEAD
    assert metadata["digest"] == "sha256:" + ARTIFACT_SHA and sha(archive) == ARTIFACT_SHA
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            p = Path(entry.filename)
            assert not p.is_absolute() and ".." not in p.parts
            if not entry.is_dir():
                assert sha(unpacked / p) == hashlib.sha256(z.read(entry)).hexdigest()
    evidence = unpacked / "log4j-native-evidence"
    assert sha(evidence / "maven-purifier.lock.json") == LOCK_SHA
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert receipt["passed"] and receipt["revision"] == SOURCE_REV
    assert receipt["purifierRevision"] == PURIFIER_REV and receipt["purifierSource"]["narHash"] == PURIFIER_NAR
    assert receipt["scriptSha256"] == NATIVE_SCRIPT_SHA and receipt["lockSha256"] == LOCK_SHA
    assert receipt["lockJavaMajor"] == "17" and receipt["goals"] == GOALS
    assert {i["reference"] for i in receipt["images"]} == {"elasticsearch:8.15.3", "logstash:8.15.3", "mongo:latest"}
    return receipt


def product_definition_functions(purifier):
    """Use the admitted product's actual pure freshness helpers, not a rewrite."""
    tree = ast.parse((purifier / "cli/maven_purifier.py").read_text())
    functions = {"build_config_lines", "definition_files", "definition_hash"}
    constants = {"STATE_DIR", "PREFETCH_MARKER", "SKIPPED_DIRS"}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions
            or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in n.targets)]
    namespace = {"os": os, "Path": Path, "hashlib": hashlib}
    exec(compile(ast.Module(body=body, type_ignores=[]), "admitted-product-definition-helpers", "exec"), namespace)
    return namespace


class ProofInterrupted(Exception):
    pass


class Proof:
    def __init__(self, root):
        self.root = root
        self.env = dict(os.environ)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "MAVEN_OPTS", "DOCKER_HOST", "DOCKER_CONTEXT"):
            self.env.pop(key, None)
        for name in ("home", "jvm-home", "tmp", "state", "cache"):
            (root / name).mkdir(exist_ok=False)
        self.env.update(CI="true", HOME=str(root / "home"), TMPDIR=str(root / "tmp"),
                        XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                        JAVA_TOOL_OPTIONS="-Duser.home=" + str(root / "jvm-home"))
        self.receipt = {"passed": False, "scope": "native Maven offline replay; cached real services",
                        "scriptSha256": sha(__file__), "stages": [],
                        "kernelNetworkDenial": False, "siteOrPureCredit": False}
        self.events = None
        self.event_file = None
        self.active = {}
        self.cleanup_results = []
        self.cleaning = False
        self.interrupted = None
        self.old_handlers = {}

    def install_handlers(self):
        def interrupted(number, frame):
            self.interrupted = number
            self.receipt["interruptedBySignal"] = number
            self.receipt["passed"] = False
            if not self.cleaning:
                raise ProofInterrupted("supervisor interrupted by signal " + str(number))
        for number in (signal.SIGTERM, signal.SIGINT):
            self.old_handlers[number] = signal.signal(number, interrupted)

    def restore_handlers(self):
        for number, handler in self.old_handlers.items():
            signal.signal(number, handler)

    def spawn(self, command, **kwargs):
        # Register a detached child before delivering TERM/INT to this process.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            # This proof process is single-threaded. Restore the inherited mask
            # in the child before exec, while the parent stays blocked until
            # ownership registration is complete.
            child = subprocess.Popen(command, env=self.env, start_new_session=True,
                                     preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous),
                                     **kwargs)
            self.active[child.pid] = child
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        return child

    def live_group(self, group):
        members = []
        for p in Path("/proc").iterdir():
            if not p.name.isdecimal():
                continue
            try:
                fields = (p / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    members.append(int(p.name))
            except (OSError, ValueError, IndexError):
                continue
        return members

    def stop_group(self, child):
        previous_cleaning = self.cleaning
        self.cleaning = True
        result = {"group": child.pid, "signals": []}
        try:
            # The child is not reaped until both group signals and final scan.
            for number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, number)
                    result["signals"].append(number)
                except ProcessLookupError:
                    pass
                if number == signal.SIGTERM:
                    time.sleep(0.25)
            time.sleep(0.05)
            result["liveMembersBeforeReap"] = self.live_group(child.pid)
            try:
                result["exitCode"] = child.wait(timeout=1)
                self.active.pop(child.pid, None)
            except subprocess.TimeoutExpired:
                result["unreaped"] = True
                self.receipt["passed"] = False
            self.cleanup_results.append(result)
            return result.get("exitCode")
        finally:
            self.cleaning = previous_cleaning

    def stop_all(self):
        self.cleaning = True
        for child in list(self.active.values()):
            self.stop_group(child)
        self.receipt["ownedGroupCleanup"] = self.cleanup_results
        self.receipt["unreapedOwnedLeaders"] = list(self.active)
        if self.active or any(x.get("liveMembersBeforeReap") for x in self.cleanup_results):
            self.receipt["cleanupIncomplete"] = True
            self.receipt["passed"] = False

    def save(self):
        pending = self.root / "offline-receipt.json.tmp"
        pending.write_text(json.dumps(self.receipt, indent=2) + "\n")
        pending.replace(self.root / "offline-receipt.json")

    def run(self, name, command, cwd=None, limit=120, required=True):
        stage = {"name": name, "command": command, "timeoutSeconds": limit, "timedOut": False}
        self.receipt["stages"].append(stage)
        self.save()
        path = self.root / (name + ".log")
        start = time.monotonic()
        try:
            with path.open("wb") as output:
                child = None
                try:
                    child = self.spawn(command, cwd=cwd or self.root,
                                       stdout=output, stderr=subprocess.STDOUT)
                    while time.monotonic() - start < limit:
                        if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                            break
                        time.sleep(0.1)
                    else:
                        stage["timedOut"] = True
                finally:
                    if child is not None:
                        stage["exitCode"] = self.stop_group(child)
                    stage["interrupted"] = self.interrupted is not None
        finally:
            stage.update(seconds=time.monotonic() - start)
            if path.exists():
                stage["logSha256"] = sha(path)
            self.save()
        if required and (stage.get("timedOut") or stage.get("interrupted") or stage.get("exitCode") != 0):
            raise RuntimeError(name + " failed")
        return path.read_text(errors="replace")

    def images(self, bindings, label):
        images = json.loads(self.run(label, ["docker", "image", "inspect", *[x["reference"] for x in bindings]]))
        assert len(images) == len(bindings)
        for actual, admitted in zip(images, bindings):
            assert actual["Id"] == admitted["id"]
            assert actual["Os"] == "linux" and actual["Architecture"] == "amd64"
            assert admitted["reference"] in actual.get("RepoTags", [])
            expected_digest = admitted["repoDigests"][0].split("@", 1)[1]
            assert any(x.endswith("@" + expected_digest) for x in actual.get("RepoDigests", []))
        return [{"reference": x["reference"], "id": i["Id"], "repoDigests": i["RepoDigests"]}
                for i, x in zip(images, bindings)]

    def start_events(self):
        self.event_file = (self.root / "offline-image-events.jsonl").open("wb")
        self.events = self.spawn(["docker", "events", "--filter", "type=image", "--format", "{{json .}}"],
                                 stdout=self.event_file, stderr=subprocess.STDOUT)
        time.sleep(1)
        assert os.waitid(os.P_PID, self.events.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None

    def stop_events(self):
        if self.events is None:
            if self.event_file is not None:
                self.event_file.close()
                self.event_file = None
            return
        self.receipt["eventCollectorExit"] = self.stop_group(self.events)
        self.event_file.close()
        self.events = None
        self.event_file = None


def prove(cli, root):
    proof = Proof(root)
    proof.install_handlers()
    try:
        original = root / "native-input/log4j-native-evidence"
        old = validated_bindings(root / "native-artifact.zip", json.loads((root / "artifact-api.json").read_text()), root / "native-input")
        assert sha(original / "maven-purifier.lock.json") == LOCK_SHA
        lock = root / "maven-purifier.lock.json"
        lock.write_bytes((original / "maven-purifier.lock.json").read_bytes())
        data = json.loads(lock.read_text())
        assert data["javaMajor"] == "17" and data["goals"] == GOALS and data["settings"] is None
        harness = Path(os.environ["GITHUB_WORKSPACE"])
        purifier = Path(os.environ["PURIFIER_SOURCE"])
        proof.receipt["harnessRevision"] = proof.run("harness-revision", ["git", "rev-parse", "HEAD"], harness).strip()
        assert proof.receipt["harnessRevision"] == os.environ["GITHUB_SHA"]
        proof.run("harness-clean", ["git", "diff", "--exit-code", "HEAD", "--"], harness)
        assert proof.run("purifier-revision", ["git", "rev-parse", "HEAD"], purifier).strip() == PURIFIER_REV
        proof.run("purifier-clean-before", ["git", "diff", "--exit-code", "HEAD", "--"], purifier)
        metadata = json.loads((Path(os.environ["RUNNER_TEMP"]) / "log4j-source.json").read_text())
        builds = json.loads((Path(os.environ["RUNNER_TEMP"]) / "log4j-build.json").read_text())
        assert metadata["revision"] == PURIFIER_REV and metadata["locked"]["narHash"] == PURIFIER_NAR
        assert len(builds) == 1 and builds[0] == old["cliBuild"]
        assert str(cli.parent.parent) in builds[0]["outputs"].values() and sha(cli) == old["cliSha256"]
        proof.receipt["purifierSourceMetadata"] = {"revision": PURIFIER_REV, "narHash": PURIFIER_NAR}
        proof.receipt["cliBuild"] = builds[0]
        proof.run("cli-derivation", ["nix", "--offline", "derivation", "show", builds[0]["drvPath"]])
        current_nar = json.loads(proof.run("cli-nar", ["nix", "--offline", "path-info", "--json", "--json-format", "1", str(cli.parent.parent)]))
        old_nar_log = (original / "cli-nar.log").read_text()
        admitted_nar = json.loads(old_nar_log[old_nar_log.index("{"):])
        output_path = str(cli.parent.parent)
        assert current_nar[output_path]["narHash"] == admitted_nar[output_path]["narHash"]
        assert current_nar[output_path]["deriver"] == builds[0]["drvPath"]
        proof.receipt["cliNarHash"] = current_nar[output_path]["narHash"]
        current_java = proof.run("java-version", [str(Path(proof.env["JAVA_HOME"]) / "bin/java"), "-version"])
        version = lambda s: re.search(r'openjdk version "([^"]+)"', s).group(1)
        build = lambda s: next(x for x in s.splitlines() if x.startswith("OpenJDK Runtime Environment"))
        assert version(current_java) == version(old["javaVersion"])
        assert build(current_java) == build(old["javaVersion"])
        proof.receipt["runtimeVersion"] = version(current_java)
        proof.receipt["runtimeJoin"] = "same observed vendor/version/build; original Java binary hash not captured"
        proof.receipt["currentJavaSha256"] = sha(Path(proof.env["JAVA_HOME"]) / "bin/java")
        proof.run("docker-version", ["docker", "version", "--format", "{{json .}}"])
        assert not proof.run("containers-before", ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Status}}"]).strip()
        for i, binding in enumerate(old["images"]):
            assert binding["platform"] == "linux/amd64" and len(binding["repoDigests"]) == 1
            digest_ref = binding["repoDigests"][0]
            proof.run("prepare-image-" + str(i), ["docker", "pull", "--platform=linux/amd64", digest_ref], limit=100)
            proof.run("tag-image-" + str(i), ["docker", "image", "tag", digest_ref, binding["reference"]])
        proof.receipt["imagesBefore"] = proof.images(old["images"], "images-before")
        proof.run("source-init", ["git", "init", str(root / "source")])
        source = root / "source"
        proof.run("source-fetch", ["git", "-c", "protocol.version=2", "fetch", "--depth", "1",
                                   "https://github.com/apache/logging-log4j2.git", SOURCE_REV], source)
        proof.run("source-checkout", ["git", "checkout", "--detach", "FETCH_HEAD"], source)
        assert proof.run("source-revision", ["git", "rev-parse", "HEAD"], source).strip() == SOURCE_REV
        proof.run("source-submodules", ["git", "submodule", "update", "--init", "--recursive"], source)
        helpers = product_definition_functions(purifier)
        for key in ("definitionBefore",):
            proof.receipt[key] = helpers["definition_hash"](source, GOALS)
            assert proof.receipt[key] == data["definitionHash"]
        poms = {rel: sha(source / rel) for rel in helpers["definition_files"](source) if Path(rel).name == "pom.xml"}
        proof.run("prefetch", [str(cli), "prefetch", "--lockfile", str(lock)], source, limit=600)
        assert sha(lock) == LOCK_SHA
        assert helpers["definition_hash"](source, GOALS) == data["definitionHash"]
        launcher = source / ".maven-purifier/bin/mvn"
        assert "-o --settings" in launcher.read_text()
        settings = ET.parse(source / ".maven-purifier/settings.xml").getroot()
        assert next(x.text for x in settings if x.tag.split("}")[-1] == "offline") == "true"
        proof.receipt["launcherSha256"] = sha(launcher)
        (root / "offline-launcher.sh").write_bytes(launcher.read_bytes())
        (root / "offline-settings.xml").write_bytes((source / ".maven-purifier/settings.xml").read_bytes())
        (root / "source-inputs.json").write_text(json.dumps(poms, indent=2) + "\n")
        links = list((root / "state/maven-purifier/projects").glob("*/prefetch"))
        assert len(links) == 1 and links[0].is_symlink()
        assert links[0].parent.name == hashlib.sha256(os.fsencode(str(source.resolve()))).hexdigest()
        prefetched = links[0].resolve(strict=True)
        repository_store = (source / ".maven-purifier/repository.source").read_text()
        assert repository_store == str((prefetched / "repository").resolve())
        proof.receipt["repositoryStorePath"] = repository_store
        proof.receipt["prefetchedInputs"] = str(prefetched)
        prefetched_info = json.loads(proof.run("prefetch-nar", ["nix", "--offline", "path-info", "--json", "--json-format", "1", str(prefetched)]))
        assert prefetched_info[str(prefetched)] is not None
        proof.receipt["prefetchNarHash"] = prefetched_info[str(prefetched)]["narHash"]
        proof.run("prefetch-deriver", ["nix-store", "--query", "--deriver", str(prefetched)])
        maven_version = proof.run("maven-version", [str(launcher), "--version"], source)
        assert "Apache Maven " + data["maven"]["version"] in maven_version
        assert re.search(r"Java version: 17[.,]", maven_version)
        proof.start_events()
        output = proof.run("native-offline-install", [str(launcher), *GOALS], source, limit=1200)
        assert os.waitid(os.P_PID, proof.events.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
        proof.receipt["eventCollectorAliveAtReplayEnd"] = True
        proof.stop_events()
        assert sha(lock) == LOCK_SHA
        proof.receipt["definitionAfter"] = helpers["definition_hash"](source, GOALS)
        assert proof.receipt["definitionAfter"] == data["definitionHash"]
        assert {rel: sha(source / rel) for rel in helpers["definition_files"](source) if Path(rel).name == "pom.xml"} == poms
        proof.run("source-tracked-unchanged", ["git", "diff", "--exit-code", "HEAD", "--", ".", ":!.mvn/maven.config"], source)
        script = harness / "tests/real-world/log4j-native-proof.py"
        assert sha(script) == NATIVE_SCRIPT_SHA
        tree = ast.parse(script.read_text())
        expected = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "EXPECTED_REACTOR" for t in n.targets))
        summary = output.rsplit("Reactor Summary", 1)[-1]
        assert len(expected) == 31
        for module in expected:
            assert re.search(r"(?m)^\[INFO\] " + re.escape(module["name"]) + r"[. ]+SUCCESS \[", summary)
        assert "BUILD SUCCESS" in summary
        suites = re.findall(r"Tests run:\s*(\d+), Failures:\s*(\d+), Errors:\s*(\d+), Skipped:\s*(\d+).*?-- in ([\w.]+)", output)
        service = []
        for t, f, er, sk, name in suites:
            if name.endswith("LogstashIT") or ".mongodb." in name and name.endswith("IT"):
                assert int(t) > int(sk) and int(f) == 0 and int(er) == 0
                service.append(name)
        assert any(x.endswith("LogstashIT") for x in service) and any(x.endswith("MongoDbIT") for x in service)
        for module in ["log4j-layout-template-json-test", "log4j-mongodb"]:
            for goal in ["start", "stop"]:
                assert re.search(r"--- (?:docker|docker-maven-plugin):0\.45\.1:" + goal + r"[^\n]* @ " + module + r" ---", output)
        events = [json.loads(line) for line in (root / "offline-image-events.jsonl").read_text().splitlines() if line.strip()]
        assert not any(x.get("Action") == "pull" for x in events)
        assert not re.search(r"(?i)pulling (?:image|from)|downloaded newer image", output)
        proof.receipt["imagesAfter"] = proof.images(old["images"], "images-after")
        assert proof.receipt["imagesAfter"] == proof.receipt["imagesBefore"]
        remaining = proof.run("containers-after", ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Status}}"])
        assert not remaining.strip()
        proof.run("purifier-clean-after", ["git", "diff", "--exit-code", "HEAD", "--"], purifier)
        proof.receipt.update(passed=True, sourceRevision=SOURCE_REV, artifact=ARTIFACT,
                             lockSha256=LOCK_SHA, goals=GOALS, reactorModules=[m["artifactId"] for m in expected],
                             executedServiceSuites=service, imagePullEventsObserved=0)
    except Exception as error:
        proof.receipt["error"] = str(error)
    finally:
        proof.cleaning = True
        proof.stop_events()
        proof.stop_all()
        try:
            proof.run("cleanup-observation", ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Status}}"], required=False, limit=2)
        except Exception as error:
            proof.receipt["cleanupObservationError"] = str(error)
        proof.stop_all()
        if proof.interrupted is not None:
            proof.receipt["passed"] = False
        proof.receipt["cleanupBoundary"] = "Fabric8 stop hooks; owned event collector stopped; no global prune; disposable runner destruction if test failure leaves objects"
        proof.receipt["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        proof.save()
        proof.restore_handlers()
    return 0 if proof.receipt["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "fetch":
        fetch(Path(sys.argv[2]).resolve())
    elif len(sys.argv) == 4 and sys.argv[1] == "run":
        raise SystemExit(prove(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()))
    else:
        raise SystemExit("fetch <new-evidence-dir> | run <built-cli> <evidence-dir>")
