# SPDX-License-Identifier: MIT
"""Private, persistent Maven views with one source-config activation point."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

FORMAT = 1
MARKER = "-Dmaven-purifier.prefetched=true"
SHIM_MARKER = "# maven-purifier-launcher-v1"


class StateError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _owned_directory(path, private=False):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o022)):
        raise StateError(f"state directory has unsafe ownership, type or permissions: {path}")


def _private_child(parent, name):
    path = parent / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _owned_directory(path, private=True)
    return path


def namespace(root):
    root = Path(root).resolve(strict=True)
    configured = Path(os.environ.get("XDG_STATE_HOME", ""))
    base = configured if configured.is_absolute() else Path.home() / ".local/state"
    base = base.resolve()
    if base.is_relative_to(root):
        raise StateError("prefetch state must be outside the canonical project")
    base.mkdir(parents=True, exist_ok=True)
    _owned_directory(base)
    for name in ("maven-purifier", "v1", digest(os.fsencode(str(root)))):
        base = _private_child(base, name)
    return base


@contextlib.contextmanager
def project_lock(root, exclusive):
    # The directory inode supplies a lock domain independent of XDG settings.
    descriptor = os.open(Path(root).resolve(strict=True), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


def check_project_directory(root, descriptor):
    observed = Path(root).stat()
    locked = os.fstat(descriptor)
    if (observed.st_dev, observed.st_ino) != (locked.st_dev, locked.st_ino):
        raise StateError("the project directory changed during this operation")


def operation(root, intent):
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    parent = _private_child(namespace(root), digest(encoded))
    parent = _private_child(parent, "operations")
    # This is already the FINAL path. Nix roots and their parents never move.
    return Path(tempfile.mkdtemp(prefix="op-", dir=parent))


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temporary = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def split_config(data):
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip(b"\r\n") == MARKER.encode():
            return b"".join(lines[:index]), b"".join(lines[index:])
    return data, b""


def config_bytes(prefix, lines):
    suffix = ("\n".join([MARKER, *lines]) + "\n").encode()
    separator = b"\n" if prefix and not prefix.endswith(b"\n") else b""
    return prefix + separator + suffix, suffix


def active_generation(root):
    path = Path(root) / ".mvn/maven.config"
    _, suffix = split_config(path.read_bytes())
    prefix = "-Dmaven-purifier.state="
    selected = [line[len(prefix):] for line in suffix.decode().splitlines() if line.startswith(prefix)]
    if len(selected) != 1:
        raise StateError("prefetch has not activated exactly one external Maven view")
    generation = Path(selected[0])
    if not generation.is_absolute():
        raise StateError("the activated Maven view must be absolute")
    return generation, suffix


def store_target(path):
    resolved = Path(path).resolve(strict=True)
    if len(resolved.parts) >= 4 and resolved.parts[:3] == ("/", "nix", "store"):
        return Path(*resolved.parts[:4])
    return None


def anchor(operation_path, path, role):
    target = store_target(path)
    if target is None:
        return {"role": role, "target": str(Path(path).resolve()), "lifetime": "provider-owned"}
    roots = _private_child(operation_path, "roots")
    root = roots / target.name
    if root.exists() or root.is_symlink():
        if not root.is_symlink() or root.resolve() != target:
            raise StateError(f"an existing GC root has another target: {root}")
    else:
        subprocess.run(["nix-store", "--realise", str(target), "--add-root", str(root)],
                       check=True, stdout=subprocess.PIPE, text=True)
    return {"role": role, "target": str(target), "root": str(root), "lifetime": "registered-actual-target"}


def reject_legacy(root):
    state = Path(root) / ".maven-purifier"
    for name in ("repository", "repository.source", "settings.xml", "toolchains.xml",
                 "project-inputs.properties", "prefetch", "jdk"):
        path = state / name
        if path.exists() or path.is_symlink():
            raise StateError(f"legacy generated state needs explicit migration or a fresh checkout: {path}")


def install_shim(root, interpreter, helper):
    parent = Path(root)
    for name in (".maven-purifier", "bin"):
        parent = parent / name
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _owned_directory(parent)
    path = Path(root) / ".maven-purifier/bin/mvn"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or SHIM_MARKER not in path.read_text().splitlines():
            raise StateError(f"refusing to replace an existing or incompatible Maven launcher: {path}")
        lines = path.read_text().splitlines()
        try:
            recorded = json.loads(next(line[len("# launch-helper "):] for line in lines
                                       if line.startswith("# launch-helper ")))
        except (ValueError, StopIteration) as error:
            raise StateError("existing Maven launcher has no valid helper association") from error
        if recorded.get("format") != FORMAT:
            raise StateError("existing Maven launcher needs explicit format migration")
        if path.read_bytes() != shim_bytes(root, recorded):
            raise StateError("existing Maven launcher has drifted")
        if (store_target(recorded["helper"]) is None or store_target(recorded["interpreter"]) is None
                or file_digest(recorded["helper"]) != recorded["helperHash"]):
            raise StateError("existing Maven launcher helper has drifted or disappeared")
        return path, recorded
    if store_target(helper) is None or store_target(interpreter) is None:
        raise StateError("prefetch needs its immutable packaged launch helper and interpreter")
    recorded = {"format": FORMAT, "interpreter": str(interpreter), "helper": str(helper),
                "helperHash": file_digest(helper)}
    atomic_write(path, shim_bytes(root, recorded), 0o700)
    return path, recorded


def shim_bytes(root, recorded):
    import shlex
    script = ("#!/bin/sh\n# SPDX-License-Identifier: MIT\n" + SHIM_MARKER + "\n# launch-helper "
              + json.dumps(recorded, sort_keys=True) + "\nexec "
              + shlex.join([recorded["interpreter"], recorded["helper"], str(Path(root).resolve())]) + ' "$@"\n')
    return script.encode()


def complete(generation, manifest):
    manifest = dict(manifest, format=FORMAT, generation=str(generation))
    atomic_write(generation / "manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return manifest


def validate(generation, root, suffix=None):
    root = Path(root).resolve(strict=True)
    generation = Path(generation)
    if (not generation.is_absolute() or len(generation.parents) < 6 or generation.is_symlink()
            or generation.resolve() != generation or generation.is_relative_to(root)):
        raise StateError("the Maven generation must be canonical and outside the project")
    _owned_directory(generation, private=True)
    if (generation.parent.name != "operations" or generation.parents[2].name != digest(os.fsencode(str(root)))
            or generation.parents[3].name != "v1" or generation.parents[4].name != "maven-purifier"):
        raise StateError("the Maven generation has an invalid namespace")
    for parent in list(generation.parents)[:5]:
        _owned_directory(parent, private=True)
    _owned_directory(generation.parents[5])
    manifest_path = generation / "manifest.json"
    if manifest_path.is_symlink():
        raise StateError("the Maven view manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("format") != FORMAT or manifest.get("generation") != str(generation)
            or manifest.get("projectRoot") != str(root)):
        raise StateError("the Maven view manifest does not match this project or format")
    if suffix is not None and digest(suffix) != manifest["managedConfigHash"]:
        raise StateError("the managed Maven configuration has drifted from its frozen view")
    for relative, expected in manifest["files"].items():
        path = generation / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink()
                or not path.resolve().is_relative_to(generation) or file_digest(path) != expected):
            raise StateError(f"generated Maven configuration has drifted: {relative}")
    for key in ("repository", "settings", "launcher", "projectInputs"):
        path = Path(manifest["paths"][key])
        if not path.is_absolute() or not path.resolve().is_relative_to(generation):
            raise StateError(f"Maven view path escapes its generation: {key}")
        if path.is_symlink():
            raise StateError(f"Maven view path must not be a symlink: {key}")
        if key == "repository":
            if not path.is_dir():
                raise StateError("the Maven view repository is unavailable")
        elif not path.is_file() or str(path.relative_to(generation)) not in manifest["files"]:
            raise StateError(f"Maven view configuration has no frozen file association: {key}")
    return manifest
