# SPDX-License-Identifier: MIT
"""Export Maven's evaluated project runtime artifacts, excluding its tool realms."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import coursier_runtime

records, repository, catalog, destination, projects = map(Path, sys.argv[2:])
published = {(project['groupId'], project['artifactId']) for project in json.loads(projects.read_text())}
paths = set()
coordinates_by_file = {}
files = [records]
parts = records.with_name(records.name + ".parts")
for phase in ("build", "development"):
    files.extend(sorted((parts / phase).glob("*.jsonl")))
for source in files:
    data = source.read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError(f"unfinished Maven runtime record: {source}")
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f"Maven runtime record is not an object: {source}:{number}")
        if (event.get('event') == 'runtime'
                and (event['projectGroupId'], event['projectArtifactId']) in published):
            path = event['file']
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError(f"Maven runtime artifact path is not absolute: {source}:{number}")
            path = os.path.normpath(path)
            coordinates = (event['groupId'], event['artifactId'], event['version'])
            previous = coordinates_by_file.setdefault(path, coordinates)
            if previous != coordinates:
                raise ValueError(f"conflicting Maven runtime coordinates for {path}")
            paths.add(path)
coursier_runtime.export(repository, sorted(paths), catalog, destination)
