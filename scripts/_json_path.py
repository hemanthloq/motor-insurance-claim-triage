"""Read one dotted path out of a JSON document on stdin.

A helper for the shell scripts in this directory, which need to pull a single
field out of an API response and have no guarantee that `jq` is installed.

    curl -s .../result | python scripts/_json_path.py coverage.position

`len` as the final segment gives the length of a list or dict. A path that does
not resolve prints an empty line and exits 0, so the caller can test the value
rather than the exit status.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("", file=sys.stdout)
        return 0
    path = sys.argv[1]
    try:
        node = json.load(sys.stdin)
    except Exception:
        print("")
        return 0

    for part in path.split("."):
        if not part:
            continue
        if part == "len":
            node = len(node) if hasattr(node, "__len__") else 0
            break
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                node = None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            node = None
        if node is None:
            break

    print("" if node is None else node)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
