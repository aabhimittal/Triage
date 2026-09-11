"""Input generation for differential testing, driven by type annotations.

Edge cases first, then seeded random fill. The ordering matters: refactors
break on 0, -1, "" and [] far more often than on random midrange values, and
putting those first means a counterexample is usually found in the first few
executions rather than the last.
"""

from __future__ import annotations

import random
import re
from typing import Any

_SIMPLE_EDGES: dict[str, list[Any]] = {
    "int": [0, 1, -1, 2, 7, -128, 1023, 2**31],
    "float": [0.0, 1.0, -1.0, 0.5, -2.75, 1e-9, 1e9],
    "str": ["", "a", "abc", " ", "Hello, World", "ünïcødé", "0", "  pad  "],
    "bool": [True, False],
    "bytes": [b"", b"a", b"\x00\xff"],
    "complex": [complex(0, 0), complex(1, -1)],
}


class Unsupported(Exception):
    """Raised when an annotation cannot be turned into values honestly."""


def normalize(annotation: str) -> str:
    return re.sub(r"\s+", "", annotation or "").replace("typing.", "")


def generate(
    annotation: str,
    rng: random.Random,
    count: int,
    seeds: tuple[Any, ...] = (),
) -> list[Any]:
    """`count` values for one parameter; raises Unsupported rather than guess.

    `seeds` are literals mined from the function's own source. This matters more
    than it sounds: two implementations of a bounds check that differ only at
    ``pct == 100.0`` agree on every random float you will ever draw. Uniform
    sampling of a continuous domain has probability ~0 of finding the one input
    that separates them, so the boundary has to be *read off the code* rather
    than searched for. Each numeric seed is expanded to its immediate
    neighbourhood so off-by-one rewrites are separated too.
    """
    ann = normalize(annotation)
    edges = _edges(ann, rng)
    values = _seed_values(ann, seeds) + list(edges)
    while len(values) < count:
        values.append(_random(ann, rng))
    return values[:count]


def _seed_values(ann: str, seeds: tuple[Any, ...]) -> list[Any]:
    base = _strip_optional(ann)
    container, args = _split_generic(base)
    out: list[Any] = []
    for seed in seeds:
        expanded = _neighbourhood(seed)
        if base in ("int", "float", "str", "bool", "bytes"):
            out += [v for v in expanded if _fits(v, base)]
        elif container in ("list", "sequence", "iterable", "set", "frozenset"):
            inner = _strip_optional(args[0]) if args else ""
            usable = [v for v in expanded if not inner or _fits(v, inner)]
            if usable:
                out.append(list(usable[:3]) if container.startswith(("list", "seq", "iter"))
                           else set(usable[:3]))
    deduped: list[Any] = []
    for value in out:
        if not any(type(value) is type(v) and value == v for v in deduped):
            deduped.append(value)
    return deduped[:12]


def _neighbourhood(seed: Any) -> list[Any]:
    if isinstance(seed, bool):
        return [seed, not seed]
    if isinstance(seed, int):
        return [seed, seed - 1, seed + 1]
    if isinstance(seed, float):
        return [seed, seed - 1.0, seed + 1.0]
    if isinstance(seed, str):
        return [seed, seed + "x", ""] if seed else [""]
    return [seed]


def _fits(value: Any, base: str) -> bool:
    if base == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if base == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if base == "bool":
        return isinstance(value, bool)
    if base == "str":
        return isinstance(value, str)
    if base == "bytes":
        return isinstance(value, bytes)
    return False


def _edges(ann: str, rng: random.Random) -> list[Any]:
    base = _strip_optional(ann)
    out: list[Any] = []
    if base != ann:
        out.append(None)
    if base in _SIMPLE_EDGES:
        return out + list(_SIMPLE_EDGES[base])
    container, args = _split_generic(base)
    if container in ("list", "sequence", "iterable"):
        inner = args[0] if args else "int"
        return out + [[], _gen_list(inner, rng, 1), _gen_list(inner, rng, 3), _gen_list(inner, rng, 8)]
    if container in ("set", "frozenset"):
        inner = args[0] if args else "int"
        return out + [set(), set(_gen_list(inner, rng, 3))]
    if container == "tuple":
        if args and args[-1] == "...":
            return out + [(), tuple(_gen_list(args[0], rng, 3))]
        return out + [tuple(_scalar(a, rng) for a in args)]
    if container == "dict":
        k, v = (args + ["str", "int"])[:2]
        return out + [{}, {_scalar(k, rng): _scalar(v, rng) for _ in range(3)}]
    raise Unsupported(f"no generator for annotation '{ann}'")


def _random(ann: str, rng: random.Random) -> Any:
    base = _strip_optional(ann)
    if base != ann and rng.random() < 0.15:
        return None
    return _scalar(base, rng)


def _scalar(ann: str, rng: random.Random) -> Any:
    ann = _strip_optional(normalize(ann))
    if ann == "int":
        return rng.randint(-10_000, 10_000)
    if ann == "float":
        return rng.uniform(-1000.0, 1000.0)
    if ann == "bool":
        return rng.random() < 0.5
    if ann == "str":
        alphabet = "abcdefghijklmnopqrstuvwxyz ABC_0123456789"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    if ann == "bytes":
        return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 8)))
    container, args = _split_generic(ann)
    if container in ("list", "sequence", "iterable"):
        return _gen_list(args[0] if args else "int", rng, rng.randint(0, 6))
    if container in ("set", "frozenset"):
        return set(_gen_list(args[0] if args else "int", rng, rng.randint(0, 5)))
    if container == "tuple":
        if args and args[-1] == "...":
            return tuple(_gen_list(args[0], rng, rng.randint(0, 4)))
        return tuple(_scalar(a, rng) for a in args)
    if container == "dict":
        k, v = (args + ["str", "int"])[:2]
        return {_scalar(k, rng): _scalar(v, rng) for _ in range(rng.randint(0, 4))}
    raise Unsupported(f"no generator for annotation '{ann}'")


def _gen_list(inner: str, rng: random.Random, n: int) -> list[Any]:
    return [_scalar(inner, rng) for _ in range(n)]


def _strip_optional(ann: str) -> str:
    if ann.startswith("Optional[") and ann.endswith("]"):
        return ann[9:-1]
    if "|" in ann:
        parts = [p for p in _split_top(ann, "|") if p not in ("None", "NoneType")]
        if len(parts) == 1:
            return parts[0]
    return ann


def _split_generic(ann: str) -> tuple[str, list[str]]:
    m = re.fullmatch(r"([A-Za-z_][\w.]*)\[(.+)\]", ann)
    if not m:
        return ann.lower(), []
    return m.group(1).lower(), _split_top(m.group(2), ",")


def _split_top(text: str, sep: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]
