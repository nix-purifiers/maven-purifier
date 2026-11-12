# SPDX-License-Identifier: MIT
"""Snapshot the metadata used by native Javadoc links into ordinary lock entries.

Documentation endpoints change independently of Maven releases. Their UTF-8
contents therefore use the lock's existing snapshot representation, just like
mutable Maven metadata. The private index scopes native offlineLinks by reactor
project and execution; it is not a new lock field or public builder argument.
"""
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import shlex
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

PREFIX = "downloads/javadoc/"
INDEX = PREFIX + "index.xml"


def base(url):
    return url.rstrip("/")


def remote(url):
    return url.startswith(("https://", "http://"))


def index(entries):
    entry = next((e for e in entries if e["path"] == INDEX), None)
    return ET.fromstring(entry["contents"]) if entry else ET.Element("javadoc", version="1")


def seed(previous, repository, refresh=False):
    paths = set()
    if previous is not None and not refresh:
        for entry in previous["downloads"]:
            if entry["path"].startswith(PREFIX):
                target = repository / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry["contents"], encoding="utf-8")
                paths.add(entry["path"])
    return paths


def arguments(options):
    lexer = shlex.shlex(options, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escapedquotes = "\"'"
    return list(lexer)


class PlatformLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bases = set()

    def handle_starttag(self, tag, attributes):
        if tag != "a":
            return
        attrs = dict(attributes)
        href = attrs.get("href", "")
        classes = attrs.get("class", "").replace("-", "").lower()
        if "externallink" not in classes and "is-external=true" not in href:
            return
        match = re.match(r"^(https?://.+?)/(?:java\.base/)?java/lang/[^?#]+\.html(?:[?#].*)?$", href)
        if match:
            self.bases.add(base(match[1]))


def platform_links(directory):
    parser = PlatformLinks()
    if directory and Path(directory).is_dir():
        for html in sorted(Path(directory).rglob("*.html")):
            data = html.read_bytes()
            charset = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", data[:4096], re.IGNORECASE)
            encoding = charset[1].decode("ascii") if charset else "utf-8"
            parser.feed(data.decode(encoding))
            if parser.bases:
                break
    return parser.bases


def metadata(url):
    files = []
    errors = []
    for name in ("element-list", "package-list"):
        source = base(url) + "/" + name
        try:
            with urllib.request.urlopen(source, timeout=30) as response:
                content = response.read().decode("utf-8")
            if not content.strip() or "<html" in content.lower() or "<!doctype" in content.lower():
                raise ValueError("response is not a Javadoc package or element list")
            files.append((name, source, content))
        except (urllib.error.HTTPError, UnicodeError, ValueError) as error:
            errors.append(str(error))
    if not files:
        raise ValueError("cannot snapshot Javadoc metadata for " + url + ": " + "; ".join(errors))
    return files


def capture(events, repository, previous=None, refresh=False):
    """Observe completed native Javadoc invocations, preserving pinned snapshots."""
    old_entries = {e["path"]: e for e in (previous or {}).get("downloads", [])}
    old = index(old_entries.values()) if not refresh else ET.Element("javadoc", version="1")
    old_links = {}
    for project in old.findall("project"):
        for execution in project.findall("execution"):
            for link in execution.findall("link"):
                old_links[(project.get("id"), execution.get("id"), link.get("url"))] = link
    requested = {}
    for event in events:
        if event["event"] == "javadoc-error":
            raise ValueError("cannot observe native Javadoc inputs: " + event["message"])
        if event["event"] != "javadoc":
            continue
        tokens = arguments(event["options"])
        links = requested.setdefault((event["project"], event["execution"]), set())
        output = None
        supplied = set()
        for i, token in enumerate(tokens):
            if token == "-link" and i + 1 < len(tokens) and remote(tokens[i + 1]):
                links.add((base(tokens[i + 1]), base(tokens[i + 1])))
            elif token == "-linkoffline" and i + 2 < len(tokens):
                public, location = base(tokens[i + 1]), base(tokens[i + 2])
                if remote(location):
                    links.add((public, location))
                else:
                    pinned = old_links.get((event["project"], event["execution"], public))
                    if pinned is not None and Path(location) == repository / pinned.get("directory"):
                        links.add((public, pinned.get("metadata")))
                    else:
                        # Native plugin resources (such as its bundled Java API
                        # package list) are already reproduced by locked artifacts.
                        supplied.add(public)
            elif token == "-d" and i + 1 < len(tokens):
                output = tokens[i + 1]
        links.update((url, url) for url in platform_links(output) - supplied)
    snapshots = {}
    document = ET.Element("javadoc", version="1")
    projects = {}
    for (project, execution), links in sorted(requested.items()):
        if not links:
            continue
        project_node = projects.get(project)
        if project_node is None:
            project_node = ET.SubElement(document, "project", id=project)
            projects[project] = project_node
        execution_node = ET.SubElement(project_node, "execution", id=execution)
        for public, source in sorted(links):
            directory = PREFIX + hashlib.sha256(source.encode("utf-8")).hexdigest()
            link = ET.SubElement(execution_node, "link", url=public, metadata=source, directory=directory)
            pinned = [] if refresh else [old_entries[directory + "/" + name]
                for name in ("element-list", "package-list") if directory + "/" + name in old_entries]
            files = [(Path(e["path"]).name, source + "/" + Path(e["path"]).name, e["contents"]) for e in pinned]
            if not files:
                existing = [e for path, e in snapshots.items() if path.startswith(directory + "/")]
                files = [(Path(e["path"]).name, source + "/" + Path(e["path"]).name, e["contents"]) for e in existing]
            if not files:
                files = metadata(source)
            for name, url, content in files:
                path = directory + "/" + name
                snapshots[path] = {"path": path, "contents": content}
                ET.SubElement(link, "source", name=name, url=url,
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())
    if not snapshots:
        return []
    snapshots[INDEX] = {"path": INDEX, "contents": ET.tostring(document, encoding="unicode") + "\n"}
    return [snapshots[path] for path in sorted(snapshots)]
