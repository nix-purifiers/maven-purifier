#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a pinned upstream Maven reactor through capture and sandboxed builds.

The output directory must be new. This is an individual project observation;
ranking eligibility and completeness of a top-50 set are separate obligations.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def test_counts(root):
    reports = sorted(Path(root).rglob("TEST-*.xml"))
    counts = dict(reports=len(reports), tests=0, failures=0, errors=0, skipped=0)
    for report in reports:
        document = ET.parse(report).getroot()
        suites = list(document.iter("testsuite"))
        # Container suites may repeat child totals.
        for suite in suites:
            if suite.findall("testsuite"):
                continue
            for key in ("tests", "failures", "errors", "skipped"):
                value = int(suite.get(key, "0"))
                if value < 0:
                    raise ValueError(f"negative {key} in {report}")
                counts[key] += value
    counts["executed"] = counts["tests"] - counts["skipped"]
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purifier", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--build-root", default=".")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--minimum-tests", type=int, default=1)
    parser.add_argument("goals", nargs="*", default=["clean", "install"])
    args = parser.parse_args()
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        parser.error("--revision must be a full Git commit")
    if args.minimum_tests < 0 or args.timeout <= 0:
        parser.error("invalid test minimum or timeout")
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    report = dict(schemaVersion=1, repository=args.repository, revision=args.revision,
                  runnerSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  buildRoot=args.build_root, goals=args.goals, minimumTests=args.minimum_tests,
                  startedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  stages={}, selectedProjectPassed=False)

    def save():
        pending = results / "report.json.tmp"
        pending.write_text(json.dumps(report, indent=2) + "\n")
        pending.replace(results / "report.json")

    save()
    env = dict(os.environ)
    home = results / "home"
    home.mkdir()
    env.update(HOME=str(home), XDG_CACHE_HOME=str(home / "cache"), CI="true")

    def run(name, command, cwd=results):
        log = results / f"{name}.log"
        stdout = results / f"{name}.stdout"
        stage = dict(command=command, cwd=str(cwd), status="running")
        report["stages"][name] = stage
        save()
        start = time.monotonic()
        with stdout.open("w") as output, log.open("w") as errors:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=errors, start_new_session=True)
            try:
                stage["exitCode"] = process.wait(timeout=args.timeout)
                stage["status"] = "pass" if process.returncode == 0 else "fail"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                stage.update(status="timeout", exitCode=process.returncode)
        stage.update(seconds=round(time.monotonic() - start, 3), log=log.name,
                     logSha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                     stdout=stdout.name, stdoutSha256=hashlib.sha256(stdout.read_bytes()).hexdigest())
        save()
        print(name, stage["status"], flush=True)
        if stage["status"] != "pass":
            raise RuntimeError(f"{name}: {stage['status']}; see {log}")
        return stdout.read_text()

    try:
        metadata = json.loads(run("purifier-source", ["nix", "flake", "metadata", "--json",
                                                     str(args.purifier.resolve())]))
        report["purifier"] = metadata
        purifier = metadata["path"]
        cli = run("cli", ["nix", "build", purifier + "#default", "--no-link", "--print-out-paths"]).strip()
        config = json.loads(run("nix-config", ["nix", "config", "show", "--json"]))
        report["sandbox"] = config["sandbox"]["value"]
        if report["sandbox"] is not True:
            raise RuntimeError("Nix sandbox=true is required for offline build evidence")
        run("clone", ["git", "init", str(results / "source")])
        source = results / "source"
        run("remote", ["git", "remote", "add", "origin", args.repository], source)
        run("fetch", ["git", "fetch", "--depth", "1", "origin", args.revision], source)
        run("checkout", ["git", "checkout", "--detach", "FETCH_HEAD"], source)
        run("submodules", ["git", "submodule", "update", "--init", "--recursive"], source)
        actual = run("source-revision", ["git", "rev-parse", "HEAD"], source).strip()
        if actual != args.revision:
            raise RuntimeError("source revision mismatch")
        build_root = (source / args.build_root).resolve()
        if not build_root.is_relative_to(source) or not (build_root / "pom.xml").is_file():
            raise RuntimeError("build root must name a Maven project within the source")
        lock = results / "maven-purifier.lock.json"
        command = [str(Path(cli) / "bin/maven-purifier")]
        settings = ["--settings", str(args.settings.resolve())] if args.settings else []
        run("capture", command + ["lock", "--lockfile", str(lock)] + settings + args.goals, build_root)
        report["lockSha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
        run("prefetch", command + ["prefetch", "--lockfile", str(lock)], build_root)
        # This launcher selects the locked Maven, JDK, repository and offline
        # settings. Host network denial is established by the Nix builds below.
        run("native-offline", [str(build_root / ".maven-purifier/bin/mvn")] + args.goals, build_root)
        expression = (
            "let f = builtins.getFlake " + json.dumps(purifier) + "; "
            "pkgs = import f.inputs.nixpkgs { system = builtins.currentSystem; "
            "overlays = [ f.overlays.default ]; }; "
            "in pkgs.maven-purifier.buildMavenProject { src = builtins.toPath "
            + json.dumps(str(build_root)) + "; lockFile = builtins.toPath "
            + json.dumps(str(lock)) + "; }")
        (results / "project.nix").write_text(expression + "\n")
        for output in ("release", "tests"):
            builds = json.loads(run(output, ["nix", "build", "--impure", "--no-link", "--json",
                                            "--expr", "(" + expression + ")." + output]))
            report[output + "Builds"] = builds
            save()
        counts = test_counts(report["testsBuilds"][0]["outputs"]["out"])
        report["testCounts"] = counts
        report["selectedProjectPassed"] = (
            counts["executed"] >= args.minimum_tests and counts["failures"] == 0
            and counts["errors"] == 0)
        if not report["selectedProjectPassed"]:
            raise RuntimeError(f"insufficient or failed test execution: {counts}")
    except Exception as error:
        report["error"] = str(error)
    finally:
        report["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
    return 0 if report["selectedProjectPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
