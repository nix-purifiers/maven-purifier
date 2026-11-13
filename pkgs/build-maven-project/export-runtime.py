# SPDX-License-Identifier: MIT
"""Export Maven's evaluated project runtime artifacts, excluding its tool realms."""
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import coursier_runtime

records, repository, catalog, destination, projects = map(Path, sys.argv[2:])
published = {(project['groupId'], project['artifactId']) for project in json.loads(projects.read_text())}
paths = set()
for line in records.read_text().splitlines():
    event = json.loads(line)
    if (event.get('event') == 'runtime'
            and (event['projectGroupId'], event['projectArtifactId']) in published):
        paths.add(event['file'])
coursier_runtime.export(repository, sorted(paths), catalog, destination)
