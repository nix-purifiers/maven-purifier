# SPDX-License-Identifier: MIT
"""Build the native Maven input metadata and merge immutable repository views."""
import argparse
import importlib.util
import json
from pathlib import Path


def properties(catalog, output):
    modules = json.loads(Path(catalog).read_text())["modules"]
    lines = []
    for module in sorted(modules, key=lambda module: (module["group"], module["name"])):
        key = module["group"] + ":" + module["name"]
        key = key.replace("\\", "\\\\").replace(":", "\\:").replace("=", "\\=").replace(" ", "\\ ")
        lines.append(key + "=" + module["version"] + "\n")
    Path(output).write_text("".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    props = subcommands.add_parser("properties")
    props.add_argument("catalog")
    props.add_argument("output")
    merge = subcommands.add_parser("merge")
    merge.add_argument("tool")
    merge.add_argument("source")
    merge.add_argument("target")
    args = parser.parse_args()
    if args.command == "properties":
        properties(args.catalog, args.output)
    else:
        spec = importlib.util.spec_from_file_location("purifier_jvm_repository", args.tool)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.merge(Path(args.source), Path(args.target))


if __name__ == "__main__":
    main()
