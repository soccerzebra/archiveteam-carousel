"""Small helpers projects import.

Public surface: ``find_executable`` — projects use it to pick a working ``wget-at`` from a
list of candidate paths, verifying each reports an acceptable version.
"""

from __future__ import annotations

import subprocess
from typing import Any, Iterable


def test_executable(name: str, version: Any, path: str, version_arg: str = "-V") -> bool:
    """Run ``path version_arg`` and decide whether it is an acceptable ``name``.

    ``version`` may be a string (must appear in the output), an object with a ``.search``
    method (regex-like matcher, as projects pass for wget-at), or an iterable of strings
    (any must appear).
    """
    print(f"Looking for {name} in {path}")
    try:
        proc = subprocess.run([path, version_arg], capture_output=True)
    except OSError as e:
        print(f"{path}: {e}")
        return False

    if proc.returncode != 0:
        print(f"{path}: returned code {proc.returncode}")
        return False

    output = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    if isinstance(version, str):
        ok = version in output
    elif hasattr(version, "search"):
        ok = bool(version.search(output))
    elif isinstance(version, Iterable):
        ok = any(v in output for v in version)
    else:
        ok = True

    if not ok:
        print(f"{path}: incorrect {name} version.")
        return False
    print(f"Found usable {name} in {path}")
    return True


def find_executable(name: str, version: Any, paths: Iterable[str],
                    version_arg: str = "-V") -> str | None:
    """Return the first candidate path that is an acceptable ``name``, or None."""
    for path in paths:
        if test_executable(name, version, path, version_arg):
            return path
    return None
