# SPDX-License-Identifier: MIT
"""Deploy immutable Maven projections, including their Nix-provided closure."""
import argparse
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser(description=__doc__)
for name in ('maven', 'repository', 'catalog', 'local-repository', 'plugin-version', 'repository-id', 'url'):
    parser.add_argument('--' + name, required=True)
parser.add_argument('arguments', nargs=argparse.REMAINDER)
args = parser.parse_args()
extra = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
catalog = json.loads(Path(args.catalog).read_text())
modules = catalog.get('publications', catalog['modules'])
for module in modules:
    group, name, version = (module[key] for key in ('group', 'name', 'version'))
    directory = Path(args.repository) / group.replace('.', '/') / name / version
    prefix = name + '-' + version
    pom = directory / (prefix + '.pom')
    model = ET.parse(pom).getroot()
    namespace = model.tag.rpartition('}')[0] + '}' if '}' in model.tag else ''
    packaging = model.findtext(namespace + 'packaging', 'jar')
    artifacts = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        suffix = path.name[len(prefix):]
        if suffix.endswith(('.sha1', '.md5', '.sha256', '.sha512', '.asc')):
            continue
        if suffix.startswith('.'):
            classifier, extension = '', suffix[1:]
        elif suffix.startswith('-') and '.' in suffix:
            classifier, extension = suffix[1:].split('.', 1)
        else:
            continue
        artifacts.append((path, classifier, extension))
    main_type = 'jar' if packaging in ('bundle', 'maven-plugin', 'ejb') else packaging
    main = next((artifact for artifact in artifacts if artifact[1:] == ('', main_type)),
                (pom, '', 'pom'))
    base = [args.maven, '-B', '-Dmaven.repo.local=' + args.local_repository,
            '-Daether.remoteRepositoryFilter.prefixes=false', *extra,
            'org.apache.maven.plugins:maven-deploy-plugin:' + args.plugin_version + ':deploy-file',
            '-DrepositoryId=' + args.repository_id, '-Durl=' + args.url, '-DgeneratePom=false',
            '-DgroupId=' + group, '-DartifactId=' + name, '-Dversion=' + version]
    print('maven-purifier: publishing ' + group + ':' + name + ':' + version, flush=True)
    subprocess.run(base + ['-Dfile=' + str(main[0]), '-DpomFile=' + str(pom)], check=True)
    for path, classifier, extension in artifacts:
        if path == pom or path == main[0]:
            continue
        command = base + ['-Dfile=' + str(path), '-DpomFile=' + str(pom), '-Dpackaging=' + extension]
        if classifier:
            command.append('-Dclassifier=' + classifier)
        subprocess.run(command, check=True)
