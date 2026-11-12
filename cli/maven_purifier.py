# SPDX-License-Identifier: MIT
"""maven-purifier: lock a Maven build's downloads, work offline from them, publish what Nix built.

  maven-purifier lock [--lockfile FILE] [--force] [--input FLAKE#PACKAGE] [--settings FILE] [GOAL...]
      Runs the build once with an empty local repository and records every
      file Maven downloads in the lockfile, maven-purifier.lock.json at the
      build root unless --lockfile names another. The default goals are
      `clean install`. The lock holds what these goals need, so lock the
      goals you run; Maven options such as -P belong among them. A second
      pass adds what development needs beyond them: the plugins of the
      build, the source jars of its dependencies, and the default plugins
      of the Maven inside the editor. The lock run uses the settings FILE,
      or ~/.m2/settings.xml, and the lockfile records them without servers
      and proxies. The lock is renewed only when the build definition, the
      goals or the settings changed, unless --force is given. A relock
      keeps what version ranges and SNAPSHOTs resolved to in the previous
      lock. `--input` supplies a Nix-built JVM package; repeat it for more
      inputs. Its native coordinates are discovered from its repository
      projection, and its Nix identity is pinned separately from downloads.

  maven-purifier update [--lockfile FILE] [--settings FILE] [GROUP:ARTIFACT...]
      Locks again with the goals of the lockfile, resolving the version
      ranges and SNAPSHOTs of the named packages, or of every package,
      afresh.

  maven-purifier prefetch [--lockfile FILE]
      Fetches everything the lockfile lists into the Nix store, behind a
      garbage-collector root in .maven-purifier/ at the build root, and
      points Maven at it: it writes lines of its own at the end of
      .mvn/maven.config, so that `mvn`, `./mvnw` and an editor that reads
      that file resolve offline from the locked files. The locked Maven is
      .maven-purifier/bin/mvn. Local builds keep your existing Java runtime;
      .maven-purifier/jdk points to the runtime found during prefetch.

  maven-purifier publish [--lockfile FILE] [--repository-id ID --url URL] [ARG...]
      Builds the release with Nix and deploys it with the locked Maven and
      ~/.m2/settings.xml, which holds the credentials. The default target is
      the root pom.xml's distributionManagement.

Every command takes the lockfile from --lockfile, or else from the
environment variable MAVEN_PURIFIER_LOCKFILE, or else from the build root.

The package that installs this script sets the environment variables that
pkgs/cli/default.nix describes.
"""

import base64
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
import javadoc_resources
from pathlib import Path
from typing import NoReturn

LOCK_NAME = "maven-purifier.lock.json"
FORMAT_VERSION = 2
STATE_DIR = ".maven-purifier"
CENTRAL = "https://repo.maven.apache.org/maven2/"

# The line of .mvn/maven.config from which on `prefetch` writes lines of
# its own (lib-overlays/default/maven-config-marker.nix). It defines a property that
# nothing reads, since maven.config has no comments.
PREFETCH_MARKER = "-Dmaven-purifier.prefetched=true"

# The maven-dependency-plugin of the development pass of a lock run, at a
# fixed version, so that a relock does not move with its releases.
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"

# Files Maven Resolver writes into a local repository for its own
# bookkeeping. They are not downloads, and an offline build does not need
# them.
BOOKKEEPING = {"_remote.repositories", "_maven.repositories", "resolver-status.properties", "maven-metadata-local.xml"}

# Maven Resolver stores the checksum files it fetched to validate a download
# beside the download. An offline build validates no checksums.
CHECKSUM_SUFFIXES = (".sha1", ".md5", ".sha256", ".sha512")

# Flags of every Maven run of the purifier. Maven Resolver 2 downloads a
# list of the groups each repository holds (.meta/prefixes.txt), whose
# contents change as groups are published; the list only saves requests,
# so the runs switch the filter that reads it off. Maven Resolver 1 does
# not know the property.
MAVEN_FLAGS = ["-Daether.remoteRepositoryFilter.prefixes=false"]

# Directories that never belong to the build definition or the build input.
SKIPPED_DIRS = {"target", ".git", STATE_DIR, ".idea", "node_modules"}

# Maven Central serves the same files under each of these URLs. The lock
# records the first as a file's URL and the others as its mirrors.
CENTRAL_HOSTS = (
    "https://repo1.maven.org/maven2/",
    "https://repo.maven.apache.org/maven2/",
    "https://maven-central.storage-download.googleapis.com/maven2/",
    "http://repo1.maven.org/maven2/",
    "http://repo.maven.apache.org/maven2/",
)

LEGACY_RECORDER = "io.github.nix-purifiers:maven-purifier-legacy-recorder:1:record"
LEGACY_RECORDER_PATH = "io/github/nix-purifiers/maven-purifier-legacy-recorder/1/"


def maven_version_tuple(mvn):
    version = Path(mvn).parent.parent.name.removeprefix("apache-maven-")
    found = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not found:
        fail(f"cannot determine the Maven version from {mvn}")
    return tuple(int(x or 0) for x in found.groups())


def seed_legacy_recorder(repo):
    target = repo / LEGACY_RECORDER_PATH
    target.mkdir(parents=True, exist_ok=True)
    # Older plugin containers must not discover the core model adapters or
    # Maven 4 classes while loading this small recording Mojo.
    with zipfile.ZipFile(os.environ["MAVEN_PURIFIER_EXTENSION"]) as source, zipfile.ZipFile(
        target / "maven-purifier-legacy-recorder-1.jar", "w"
    ) as output:
        for entry in source.infolist():
            if entry.filename == "META-INF/maven/plugin.xml" or (
                entry.filename.startswith("io/github/nixpurifiers/maven/LegacyRecorderMojo")
                and entry.filename.endswith(".class")
            ):
                output.writestr(entry, source.read(entry.filename))
    (target / "maven-purifier-legacy-recorder-1.pom").write_text(
        '<project><modelVersion>4.0.0</modelVersion><groupId>io.github.nix-purifiers</groupId>'
        '<artifactId>maven-purifier-legacy-recorder</artifactId><version>1</version>'
        '<packaging>maven-plugin</packaging></project>\n')


def legacy_remote_path(relative):
    """Legacy Resolver appends the repository id to a remote metadata filename."""
    path = Path(relative)
    if path.name.startswith("maven-metadata-") and path.suffix == ".xml":
        return str(path.with_name("maven-metadata.xml"))
    return relative


def legacy_remote_paths(relative, repo):
    paths = [legacy_remote_path(relative)]
    file = Path(relative)
    if "-SNAPSHOT" in file.name:
        for metadata in sorted((repo / file.parent).glob("maven-metadata*.xml")):
            try:
                snapshot = ET.parse(metadata).find("versioning/snapshot")
                if snapshot is not None:
                    timestamp = snapshot.findtext("timestamp")
                    build = snapshot.findtext("buildNumber")
                    if timestamp and build:
                        paths.append(str(file.with_name(file.name.replace("-SNAPSHOT", f"-{timestamp}-{build}"))))
            except (OSError, ET.ParseError):
                continue
    return list(dict.fromkeys(paths))


def record_legacy_downloads(repo, record, preserved=()):
    """Recover origins from the effective reactor, verifying the bytes remotely.

    Maven 2 and Maven 3.0 have no repository events. The recorder goal supplies
    their effective repositories and installed artifacts. Every remaining file
    must match a file at one of those repositories before it can enter the lock.
    Do this after each pass so build downloads remain distinct from IDE extras.
    """
    events = read_record(record)
    seen = {e["file"] for e in events if e["event"] in ("download", "install", "resolved")}
    repositories = {}
    for event in events:
        if event["event"] in ("repository", "download"):
            base = event["repositoryUrl"]
            repositories[event["repository"]] = CENTRAL if base.rstrip("/") + "/" in CENTRAL_HOSTS else base
    installed = {Path(e["file"]).parent for e in events if e["event"] == "install"}
    module_dirs = installed | {p.parent for p in installed}
    with record.open("a") as output:
        for file in sorted(repo.rglob("*")):
            if not file.is_file() or str(file) in seen:
                continue
            relative = file.relative_to(repo).as_posix()
            if relative in preserved:
                output.write(json.dumps({"event": "resolved", "file": str(file)}) + "\n")
                continue
            if (relative.startswith(LEGACY_RECORDER_PATH) or file.name in BOOKKEEPING
                or file.name.endswith((".lastUpdated", *CHECKSUM_SUFFIXES))
                or (file.name.startswith("maven-metadata") and file.parent in module_dirs)):
                continue
            preferred = tracked_repository(file)
            candidates = [(key, base, remote) for key, base in sorted(repositories.items(), key=lambda item: item[0] != preferred)
                          for remote in legacy_remote_paths(relative, repo)]
            expected = sri(file)
            for repository, base, remote in candidates:
                url = base.rstrip("/") + "/" + remote
                try:
                    with urllib.request.urlopen(fetch_url(url), timeout=60) as response:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: response.read(1 << 20), b""):
                            digest.update(chunk)
                    actual = "sha256-" + base64.b64encode(digest.digest()).decode()
                except (OSError, urllib.error.HTTPError):
                    continue
                if actual != expected:
                    continue
                output.write(json.dumps({"event": "download", "repository": repository,
                    "repositoryUrl": base, "url": url, "file": str(file)}) + "\n")
                break
            else:
                fail(f"cannot establish the repository and matching bytes of legacy Maven file {relative}")


def fail(message) -> NoReturn:
    print(f"maven-purifier: {message}", file=sys.stderr)
    sys.exit(1)


def info(message):
    print(f"maven-purifier: {message}", file=sys.stderr)


def build_root():
    root = Path.cwd()
    if not (root / "pom.xml").is_file():
        fail(f"{root} has no pom.xml; run maven-purifier from the root of a Maven build")
    return root


def sri(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return "sha256-" + base64.b64encode(digest.digest()).decode()


def canonical_json(value):
    """JSON as Nix's builtins.toJSON writes it: keys sorted, no spaces, text unescaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lock_hash(lock_data):
    """The lockHash of a lockfile: the SHA-256 of its canonical JSON without the field."""
    without = {k: v for k, v in lock_data.items() if k != "lockHash"}
    return "sha256:" + hashlib.sha256(canonical_json(without).encode()).hexdigest()


def build_config_lines(text):
    """The build's own lines of .mvn/maven.config: those before the lines of `prefetch`, less trailing empty ones."""
    lines = text.split("\n")
    if PREFETCH_MARKER in lines:
        lines = lines[: lines.index(PREFETCH_MARKER)]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def without_prefetch_lines(text):
    """The text of .mvn/maven.config with only the build's own lines, each ending in a newline."""
    lines = build_config_lines(text)
    return "".join(line + "\n" for line in lines)


def definition_files(root):
    """The files that define the build, as relative path strings in sorted order.

    They are every regular file named pom.xml, and every regular file under
    .mvn/ at the build root. Directories in SKIPPED_DIRS and symbolic links
    take no part.
    lib-overlays/default/definition-hash.nix walks the build root by the same rules.
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
        rel = Path(dirpath).relative_to(root)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            if name == "pom.xml" or (rel.parts and rel.parts[0] == ".mvn"):
                files.append((rel / name).as_posix())
    return sorted(files)


def definition_hash(root, goals):
    """A hash of the build definition and the lock goals.

    It is the SHA-256, in hex, of the text "goals <goals joined by spaces>"
    followed by a line "<relative path> <SHA-256 of the file in hex>" for
    each definition file. For .mvn/maven.config the hash covers the build's
    own lines (build_config_lines) joined by newlines, so the lines that
    `prefetch` writes take no part. Nix computes the same text from the
    build root (lib-overlays/default/definition-hash.nix), so a sandboxed build can tell
    a stale lock at evaluation time.
    """
    lines = ["goals " + " ".join(goals)]
    for rel in definition_files(root):
        content = (root / rel).read_bytes()
        if rel == ".mvn/maven.config":
            content = "\n".join(build_config_lines(content.decode())).encode()
            if not content:
                continue
        lines.append(f"{rel} {hashlib.sha256(content).hexdigest()}")
    return "sha256:" + hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def distribution_url(root):
    """The Maven distribution the build uses: the one the Maven wrapper names, or the default."""
    props = root / ".mvn" / "wrapper" / "maven-wrapper.properties"
    if props.is_file():
        for line in props.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "distributionUrl":
                return value.strip().replace("\\:", ":")
    return maven_url(os.environ["MAVEN_PURIFIER_DEFAULT_MAVEN"])


def maven_url(version):
    return f"{CENTRAL}org/apache/maven/apache-maven/{version}/apache-maven-{version}-bin.tar.gz"


def central_path(url):
    """The path of url in Maven Central, when one of its hosts serves it, or None."""
    for host in CENTRAL_HOSTS:
        if url.startswith(host):
            return url[len(host) :]
    return None


def file_entry(path, url, hash_):
    """A file entry of the lock, with the canonical URL of a file of Maven Central.

    The lock holds no mirrors: Nix derives them from the URL (lock.nix), as
    the other hosts of Maven Central.
    """
    rest = central_path(url)
    return {"path": path, "url": url if rest is None else CENTRAL_HOSTS[0] + rest, "hash": hash_}


def central_mirror():
    """The URL the lock run downloads Maven Central's files from, when MAVEN_PURIFIER_CENTRAL_MIRROR names one."""
    mirror = os.environ.get("MAVEN_PURIFIER_CENTRAL_MIRROR")
    return (mirror.rstrip("/") + "/") if mirror else None


def fetch_url(url):
    """The URL the lock run fetches url from: the Central mirror for a file of Central, when one is set."""
    rest = central_path(url)
    if central_mirror() and rest is not None:
        return central_mirror() + rest
    return url


# The classifiers that os-maven-plugin gives each Nix system of the
# nix-purifiers. Builds that need a native binary, such as protoc or
# netty's transports, name it by such a classifier.
PLATFORM_CLASSIFIERS = {
    "x86_64-linux": "linux-x86_64",
    "aarch64-linux": "linux-aarch_64",
    "aarch64-darwin": "osx-aarch_64",
}


def native_system():
    uname = os.uname()
    system = {
        ("Linux", "x86_64"): "x86_64-linux",
        ("Linux", "aarch64"): "aarch64-linux",
        ("Linux", "arm64"): "aarch64-linux",
        ("Darwin", "arm64"): "aarch64-darwin",
    }.get((uname.sysname, uname.machine))
    if system is None:
        fail(
            "maven-purifier runs on x86_64-linux, aarch64-linux and aarch64-darwin, "
            f"and this machine is {uname.sysname} {uname.machine}"
        )
    return system


def platform_counterparts(path, native):
    """The paths of the other systems' builds of a file named by the native platform classifier, by system.

    A file such as protoc-4.35.1-linux-x86_64.exe has the counterpart
    protoc-4.35.1-linux-aarch_64.exe for aarch64-linux. A file with no
    platform classifier has none.
    """
    directory, _, name = path.rpartition("/")
    native_classifier = PLATFORM_CLASSIFIERS[native]
    match = re.fullmatch(rf"(.*-){re.escape(native_classifier)}(\..*)?", name)
    if match is None:
        return {}
    stem, suffix = match.group(1), match.group(2) or ""
    return {
        system: f"{directory}/{stem}{classifier}{suffix}"
        for system, classifier in PLATFORM_CLASSIFIERS.items()
        if system != native
    }


def download_path(url):
    """The tree path of a download that is not a Maven artifact, such as the Maven distribution."""
    parsed = urllib.parse.urlparse(url)
    marker = "/maven2/"
    if marker in parsed.path:
        return parsed.path.split(marker, 1)[1]
    return f"downloads/{parsed.netloc}{parsed.path}"


def prefetch_file(url):
    """Adds url to the Nix store and returns its SRI hash and store path."""
    try:
        out = subprocess.run(
            ["nix", "store", "prefetch-file", "--json", "--hash-type", "sha256", fetch_url(url)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        fail(f"could not download {fetch_url(url)}: {e.stderr.strip()}")
    result = json.loads(out.stdout)
    return result["hash"], result["storePath"]


def unpack_maven(archive, dest):
    """Unpacks a Maven distribution archive and returns its bin/mvn."""
    dest.mkdir(parents=True, exist_ok=True)
    if str(archive).endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="tar")
    homes = [p for p in dest.iterdir() if (p / "bin" / "mvn").exists()]
    if len(homes) != 1:
        fail(f"{archive} does not unpack to a single Maven home")
    for script in (homes[0] / "bin").iterdir():
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
    return homes[0] / "bin" / "mvn"


def java_home():
    """Select the existing local runtime without realizing or substituting a Nix SDK."""
    home = os.environ.get("JAVA_HOME")
    if home:
        home = str(Path(home).expanduser().absolute())
        java_version(home)
        return home
    executable = shutil.which("java")
    if executable is None:
        fail("set JAVA_HOME to an existing Java runtime or put java on PATH")
    properties = java_properties(executable)
    if not properties.get("java.version"):
        fail(f"could not read the version of the existing runtime {executable}")
    home = properties.get("java.home")
    if not home:
        fail(f"could not read java.home from the existing runtime {executable}")
    # Java 8 reports its nested JRE directory even when java belongs to a JDK.
    path = Path(home)
    if path.name == "jre" and (path.parent / "bin/javac").is_file():
        return str(path.parent)
    return home


def java_properties(executable):
    try:
        out = subprocess.run(
            [str(executable), "-XshowSettings:properties", "-version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        fail(f"cannot run the existing Java runtime {executable}: {error}")
    properties = {}
    for line in (out.stderr + "\n" + out.stdout).splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep:
            properties[key] = value
    return properties


def java_version(home):
    version = java_properties(f"{home}/bin/java").get("java.version")
    if version:
        return version
    fail(f"could not read the version of the JDK at {home}")


def java_major(version):
    found = re.match(r"(?:1\.)?(\d+)", version)
    if not found:
        fail(f"cannot determine the Java major version from {version!r}")
    return found.group(1)


def proxy_with_credentials():
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.environ.get(name)
        if value and urllib.parse.urlparse(value).username:
            return True
    return False


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def generated_obr_index(path, repo, installed):
    """Recognize Felix's local OBR index of this reactor's installed bundles.

    The bundle plugin writes repository.xml itself during install, outside
    Resolver events. It is regenerated during replay, not an input download.
    Do not exempt arbitrary files or indexes of unrecorded external bundles.
    """
    if path != repo / "repository.xml":
        return False
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    resources = list(root)
    if local_name(root.tag) != "repository" or not resources:
        return False
    for resource in resources:
        if local_name(resource.tag) != "resource":
            return False
        uri = urllib.parse.urlparse(resource.get("uri", ""))
        if uri.scheme not in ("", "file") or uri.netloc or uri.query or uri.fragment or not uri.path:
            return False
        target = (repo / urllib.parse.unquote(uri.path)).resolve()
        try:
            relative = target.relative_to(repo.resolve()).as_posix()
        except ValueError:
            return False
        if relative not in installed or not target.is_file():
            return False
    return True


def qualifier(element):
    """A function that names a child of element in the namespace of element."""
    namespace = element.tag[1:].split("}")[0] if element.tag.startswith("{") else ""
    if namespace:
        ET.register_namespace("", namespace)
    return lambda name: f"{{{namespace}}}{name}" if namespace else name


def env_proxies(qualify):
    """<proxy> elements for the proxy environment variables, which Maven ignores."""
    proxies = []
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    non_proxy = "|".join(h.strip() for h in no_proxy.split(",") if h.strip())
    for protocol in ("https", "http"):
        value = os.environ.get(f"{protocol}_proxy") or os.environ.get(f"{protocol.upper()}_PROXY")
        if not value:
            continue
        p = urllib.parse.urlparse(value)
        fields = {
            "id": f"env-{protocol}",
            "active": "true",
            "protocol": protocol,
            "host": p.hostname or "",
            "port": str(p.port or (443 if p.scheme == "https" else 80)),
        }
        if p.username:
            fields["username"] = urllib.parse.unquote(p.username)
        if p.password:
            fields["password"] = urllib.parse.unquote(p.password)
        if non_proxy:
            fields["nonProxyHosts"] = non_proxy
        proxy = ET.Element(qualify("proxy"))
        for name, text in fields.items():
            ET.SubElement(proxy, qualify(name)).text = text
        proxies.append(proxy)
    return proxies


def read_settings(source):
    """The settings the lock run uses, and the part of them the lock records.

    The lock run uses the given settings file, or the user's
    ~/.m2/settings.xml. The lock records them without <servers> and
    <proxies>, which hold credentials, and without <localRepository> and
    <offline>, which each run sets itself. The sandboxed build and a
    prefetched Maven use the recorded settings, so their repository and
    mirror ids are the ids of the lock run, which the names of metadata files
    in the local repository depend on.
    """
    if source is None:
        default = Path.home() / ".m2" / "settings.xml"
        source = default if default.is_file() else None
    if source is None:
        return ET.Element("settings"), None
    try:
        settings = ET.parse(source).getroot()
    except (OSError, ET.ParseError) as e:
        fail(f"cannot read the settings file {source}: {e}")
    if local_name(settings.tag) != "settings":
        fail(f"{source} is not a Maven settings file")
    qualifier(settings)
    for child in list(settings):
        if local_name(child.tag) in ("localRepository", "offline"):
            settings.remove(child)
    recorded = copy.deepcopy(settings)
    for child in list(recorded):
        if local_name(child.tag) in ("servers", "proxies"):
            recorded.remove(child)
    return settings, ET.tostring(recorded, encoding="unicode") + "\n"


def write_run_settings(settings, path, legacy=False):
    """Writes the lock run's settings, adding the environment's proxy when they name none."""
    settings = copy.deepcopy(settings)
    qualify = qualifier(settings)

    if not any(local_name(c.tag) == "proxies" for c in settings):
        proxies = env_proxies(qualify)
        if proxies:
            element = ET.SubElement(settings, qualify("proxies"))
            element.extend(proxies)

    # A mirror of Maven Central under the id `central`, so that the names of
    # metadata files stay those of Central. Mirrors the settings declare
    # come first.
    mirror = central_mirror() or (CENTRAL if legacy else None)
    if mirror:
        mirrors = next((c for c in settings if local_name(c.tag) == "mirrors"), None)
        declared = [] if mirrors is None else [m.findtext(qualify("mirrorOf")) or "" for m in mirrors]
        if not any("central" in d.split(",") or d.strip() == "*" for d in declared):
            if mirrors is None:
                mirrors = ET.SubElement(settings, qualify("mirrors"))
            element = ET.SubElement(mirrors, qualify("mirror"))
            for name, text in (("id", "central"), ("mirrorOf", "central"), ("url", mirror.rstrip("/") if legacy else mirror)):
                ET.SubElement(element, qualify(name)).text = text
    path.write_text(ET.tostring(settings, encoding="unicode") + "\n")


def offline_settings(lock_data, repo, path):
    """Writes the settings of a prefetched Maven: the recorded settings, offline, on the locked repository."""
    settings = ET.fromstring(lock_data.get("settings") or "<settings/>")
    qualify = qualifier(settings)
    for child in list(settings):
        if local_name(child.tag) in ("localRepository", "offline"):
            settings.remove(child)
    local = ET.Element(qualify("localRepository"))
    local.text = str(repo)
    offline = ET.Element(qualify("offline"))
    offline.text = "true"
    settings.insert(0, offline)
    settings.insert(0, local)
    path.write_text(ET.tostring(settings, encoding="unicode") + "\n")


def copy_build(root, dest):
    """Copies the build root for a lock run, without build outputs and without the lines of `prefetch`."""

    # Nix's default output links live at the build root. Ordinary source
    # directories such as org.junit.experimental.results must be retained.
    def ignore(directory, names):
        return [n for n in names if n in SKIPPED_DIRS or (
            Path(directory) == Path(root) and n.startswith("result") and (Path(directory) / n).is_symlink()
        )]

    shutil.copytree(root, dest, symlinks=True, ignore=ignore)
    config = dest / ".mvn" / "maven.config"
    if config.is_file() and not config.is_symlink() and PREFETCH_MARKER in config.read_text().split("\n"):
        config.write_text(without_prefetch_lines(config.read_text()))


def read_record(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def artifact_key(install):
    classifier = f"-{install['classifier']}" if install["classifier"] else ""
    extension = f".{install['extension']}" if install["extension"] else ""
    return f"{install['artifactId']}{classifier}{extension}"


def declared_downloads(root):
    """The downloads the build declares in .mvn/maven-purifier.json.

    A plugin that downloads a file itself, outside Maven Resolver, gives the
    recording extension nothing to see. The build declares such a file with
    its URL and its path in the local repository, where such plugins cache
    what they download.
    """
    config = root / ".mvn" / "maven-purifier.json"
    if not config.is_file():
        return []
    try:
        downloads = json.loads(config.read_text()).get("downloads", [])
    except json.JSONDecodeError as e:
        fail(f"cannot read {config}: {e}")
    for d in downloads:
        if not isinstance(d, dict) or not d.get("url") or not d.get("path"):
            fail(f"each entry of `downloads` in {config} needs `url` and `path`")
        if Path(d["path"]).is_absolute() or ".." in Path(d["path"]).parts:
            fail(f"the path {d['path']} in {config} must be relative to the local repository")
    return downloads


def tracked_repository(path):
    """The repository id that Maven Resolver's tracking file names for a file, or None."""
    tracking = path.parent / "_remote.repositories"
    if not tracking.exists():
        tracking = path.parent / "_maven.repositories"
    if not tracking.is_file():
        return None
    for line in tracking.read_text().splitlines():
        if line.startswith("#"):
            continue
        name, sep, rest = line.partition(">")
        if sep and name == path.name:
            # Maven Resolver 2 writes the repository key as the id followed
            # by a digest of the repository, Maven Resolver 1 as the id.
            repository = re.sub(r"-[0-9a-f]{40}$", "", rest.rstrip("=").strip())
            if repository:
                return repository
    return None


class Options:
    """The options of a command: --lockfile, the flags it names, and the rest in order."""

    def __init__(self, args, flags=(), values=(), repeatable=()):
        # MAVEN_PURIFIER_LOCKFILE names the lockfile of an external build,
        # such as the lockFile a dev shell passes to buildMavenProject;
        # --lockfile takes precedence over it.
        variable = os.environ.get("MAVEN_PURIFIER_LOCKFILE")
        self.lockfile = Path(variable).resolve() if variable else None
        self.flags = set()
        self.values = {}
        self.rest = []
        it = iter(args)
        for arg in it:
            if arg == "--lockfile":
                self.lockfile = Path(next(it, "")).resolve()
            elif arg in flags:
                self.flags.add(arg)
            elif arg in repeatable:
                value = next(it, None)
                if value is None:
                    fail(f"{arg} needs a value")
                self.values.setdefault(arg, []).append(value)
            elif arg in values:
                value = next(it, None)
                if value is None:
                    fail(f"{arg} needs a value")
                self.values[arg] = value
            else:
                self.rest.append(arg)

    def lock_path(self, root):
        return self.lockfile or root / LOCK_NAME


def read_lock(lock_path):
    """The lockfile, checked: it is JSON, of this format version, and matches its lockHash."""
    fix = "run `maven-purifier lock --force` to write it again"
    if not lock_path.is_file():
        fail(f"there is no lockfile at {lock_path}; run `maven-purifier lock` first")
    try:
        lock_data = json.loads(lock_path.read_text())
    except json.JSONDecodeError as e:
        fail(f"{lock_path} is not valid JSON ({e}); {fix}")
    if not isinstance(lock_data, dict):
        fail(f"{lock_path} is not a lockfile that maven-purifier wrote; {fix}")
    if lock_data.get("version") != FORMAT_VERSION:
        fail(f"{lock_path} has lock format version {lock_data.get('version')}, and this maven-purifier reads version {FORMAT_VERSION}; {fix}")
    if lock_data.get("lockHash") != lock_hash(lock_data):
        fail(f"{lock_path} does not match its lockHash, so it was changed after `maven-purifier lock` wrote it; {fix}")
    return lock_data


def check_fresh(root, lock_path, lock_data):
    if lock_data.get("definitionHash") != definition_hash(root, lock_data.get("goals", [])):
        fail(
            f"the build definition (a pom.xml, .mvn/ or the goals) changed since {lock_path} was made; "
            "run `maven-purifier lock` to lock it again"
        )


def run_maven(mvn, project, env, settings, repo, goals):
    legacy = maven_version_tuple(mvn) < (3, 1, 0)
    if legacy:
        seed_legacy_recorder(repo)
    command = [str(mvn), "-B", "-s", str(settings), f"-Dmaven.repo.local={repo}", f"-Dmaven-purifier.root={project}"]
    if maven_version_tuple(mvn) >= (3, 0, 0):
        command.append(f"-Dmaven.ext.class.path={os.environ['MAVEN_PURIFIER_EXTENSION']}")
    command += [*MAVEN_FLAGS, *goals]
    if legacy:
        command.append(LEGACY_RECORDER)
    result = subprocess.run(command, cwd=project, env=env, check=False).returncode
    if result == 0 and legacy:
        record_legacy_downloads(repo, Path(env["MAVEN_PURIFIER_RECORD"]), json.loads(env.get("MAVEN_PURIFIER_PRESERVED", "[]")))
    return result


def previous_lock(lock_path):
    """The previous lockfile, when it is one this maven-purifier reads and matches its lockHash, or None."""
    try:
        data = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("version") == FORMAT_VERSION and data.get("lockHash") == lock_hash(data):
        return data
    return None


def package_prefix(package):
    """The local repository directory of a package named groupId:artifactId."""
    group, sep, artifact = package.partition(":")
    if not sep or not group or not artifact or ":" in artifact:
        fail(f"name a package as groupId:artifactId, such as org.slf4j:slf4j-api, not {package!r}")
    return f"{group.replace('.', '/')}/{artifact}/"


def seed_metadata(repo, previous, refresh):
    """Lays the metadata files of the previous lock into the empty local repository of a lock run.

    Each is marked as checked now in resolver-status.properties, so that
    Maven Resolver reads it rather than fetching the repository's current
    one, and version ranges and SNAPSHOTs resolve as the previous lock run
    resolved them. `refresh` is None to seed every file, "all" to seed none,
    or a list of groupId:artifactId whose files are left out. Returns the
    paths it seeded.
    """
    if previous is None or refresh == "all":
        return set()
    prefixes = [package_prefix(p) for p in (refresh or [])]
    now = int(time.time() * 1000)
    seeded = set()
    for entry in previous["downloads"]:
        if "contents" not in entry or any(entry["path"].startswith(p) for p in prefixes):
            continue
        target = repo / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["contents"])
        with open(target.parent / "resolver-status.properties", "a") as status:
            status.write(f"{target.name}.lastUpdated={now}\n")
        seeded.add(entry["path"])
    return seeded


def lock(args):
    options = Options(args, flags=("--force",), values=("--settings",), repeatable=("--input",))
    root = build_root()
    run_lock(
        root,
        options.lock_path(root),
        options.rest or ["clean", "install"],
        options.values.get("--settings"),
        force="--force" in options.flags,
        refresh=None,
        input_refs=options.values.get("--input"),
    )


def update(args):
    options = Options(args, values=("--settings",), repeatable=("--input",))
    root = build_root()
    lock_path = options.lock_path(root)
    previous = read_lock(lock_path)
    run_lock(
        root,
        lock_path,
        previous["goals"],
        options.values.get("--settings"),
        force=True,
        refresh=options.rest or "all",
        input_refs=options.values.get("--input"),
    )


def run_lock(root, lock_path, goals, settings_file, force, refresh, input_refs=None):
    """The lock run. `refresh` says which metadata of the previous lock to resolve afresh (seed_metadata)."""
    def_hash = definition_hash(root, goals)
    settings_source = Path(settings_file).resolve() if settings_file else None
    settings, settings_record = read_settings(settings_source)
    previous = previous_lock(lock_path)
    previous_inputs = (previous or {}).get("projectInputs", [])
    if input_refs is None:
        input_refs = [entry["reference"] for entry in previous_inputs]
    inputs = []
    if input_refs:
        import project_inputs
        inputs = project_inputs.pin(input_refs, previous_inputs, refresh=refresh == "all")
    if not force and lock_path.is_file():
        try:
            old = json.loads(lock_path.read_text())
        except json.JSONDecodeError:
            old = {}
        if (
            isinstance(old, dict)
            and old.get("version") == FORMAT_VERSION
            and old.get("lockHash") == lock_hash(old)
            and old.get("definitionHash") == def_hash
            and old.get("settings") == settings_record
            and old.get("projectInputs", []) == inputs
        ):
            info(f"{lock_path} is up to date; pass --force to lock again")
            return

    # Capture and relock both use the caller's existing runtime. javaMajor
    # records that observation; it is not a local SDK selection table.
    home = java_home()
    with tempfile.TemporaryDirectory(prefix="maven-purifier-lock-") as tmp:
        tmp = Path(tmp)
        url = distribution_url(root)
        info(f"fetching {fetch_url(url)}")
        dist_hash, dist_store = prefetch_file(url)
        dist_path = download_path(url)
        mvn = unpack_maven(Path(dist_store), tmp / "maven")
        maven_version = mvn.parent.parent.name.removeprefix("apache-maven-")
        if inputs and maven_version_tuple(mvn) < (3, 0, 0):
            fail("native Nix project inputs currently need Maven 3 or newer; "
                 "older Maven builds can use the Nix builder's projectDependencies argument")
        project = tmp / "project"
        copy_build(root, project)
        repo = tmp / "repository"
        repo.mkdir()
        declared = []
        for d in declared_downloads(root):
            info(f"fetching {fetch_url(d['url'])}")
            declared_hash, declared_store = prefetch_file(d["url"])
            target = repo / d["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(declared_store, target)
            declared.append(file_entry(Path(d["path"]).as_posix(), d["url"], declared_hash))
        provided_paths = set()
        if inputs:
            import project_inputs
            import jvm_repository
            state = project / STATE_DIR
            state.mkdir(exist_ok=True)
            selections = {}
            for view in project_inputs.maven_views(inputs):
                # Catalogs discover native coordinates; Nix identifies their
                # provider. No caller-supplied version rewrites are involved.
                jvm_repository.merge(view["repository"], repo)
                provided_paths.update(path.relative_to(view["repository"]).as_posix()
                                      for path in view["repository"].rglob("*") if path.is_file())
                for module in json.loads(view["catalog"].read_text())["modules"]:
                    key = module["group"] + ":" + module["name"]
                    if key in selections and selections[key] != module["version"]:
                        fail(f"conflicting Nix providers for {key}")
                    selections[key] = module["version"]
            lines = [key.replace(":", "\\:") + "=" + version + "\n" for key, version in sorted(selections.items())]
            (state / "project-inputs.properties").write_text("".join(lines))
        declared_paths = {d["path"] for d in declared}
        seeded = seed_metadata(repo, previous, refresh)
        seeded.update(javadoc_resources.seed(previous, repo, refresh == "all"))
        (tmp / "home").mkdir()
        write_run_settings(settings, tmp / "settings.xml", legacy=maven_version_tuple(mvn) < (3, 1, 0))
        record = tmp / "record.jsonl"

        env = dict(os.environ)
        env.update({"JAVA_HOME": home, "HOME": str(tmp / "home"), "MAVEN_PURIFIER_RECORD": str(record),
                    "MAVEN_PURIFIER_PRESERVED": json.dumps(sorted(declared_paths | seeded | provided_paths))})
        # Plugins that write temporary files write them into the lock run's
        # own directory.
        (tmp / "java-tmp").mkdir()
        maven_opts = [env.get("MAVEN_OPTS", ""), f"-Duser.home={tmp / 'home'}", f"-Djava.io.tmpdir={tmp / 'java-tmp'}"]
        if proxy_with_credentials():
            # Java disables Basic authentication to a proxy by default, for
            # HTTPS tunnels and plain requests alike. Maven 3.9's Apache
            # HttpClient transport also fails with 407 on concurrent
            # downloads through a proxy that asks for credentials, so the
            # lock run downloads one file at a time.
            maven_opts += [
                "-Djdk.http.auth.tunneling.disabledSchemes=",
                "-Djdk.http.auth.proxying.disabledSchemes=",
                "-Dmaven.artifact.threads=1",
            ]
        env["MAVEN_OPTS"] = " ".join(filter(None, maven_opts))

        info(f"running Maven {maven_version} on a copy of the build: {' '.join(goals)}")
        if run_maven(mvn, project, env, tmp / "settings.xml", repo, goals) != 0:
            fail(f"the lock run failed; {lock_path} is unchanged")
        build_events = len(read_record(record)) if record.exists() else 0
        try:
            documentation = javadoc_resources.capture(read_record(record)[:build_events], repo, previous, refresh == "all")
        except (OSError, ValueError) as error:
            fail(f"cannot lock Javadoc metadata: {error}; {lock_path} is unchanged")

        # The development pass: the plugins of the build and the source jars
        # of its dependencies, with the locked Maven, and the default plugins
        # of the Maven inside the editor, with that Maven. What it adds is
        # marked as development: `prefetch` fetches it, and a sandboxed build
        # leaves it out. A failure here costs the lock nothing the build
        # needs, so it is reported and passed over.
        dependency_plugin = "org.apache.maven.plugins:maven-dependency-plugin:2.8" if maven_version_tuple(mvn) < (3, 6, 3) else DEPENDENCY_PLUGIN
        development = [f"{dependency_plugin}:resolve-plugins", f"{dependency_plugin}:sources"]
        info("adding the build's plugins and its dependencies' source jars for development")
        if run_maven(mvn, project, env, tmp / "settings.xml", repo, development) != 0:
            info("the development pass with the locked Maven failed; the lock leaves out what it would have added")
        editor_maven = os.environ.get("MAVEN_PURIFIER_EDITOR_MAVEN")
        editor_dist = None
        if editor_maven and editor_maven != maven_version:
            info(f"adding the default plugins of Maven {editor_maven}, which the editor runs")
            editor_hash, editor_store = prefetch_file(maven_url(editor_maven))
            editor_dist = file_entry(download_path(maven_url(editor_maven)), maven_url(editor_maven), editor_hash)
            editor_mvn = unpack_maven(Path(editor_store), tmp / "editor-maven")
            if run_maven(editor_mvn, project, env, tmp / "settings.xml", repo, [f"{DEPENDENCY_PLUGIN}:resolve-plugins"]) != 0:
                info(f"the development pass with Maven {editor_maven} failed; the editor may miss plugins offline")

        events = read_record(record) if record.exists() else []
        downloads = {}
        development_paths = set()
        repository_urls = {}
        for index, e in enumerate(events):
            if e["event"] != "download":
                continue
            rel = Path(e["file"]).relative_to(repo).as_posix()
            if rel in provided_paths:
                continue
            found = [rel]
            # Maven Resolver stores a timestamped SNAPSHOT under its base
            # version, which the event names, and also under the name it has
            # in the remote repository. Both copies are the same file.
            remote_name = e["url"].rsplit("/", 1)[-1]
            sibling = Path(rel).parent / remote_name
            if remote_name != Path(rel).name and (repo / sibling).is_file():
                found.append(sibling.as_posix())
            for path in found:
                if path not in downloads:
                    downloads[path] = e["url"]
                    if index >= build_events:
                        development_paths.add(path)
            repository_urls.setdefault(e["repository"], e["repositoryUrl"])

        # A seeded metadata file stays in the lock while the build reads it.
        read_metadata = {Path(e["file"]).relative_to(repo).as_posix() for e in events if e["event"] == "resolved"}
        for path in sorted(seeded):
            if path in read_metadata and path not in downloads and (repo / path).is_file():
                downloads[path] = None

        installs = [e for e in events if e["event"] == "install"]
        installed = {Path(e["file"]).relative_to(repo).as_posix() for e in installs}
        # The version and artifact directories of the build's own modules,
        # where installing and deploying write metadata files of their own.
        module_dirs = set()
        for rel in installed:
            version_dir = Path(rel).parent
            module_dirs.update({version_dir.as_posix(), version_dir.parent.as_posix()})

        unrecorded = []
        for path in sorted(repo.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(repo).as_posix()
            if path.name in BOOKKEEPING or path.name.endswith(".lastUpdated"):
                continue
            if rel.startswith(LEGACY_RECORDER_PATH) or rel in downloads or rel in installed or rel in declared_paths or rel in seeded or rel in provided_paths:
                continue
            if path.name.startswith("maven-metadata") and Path(rel).parent.as_posix() in module_dirs:
                continue
            if generated_obr_index(path, repo, installed):
                continue
            _, dot, suffix = rel.rpartition(".")
            if dot and f".{suffix}" in CHECKSUM_SUFFIXES:
                continue
            # Maven resolves core extensions (.mvn/extensions.xml) before
            # it loads the recording extension. Maven Resolver still tracks
            # which repository each of those files came from, and a release
            # artifact has the same path in the remote repository as in the
            # local one.
            repository = tracked_repository(path)
            if repository is not None and repository in repository_urls and "SNAPSHOT" not in rel:
                base_url = str(repository_urls[repository])
                downloads[rel] = (base_url if base_url.endswith("/") else base_url + "/") + rel
                continue
            unrecorded.append(rel)
        if unrecorded:
            listing = "\n  ".join(unrecorded)
            fail(
                "these files reached the local repository without a recorded download, "
                f"so a sandboxed build could not have them:\n  {listing}\n"
                "Declare each one that a plugin downloads itself in .mvn/maven-purifier.json"
            )

        # A metadata file lists the versions a repository holds, and its URL
        # serves new contents whenever a version is published, so a fetch
        # pinned by hash would break. The lock holds the contents the
        # resolver read. Every other download is immutable.
        entries: list[dict] = [file_entry(dist_path, url, dist_hash), *declared, *documentation]
        if editor_dist is not None and editor_dist["path"] != dist_path:
            entries.append(dict(editor_dist, development=True))
        for rel, src in sorted(downloads.items()):
            if rel in declared_paths:
                continue
            if Path(rel).name.startswith("maven-metadata"):
                entry = {"path": rel, "contents": (repo / rel).read_text()}
            else:
                entry = file_entry(rel, src, sri(repo / rel))
            if rel in development_paths:
                entry["development"] = True
            entries.append(entry)
        # A native binary that the build downloaded by the platform
        # classifier of this system is needed on this system only, and each
        # other system needs its own build of it, which the lock run could
        # not download by running the build. The lock fetches those by the
        # same name with the other classifier, and marks every such entry
        # with the systems that need it.
        native = native_system()
        for entry in list(entries):
            counterparts = platform_counterparts(entry["path"], native) if "url" in entry else {}
            if not counterparts:
                continue
            found = {}
            for system, path in counterparts.items():
                url = entry["url"][: -len(entry["path"].rpartition("/")[2])] + path.rpartition("/")[2]
                try:
                    out = subprocess.run(
                        ["nix", "store", "prefetch-file", "--json", "--hash-type", "sha256", fetch_url(url)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError:
                    info(f"{entry['path']} has no build for {system} at {url}; a build there will miss it")
                    continue
                found[system] = file_entry(path, url, json.loads(out.stdout)["hash"])
            if found:
                entry["systems"] = [native]
                for system, other in found.items():
                    other["systems"] = [system]
                    if entry.get("development"):
                        other["development"] = True
                    entries.append(other)
        entries.sort(key=lambda d: str(d["path"]))

        projects = {}
        for e in installs:
            key = (e["groupId"], e["artifactId"])
            p = projects.setdefault(
                key,
                {"groupId": e["groupId"], "artifactId": e["artifactId"], "version": e["version"], "artifacts": {}},
            )
            p["artifacts"][artifact_key(e)] = {
                "key": artifact_key(e),
                "classifier": e["classifier"],
                "extension": e["extension"],
                "path": Path(e["file"]).relative_to(repo).as_posix(),
            }
        names = {}
        for g, a in projects:
            names.setdefault(a, []).append(g)
        clashes = {a: gs for a, gs in names.items() if len(gs) > 1}
        if clashes:
            fail(f"several modules share an artifactId, which names a package: {clashes}")

        result = {
            "version": FORMAT_VERSION,
            "maven": {"version": maven_version},
            "javaMajor": java_major(java_version(home)),
            "goals": goals,
            "settings": settings_record,
            "definitionHash": def_hash,
            "projects": [
                dict(p, artifacts=sorted(p["artifacts"].values(), key=lambda a: a["key"]))
                for _, p in sorted(projects.items())
            ],
            "downloads": entries,
        }
        if inputs:
            result["projectInputs"] = inputs
        result["lockHash"] = lock_hash(result)
        lock_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        modules = ", ".join(sorted(a for _, a in projects))
        dev = sum(1 for d in entries if d.get("development"))
        info(f"wrote {lock_path} with {len(entries)} downloads, {dev} of them for development; the build installs {modules}")


def nix_expression(attribute):
    """A Nix expression for an attribute of this flake's buildMavenProject, for the build root and lockfile in the environment."""
    if attribute == "prefetch":
        return """
          let
            flake = builtins.getFlake ("path:" + builtins.getEnv "MAVEN_PURIFIER_FLAKE");
            pkgs = import flake.inputs.nixpkgs {
              system = builtins.currentSystem;
              overlays = [ flake.overlays.default ];
            };
            inputs = pkgs.callPackage (flake.outPath + "/pkgs/build-maven-project/locked-inputs.nix") {
              inherit (flake.lib) purifier;
            };
          in (inputs {
            src = /. + builtins.getEnv "MAVEN_PURIFIER_ROOT";
            lockFile = /. + builtins.getEnv "MAVEN_PURIFIER_LOCKFILE";
          }).prefetch
        """
    return f"""
      let
        flake = builtins.getFlake ("path:" + builtins.getEnv "MAVEN_PURIFIER_FLAKE");
        pkgs = import flake.inputs.nixpkgs {{
          system = builtins.currentSystem;
          overlays = [ flake.overlays.default ];
        }};
      in
      (pkgs.maven-purifier.buildMavenProject {{
        src = /. + builtins.getEnv "MAVEN_PURIFIER_ROOT";
        lockFile = /. + builtins.getEnv "MAVEN_PURIFIER_LOCKFILE";
      }}).{attribute}
    """


def nix_build(root, lock_path, attribute, out_link):
    env = dict(os.environ, MAVEN_PURIFIER_ROOT=str(root), MAVEN_PURIFIER_LOCKFILE=str(lock_path))
    try:
        return subprocess.run(
            ["nix", "build", "--impure", "--out-link", str(out_link), "--print-out-paths", "--expr", nix_expression(attribute)],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        fail(f"Nix could not build what {lock_path} lists")


def write_maven_config(root, lines):
    """Replaces the lines of `prefetch` at the end of .mvn/maven.config with lines, keeping the build's own."""
    config = root / ".mvn" / "maven.config"
    own = without_prefetch_lines(config.read_text()) if config.is_file() else ""
    config.parent.mkdir(exist_ok=True)
    config.write_text(own + "\n".join([PREFETCH_MARKER, *lines]) + "\n")


def prefetch(args):
    options = Options(args, values=("--prefetched",))
    if options.rest:
        fail(f"`maven-purifier prefetch` takes no argument {options.rest[0]!r}")
    root = build_root()
    lock_path = options.lock_path(root)
    lock_data = read_lock(lock_path)
    check_fresh(root, lock_path, lock_data)
    host_java = java_home()
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)

    # `--prefetched` names a prefetch that is built already, as the check of
    # the offline loop passes one built in the sandbox.
    if "--prefetched" in options.values:
        prefetched = Path(options.values["--prefetched"]).resolve()
    else:
        info(f"fetching what {lock_path.name} lists into the Nix store")
        prefetched = Path(nix_build(root, lock_path, "prefetch", state / "prefetch"))

    # Maven writes tracking files next to what it reads and installs into
    # the local repository, and Maven 3.10 treats a file as absent when its
    # real path differs from its path in the repository, so the repository
    # is a writable copy of the one in the store.
    repo = state / "repository"
    stamp = state / "repository.source"
    source = str((prefetched / "repository").resolve())
    if not (stamp.exists() and stamp.read_text() == source and repo.is_dir()):
        if repo.exists():
            shutil.rmtree(repo)
        shutil.copytree(source, repo, symlinks=False, copy_function=shutil.copyfile)
        # copytree copies the read-only mode of each directory in the store,
        # which leaves no room for Maven's lock and tracking files.
        for directory, _, _ in os.walk(repo):
            os.chmod(directory, 0o755)
        stamp.write_text(source)

    offline_settings(lock_data, repo, state / "settings.xml")
    # The core model adapters discover this file from each original POM,
    # including IDE builds. The generated metadata remains rooted in Nix.
    selection = state / "project-inputs.properties"
    if selection.exists() or selection.is_symlink():
        selection.unlink()
    if (prefetched / "project-inputs.properties").is_file():
        selection.symlink_to(prefetched / "project-inputs.properties")
    bin_dir = state / "bin"
    bin_dir.mkdir(exist_ok=True)
    jdk = state / "jdk"
    if jdk.is_symlink() or jdk.exists():
        jdk.unlink()
    jdk.symlink_to(host_java)
    # Maven before 3.3.1 ignores .mvn/maven.config. Pass the locked settings
    # explicitly. The caller's runtime remains in control of local builds.
    launcher = bin_dir / "mvn"
    if launcher.is_symlink():
        launcher.unlink()
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(prefetched / 'maven' / 'bin' / 'mvn'))} "
        f"-Dmaven.ext.class.path={shlex.quote(os.environ['MAVEN_PURIFIER_EXTENSION'])} "
        "-Dmaven-purifier.replay=true "
        f"-Dmaven-purifier.root={shlex.quote(str(root))} "
        f"-o --settings {shlex.quote(str(state / 'settings.xml'))} \"$@\"\n"
    )
    launcher.chmod(0o755)

    write_maven_config(
        root,
        [
            f"--settings={state / 'settings.xml'}",
            f"-Dmaven.ext.class.path={os.environ['MAVEN_PURIFIER_EXTENSION']}",
            f"-Dmaven-purifier.maven={lock_data['maven']['version']}",
            "-Dmaven-purifier.replay=true",
            f"-Dmaven-purifier.root={root}",
            *MAVEN_FLAGS,
        ],
    )
    info(
        f"Maven resolves offline from {repo} now. The locked Maven {lock_data['maven']['version']} is "
        f"{state / 'bin' / 'mvn'}. Local builds use your existing Java runtime; "
        f"the runtime found during prefetch is {host_java}."
    )


def distribution_target(root, lock_data):
    """The repository id and URL of the root POM's distributionManagement."""
    try:
        pom = ET.parse(root / "pom.xml").getroot()
    except ET.ParseError as e:
        fail(f"cannot read {root / 'pom.xml'}: {e}")
    snapshot = any(p["version"].endswith("-SNAPSHOT") for p in lock_data["projects"])
    wanted = "snapshotRepository" if snapshot else "repository"
    for management in pom:
        if local_name(management.tag) != "distributionManagement":
            continue
        for repository in management:
            if local_name(repository.tag) != wanted:
                continue
            fields = {local_name(c.tag): (c.text or "").strip() for c in repository}
            if fields.get("id") and fields.get("url"):
                return fields["id"], fields["url"]
    fail(f"the root pom.xml declares no <distributionManagement><{wanted}>; pass --repository-id and --url")


def publish(args):
    options = Options(args, values=("--repository-id", "--url"))
    root = build_root()
    lock_path = options.lock_path(root)
    lock_data = read_lock(lock_path)
    check_fresh(root, lock_path, lock_data)
    repository_id = options.values.get("--repository-id")
    url = options.values.get("--url")
    if url is None or repository_id is None:
        repository_id, url = distribution_target(root, lock_data)
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    info("building the release with Nix")
    script = Path(nix_build(root, lock_path, "release.publish", state / "publish")) / "bin" / "maven-purifier-publish"
    env = dict(os.environ)
    env.pop("JAVA_HOME", None)
    os.execve(str(script), [str(script), "--repository-id", repository_id, "--url", url, *options.rest], env)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    verb, args = sys.argv[1], sys.argv[2:]
    commands = {"lock": lock, "update": update, "prefetch": prefetch, "publish": publish}
    if verb not in commands:
        fail(f"unknown command {verb!r}; the commands are `lock`, `update`, `prefetch` and `publish`")
    commands[verb](args)


if __name__ == "__main__":
    main()
