# SPDX-License-Identifier: MIT
"""Compile, test, relock, prefetch and sandbox-build using a Maven release."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("version")
parser.add_argument("cli", type=Path)
parser.add_argument("jdk", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
cli = str(args.cli.resolve())
env = dict(os.environ, JAVA_HOME=str(args.jdk.resolve()))
snapshot = json.loads(subprocess.check_output(['nix', 'flake', 'metadata', '--json',
    '--no-write-lock-file', str(root)], text=True))['path']
with tempfile.TemporaryDirectory(prefix="maven-compatibility-") as tmp:
    subprocess.run(['nix-store', '--add-root', str(Path(tmp)/'source'), '--realise', snapshot], check=True)
    project = Path(tmp) / "project"
    shutil.copytree(Path(__file__).parent, project, ignore=shutil.ignore_patterns("check.py", "__pycache__"))
    wrapper = project / ".mvn/wrapper/maven-wrapper.properties"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
        f"{args.version}/apache-maven-{args.version}-bin.tar.gz\n"
    )

    def run(*command, extra_env=None):
        capture = command[:2] == ("nix", "build") and "MAVEN_PURIFIER_EVIDENCE_DIR" in env
        result = subprocess.run(command, cwd=project, env=env | (extra_env or {}), check=True,
                                text=True, stdout=subprocess.PIPE if capture else None)
        if capture:
            print(result.stdout, end="", flush=True)
            builds = json.loads(result.stdout)
            outputs = [output for build in builds for output in build['outputs'].values()]
            directory = Path(env['MAVEN_PURIFIER_EVIDENCE_DIR'])
            directory.mkdir(parents=True, exist_ok=True)
            receipt = {
                'sourcePath': snapshot,
                'mavenVersion': args.version,
                'javaHome': str(args.jdk.resolve()),
                'command': list(command),
                'builds': builds,
                'outputs': json.loads(subprocess.check_output(['nix', 'path-info', '--json', *outputs])),
                'derivations': json.loads(subprocess.check_output(
                    ['nix', 'derivation', 'show', *[build['drvPath'] for build in builds]])),
            }
            (directory / f'build-{len(list(directory.glob("build-*.json")))}.json').write_text(
                json.dumps(receipt, indent=2) + '\n')

    run(cli, "lock")
    lockfile = project / "maven-purifier.lock.json"
    initial = lockfile.read_bytes()
    data = json.loads(initial)
    assert data["maven"]["version"] == args.version
    assert data["javaMajor"] == ("8" if args.version.startswith(("2.", "3.0.")) else "17")
    run(cli, "lock", "--force")
    assert lockfile.read_bytes() == initial, "a relock changed the pinned resolution"
    run(cli, "prefetch")
    run(str(project / ".maven-purifier/bin/mvn"), "-B", "clean", "install",
        extra_env={"JAVA_HOME": "/deliberately-absent-jdk"})
    # This also checks that prefetch's generated config doesn't stale the lock.
    run(cli, "prefetch")
    expression = """
      let
        f = builtins.getFlake ("path:" + builtins.getEnv "COMPAT_FLAKE");
        p = import f.inputs.nixpkgs {
          system = builtins.currentSystem;
          overlays = [ f.overlays.default ];
        };
      in (p.maven-purifier.buildMavenProject {
        src = /. + builtins.getEnv "COMPAT_PROJECT";
      }).tests
    """
    run("nix", "build", "--impure", "--no-link", "--json", "--print-build-logs", "--expr", expression,
        extra_env={"COMPAT_FLAKE": snapshot, "COMPAT_PROJECT": str(project)})

    if args.version.startswith('2.'):
        # Maven 2 has no native model adapter, but its sandbox builder still
        # accepts Nix inputs. Exercise the Java 8 recorder in that mode too.
        composed = expression.replace('src = /. + builtins.getEnv "COMPAT_PROJECT";',
            'src = /. + builtins.getEnv "COMPAT_PROJECT"; '
            'projectDependencies = [ f.checks.${builtins.currentSystem}.composition.producer ];')
        run('nix', 'build', '--impure', '--no-link', '--json', '--print-build-logs', '--expr', composed,
            extra_env={'COMPAT_FLAKE': snapshot, 'COMPAT_PROJECT': str(project)})

    if not args.version.startswith('2.'):
        # Maven 3.0 needs the Plexus registration; newer Maven 3 and Maven 4
        # use their respective model APIs. Exercise inferred input selection
        # on the same JDK as the baseline compatibility test.
        project = Path(tmp) / 'project-input'
        shutil.copytree(Path(__file__).parent, project, ignore=shutil.ignore_patterns('check.py', '__pycache__'))
        wrapper = project / '.mvn/wrapper/maven-wrapper.properties'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/'
                           + args.version + '/apache-maven-' + args.version + '-bin.tar.gz\n')
        pom = project / 'pom.xml'
        pom.write_text(pom.read_text().replace('<dependencies>', '<dependencies><dependency>'
            '<groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId>'
            '<version>99.0</version></dependency><dependency>'
            '<groupId>org.apache.commons</groupId><artifactId>commons-text</artifactId>'
            '<version>1.13.0</version></dependency>'))
        source_pom = pom.read_bytes()
        (project / 'src/main/java/example/Hello.java').write_text(
            'package example; public final class Hello { public static int answer() { '
            'return org.apache.commons.lang3.StringUtils.capitalize("nix").equals("Nix") ? 42 : 0; } }\n')
        system = subprocess.check_output(['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'], text=True)
        reference = 'path:' + snapshot + '#checks.' + system + '.composition.producer'
        run(cli, 'lock', '--input', reference)
        data = json.loads((project / 'maven-purifier.lock.json').read_text())
        assert len(data['projectInputs']) == 1
        assert not any('/commons-lang3/0.0.1-nix-' in entry['path'] for entry in data['downloads'])
        run(cli, 'prefetch')
        run(str(project / '.maven-purifier/bin/mvn'), '-B', 'clean', 'install')
        assert pom.read_bytes() == source_pom
        runtime_expression = expression.replace('in (p.maven-purifier.buildMavenProject {',
            'built = p.maven-purifier.buildMavenProject {').replace('}).tests', '''};
          in p.runCommand "maven-compatibility-runtime" { tests = built.tests; } ''
            find ${built.projections.maven}/org/apache/commons/commons-text -name '*-nix-*.pom' | grep -q .
            touch $out
          '' ''')
        run('nix', 'build', '--impure', '--no-link', '--json', '--print-build-logs', '--expr', runtime_expression,
            extra_env={'COMPAT_FLAKE': snapshot, 'COMPAT_PROJECT': str(project)})
