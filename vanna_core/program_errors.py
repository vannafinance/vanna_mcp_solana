"""The core program's error codes, read from the pinned IDL by name.

Error numbers shift whenever the program adds or drops an error (the 2026-10-09 upgrade moved StalePrice from 6025 to
6024), so nothing matches a hardcoded number: callers ask for an error by name and get every way a failed simulation
can spell it (the name, `Custom(6024)`, `custom program error: 0x1788`).
"""

from __future__ import annotations

import json
import pathlib
from functools import lru_cache

_IDL = pathlib.Path(__file__).resolve().parent.parent / "idl" / "vanna_credit_layer.json"


@lru_cache(maxsize=1)
def _codes() -> dict[str, int]:
    return {e["name"]: int(e["code"]) for e in json.loads(_IDL.read_text(encoding="utf-8")).get("errors", [])}


def code(name: str) -> int:
    return _codes()[name]


def markers(*names: str) -> tuple[str, ...]:
    """Lower-case substrings that identify these errors in a simulation error or its logs."""
    out: list[str] = []
    for name in names:
        c = _codes().get(name)
        out.append(name.lower())
        if c is not None:
            out += [f"custom({c})", f"0x{c:x}"]
    return tuple(out)


def name_of(code_number: int) -> str | None:
    return next((n for n, c in _codes().items() if c == code_number), None)
