#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Capture, replay and remove a download from a real requiresOnline build."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def main():
    cli = str(Path(sys.argv[1]).resolve())
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="maven-online-goal-") as temporary:
        work = Path(temporary)
        source = work / "project"
        shutil.copytree(root / "tests/fixtures/online-goal", source)
        home = work / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home))

        def run(command, success=True):
            result = subprocess.run(command, cwd=source, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
            print(result.stdout, end="", flush=True)
            if (result.returncode == 0) != success:
                raise AssertionError(f"unexpected exit {result.returncode}: {command}")
            return result.stdout

        lock = source / "maven-purifier.lock.json"
        before = lock.read_bytes()
        run([cli, "lock", "--force"])
        assert lock.read_bytes() == before, "capture did not reproduce the committed fixture lock"
        run([cli, "prefetch"])
        launcher = str(source / ".maven-purifier/bin/mvn")
        run([launcher, "clean", "install"])
        bom = json.loads((source / "target/bom.json").read_text())
        assert any(component["name"] == "commons-lang3" for component in bom["components"])
        managed = (source / ".mvn/maven.config").read_text().split(
            "-Dmaven-purifier.prefetched=true\n", 1)[1].splitlines()
        settings = Path(next(line.split("=", 1)[1] for line in managed if line.startswith("--settings=")))
        repository = Path(next(element.text for element in ET.parse(settings).getroot().iter()
                               if element.tag.rsplit("}", 1)[-1] == "localRepository"))
        jar = repository / "org/apache/commons/commons-lang3/3.17.0/commons-lang3-3.17.0.jar"
        jar.unlink()
        log = run([launcher, "clean", "install"], success=False)
        assert "offline" in log.lower() and "commons-lang3" in log
        assert not jar.exists(), "missing locked download fetched from the network"


if __name__ == "__main__":
    main()
