#!/usr/bin/env python3
"""Draft hosted native-capture proof. Does real network/test work IF authorized/run."""
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

REVISION = "28117e55f69892d3454672f9399e53fb7b74a321"
PURIFIER_REVISION = "301ee83db0a6ddde7d4ea4f385fa8d33ee4af5be"
PURIFIER_NAR = "sha256-rB8d3s58bog0Q436qLdx9+oe3wrgNjjiUtB8+xbYluM="
IMAGES = ["elasticsearch:8.15.3", "logstash:8.15.3", "mongo:latest"]
GOALS = ["-DtrimStackTrace=false", "-DinstallAtEnd=true", "clean", "install"]
EXPECTED_REACTOR = [
  {
    "artifactId": "log4j-bom",
    "name": "Apache Log4j BOM"
  },
  {
    "artifactId": "log4j",
    "name": "Apache Log4j Parent"
  },
  {
    "artifactId": "log4j-async-logger",
    "name": "Apache Log4j Async Logger"
  },
  {
    "artifactId": "log4j-compress",
    "name": "Apache Log4j Core: Extended Compression Support"
  },
  {
    "artifactId": "log4j-config-jackson",
    "name": "Log4j Core Jackson configuration"
  },
  {
    "artifactId": "log4j-config-properties",
    "name": "Log4j Core properties configuration"
  },
  {
    "artifactId": "log4j-config-yaml",
    "name": "Log4j Core YAML configuration"
  },
  {
    "artifactId": "log4j-conversant",
    "name": "Apache Log4j Conversant Disruptor-based supplements"
  },
  {
    "artifactId": "log4j-core",
    "name": "Apache Log4j Core"
  },
  {
    "artifactId": "log4j-core-test",
    "name": "Apache Log4j Core Tests"
  },
  {
    "artifactId": "log4j-csv",
    "name": "Apache Log4j CSV"
  },
  {
    "artifactId": "log4j-docker",
    "name": "Apache Log4j Docker Library"
  },
  {
    "artifactId": "log4j-gc-test",
    "name": "Apache Log4j garbage collector tests"
  },
  {
    "artifactId": "log4j-jctools",
    "name": "Apache Log4j JCTools-based supplements"
  },
  {
    "artifactId": "log4j-jdbc",
    "name": "Apache Log4j JDBC"
  },
  {
    "artifactId": "log4j-jdbc-dbcp2",
    "name": "Apache Log4j JDBC: DBCP 2 datasource"
  },
  {
    "artifactId": "log4j-jdbc-jndi",
    "name": "Apache Log4j JDBC: JNDI datasource"
  },
  {
    "artifactId": "log4j-jndi",
    "name": "Apache Log4j JNDI Support"
  },
  {
    "artifactId": "log4j-jndi-test",
    "name": "Apache Log4j JNDI Test Support"
  },
  {
    "artifactId": "log4j-jul",
    "name": "Apache Log4j JUL Handler"
  },
  {
    "artifactId": "log4j-kit",
    "name": "Apache Log4j Kit"
  },
  {
    "artifactId": "log4j-layout-template-json",
    "name": "Apache Log4j layout for templated JSON encoding"
  },
  {
    "artifactId": "log4j-layout-template-json-test",
    "name": "Apache Log4j JSON Template Layout tests"
  },
  {
    "artifactId": "log4j-mongodb",
    "name": "Apache Log4j MongoDB Appender"
  },
  {
    "artifactId": "log4j-osgi-test",
    "name": "Apache Log4j OSGi tests"
  },
  {
    "artifactId": "log4j-perf-test",
    "name": "Apache Log4J Performance Tests"
  },
  {
    "artifactId": "log4j-plugin-processor",
    "name": "Apache Log4j Plugin Processor"
  },
  {
    "artifactId": "log4j-plugins",
    "name": "Apache Log4j Plugins"
  },
  {
    "artifactId": "log4j-plugins-test",
    "name": "Apache Log4j Plugins Test"
  },
  {
    "artifactId": "log4j-script",
    "name": "Apache Log4j Script Support"
  },
  {
    "artifactId": "log4j-spring-cloud-config-client",
    "name": "Apache Log4j Spring Cloud Config Client Support"
  }
]


def main():
    if len(sys.argv) != 3:
        raise SystemExit("native-proof.py <built-cli> <new-owned-evidence-dir>")
    cli, root = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not cli.is_file() or root.exists():
        raise SystemExit("need existing CLI and fresh evidence directory")
    root.mkdir(mode=0o700, parents=True)
    for p in ["home", "jvm-home", "tmp"]:
        (root / p).mkdir()
    env = dict(os.environ)
    env.update(CI="true", HOME=str(root / "home"), TMPDIR=str(root / "tmp"),
               JAVA_TOOL_OPTIONS="-Duser.home=" + str(root / "jvm-home"))
    for key in ("_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "MAVEN_OPTS", "DOCKER_HOST", "DOCKER_CONTEXT"):
        env.pop(key, None)
    # Use only the already exposed default daemon: never start/reconfigure it.
    receipt = {"revision": REVISION, "goals": GOALS, "CI": "true", "stages": [],
               "proof": "native-only", "passed": False,
               "scriptSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def run(name, command, cwd=root, limit=120, required=True):
        stage = {"name": name, "command": command, "timeoutSeconds": limit}
        receipt["stages"].append(stage)
        start = time.monotonic()
        with (root / (name + ".log")).open("wb") as log:
            child = subprocess.Popen(command, env=env, cwd=cwd, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            try:
                result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                while result is None and time.monotonic() - start < limit:
                    time.sleep(0.1)
                    result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                stage["timedOut"] = result is None
            finally:
                # Leader remains unreaped while its owned group is terminated.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(child.pid, sig)
                    except ProcessLookupError:
                        pass
                    if sig == signal.SIGTERM:
                        time.sleep(1)
                stage["exitCode"] = child.wait()
        stage["seconds"] = time.monotonic() - start
        data = (root / (name + ".log")).read_bytes()
        stage["logSha256"] = hashlib.sha256(data).hexdigest()
        if required and (stage.get("timedOut") or stage["exitCode"]):
            raise RuntimeError(name + " failed; inspect its log")
        return data.decode(errors="replace")

    try:
        harness = Path(os.environ["GITHUB_WORKSPACE"])
        receipt["harnessRevision"] = run("harness-revision", ["git", "rev-parse", "HEAD"], harness).strip()
        if receipt["harnessRevision"] != os.environ["GITHUB_SHA"]:
            raise RuntimeError("harness checkout differs from actual PR event SHA")
        run("harness-clean", ["git", "diff", "--exit-code", "HEAD", "--"], harness)
        workspace = Path(os.environ["PURIFIER_SOURCE"])
        receipt["purifierRevision"] = run("purifier-revision", ["git", "rev-parse", "HEAD"], workspace).strip()
        if receipt["purifierRevision"] != PURIFIER_REVISION:
            raise RuntimeError("tested purifier is not the approved public commit")
        build = json.loads((Path(os.environ["RUNNER_TEMP"]) / "log4j-build.json").read_text())
        metadata = json.loads((Path(os.environ["RUNNER_TEMP"]) / "log4j-source.json").read_text())
        if metadata.get("revision") != PURIFIER_REVISION or metadata.get("locked", {}).get("narHash") != PURIFIER_NAR:
            raise RuntimeError("built frozen source does not match clean purifier SHA")
        if len(build) != 1 or str(cli.parent.parent) not in build[0]["outputs"].values():
            raise RuntimeError("executed CLI output does not match recorded Nix build")
        receipt["purifierSource"] = {"path": metadata["path"], "revision": metadata["revision"],
                                     "narHash": metadata["locked"]["narHash"]}
        receipt["cliBuild"] = build[0]
        run("cli-derivation", ["nix", "--offline", "derivation", "show", build[0]["drvPath"]])
        run("purifier-source-nar", ["nix", "--offline", "path-info", "--json", metadata["path"]])
        run("purifier-clean-before", ["git", "diff", "--exit-code", "HEAD", "--"], workspace)
        receipt["purifierLockSha256"] = hashlib.sha256((workspace / "flake.lock").read_bytes()).hexdigest()
        run("cli-nar", ["nix", "--offline", "path-info", "--json", str(cli.parent.parent)])
        run("cli-deriver", ["nix-store", "--query", "--deriver", str(cli.parent.parent)])
        receipt["cliSha256"] = hashlib.sha256(cli.read_bytes()).hexdigest()
        receipt["javaVersion"] = run("java-version", [str(Path(env["JAVA_HOME"]) / "bin/java"), "-version"])
        if not re.search(r'version "17[.\"]', receipt["javaVersion"]):
            raise RuntimeError("explicit SDK17 required")
        run("docker-version", ["docker", "version", "--format", "{{json .}}"])
        run("docker-info", ["docker", "info", "--format",
                            '{"architecture":"{{.Architecture}}","cpus":{{.NCPU}},"memory":{{.MemTotal}},"driver":"{{.Driver}}","serverVersion":"{{.ServerVersion}}"}'])
        run("runner-capacity", ["sh", "-c", "uname -a; getconf _NPROCESSORS_ONLN; head -5 /proc/meminfo; df -h ."])
        run("source-init", ["git", "init", str(root / "source")])
        source = root / "source"
        run("source-remote", ["git", "remote", "add", "origin", "https://github.com/apache/logging-log4j2.git"], source)
        run("source-fetch", ["git", "fetch", "--depth", "1", "origin", REVISION], source)
        run("source-checkout", ["git", "checkout", "--detach", "FETCH_HEAD"], source)
        if run("source-revision", ["git", "rev-parse", "HEAD"], source).strip() != REVISION:
            raise RuntimeError("source revision mismatch")
        run("submodules", ["git", "submodule", "update", "--init", "--recursive"], source)
        command = [str(cli), "lock", "--lockfile", str(root / "maven-purifier.lock.json"), *GOALS]
        output = run("capture", command, source, limit=1200)
        # Full reactor capture is the actual proof; no filtered or skipped run.
        required_markers = ["BUILD SUCCESS", "log4j-layout-template-json-test", "log4j-mongodb"]
        missing = [x for x in required_markers if x not in output]
        if missing:
            raise RuntimeError("missing actual service/IT evidence: " + repr(missing))
        summary = re.compile(r"Tests run:\s*(\d+), Failures:\s*(\d+), Errors:\s*(\d+), Skipped:\s*(\d+).*?-- in ([\w.]+)")
        service_tests = []
        for match in summary.finditer(output):
            tests, failures, errors, skipped = map(int, match.groups()[:4])
            name = match.group(5)
            if name.endswith("LogstashIT") or ".mongodb." in name and name.endswith("IT"):
                if tests <= skipped or failures or errors:
                    raise RuntimeError("service integration suite did not execute successfully: " + name)
                service_tests.append(name)
        if not any(x.endswith("LogstashIT") for x in service_tests) or not any(x.endswith("MongoDbIT") for x in service_tests):
            raise RuntimeError("missing successful executed LogstashIT/MongoDbIT result summaries")
        receipt["executedServiceSuites"] = service_tests
        for module in ["log4j-layout-template-json-test", "log4j-mongodb"]:
            if not re.search(r"--- (?:docker|docker-maven-plugin):0\.45\.1:start[^\n]* @ " + module + r" ---", output):
                raise RuntimeError("missing real Fabric8 start execution: " + module)
        reactor_summary = output.rsplit("Reactor Summary", 1)[-1]
        successful = []
        for module in EXPECTED_REACTOR:
            if not re.search(r"(?m)^\[INFO\] " + re.escape(module["name"]) + r"[. ]+SUCCESS \[", reactor_summary):
                raise RuntimeError("full reactor module not successful: " + module["artifactId"])
            successful.append(module["artifactId"])
        if len(successful) != 31:
            raise RuntimeError("expected exact full31 reactor")
        receipt["successfulReactor"] = successful
        lock = json.loads((root / "maven-purifier.lock.json").read_text())
        receipt["lockSha256"] = hashlib.sha256((root / "maven-purifier.lock.json").read_bytes()).hexdigest()
        if lock.get("goals") != GOALS:
            raise RuntimeError("capture lock goal list differs from approved full build")
        receipt["lockJavaMajor"] = lock.get("javaMajor")
        if receipt["lockJavaMajor"] != "17":
            raise RuntimeError("capture lock must record caller Java17")
        run("source-unchanged", ["git", "diff", "--exit-code", "HEAD", "--"], source)
        image_data = json.loads(run("images", ["docker", "image", "inspect", *IMAGES]))
        if len(image_data) != 3:
            raise RuntimeError("need three actual service images")
        receipt["images"] = []
        for reference, image in zip(IMAGES, image_data):
            if image.get("Os") != "linux" or image.get("Architecture") != "amd64" or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")):
                raise RuntimeError("unexpected service image platform/ID: " + reference)
            if reference not in image.get("RepoTags", []):
                raise RuntimeError("upstream service tag missing: " + reference)
            receipt["images"].append({"reference": reference, "id": image["Id"],
                                     "platform": "linux/amd64", "repoDigests": image.get("RepoDigests", []),
                                     "missingRepoDigest": not bool(image.get("RepoDigests"))})
        run("purifier-clean-after", ["git", "diff", "--exit-code", "HEAD", "--"], workspace)
        if hashlib.sha256((workspace / "flake.lock").read_bytes()).hexdigest() != receipt["purifierLockSha256"]:
            raise RuntimeError("purifier flake.lock changed")
        receipt["passed"] = True
        receipt["limits"] = "One disposable hosted job;1200s capture deadline; actual platform capacity recorded. No fabricated hard per-job RAM/disk quota."
    except Exception as error:
        receipt["error"] = str(error)
    finally:
        try:
            run("remaining-containers", ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Status}}"], required=False)
        except Exception as error:
            receipt["finalDockerObservationError"] = str(error)
        receipt["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
