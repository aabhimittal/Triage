"""Unified-diff parser.

Deliberately dependency-free and deliberately small. We only need what the
checks consume: which *new-file* lines a change introduced, which old-file
lines it removed, and enough context to name the hunk.

Contiguity note: git's default 3 lines of context often merge two logically
separate edits into one hunk. ``split_on_context`` re-splits a hunk wherever
more than ``gap`` unchanged lines separate two runs of changes, so a 200-line
diff does not become a single unreviewable blob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from triage.model import Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_OLD_FILE_RE = re.compile(r"^--- (?:a/)?(.+?)(?:\t.*)?$")
_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$")
_DEF_RE = re.compile(r"^(?:@|(?:async\s+)?def\s|class\s)")


def parse_diff(text: str, context_gap: int = 1) -> list[Hunk]:
    """Parse a unified diff into hunks, splitting on runs of context."""
    return list(_iter_hunks(text, context_gap))


def _iter_hunks(text: str, gap: int) -> Iterator[Hunk]:
    path = ""
    old_path = ""
    is_new_file = False
    is_deleted_file = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git"):
            is_new_file = is_deleted_file = False
            path = old_path = ""
        elif line.startswith("new file mode"):
            is_new_file = True
        elif line.startswith("deleted file mode"):
            is_deleted_file = True
        elif line.startswith("--- "):
            m = _OLD_FILE_RE.match(line)
            if m:
                old_path = "" if m.group(1) == "/dev/null" else m.group(1)
        elif line.startswith("+++ "):
            m = _NEW_FILE_RE.match(line)
            if m:
                path = "" if m.group(1) == "/dev/null" else m.group(1)
                if not path:
                    is_deleted_file = True
                    path = old_path
        elif line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                i += 1
                continue
            old_start = int(m.group(1))
            new_start = int(m.group(3))
            header = m.group(5).strip()
            body, i = _collect_body(lines, i + 1)
            yield from _build_hunks(
                path or old_path, header, old_start, new_start,
                body, is_new_file, is_deleted_file, gap,
            )
            continue
        i += 1


def _collect_body(lines: list[str], i: int) -> tuple[list[str], int]:
    body: list[str] = []
    while i < len(lines):
        line = lines[i]
        if line.startswith(("@@", "diff --git", "--- ", "+++ ")):
            break
        if line.startswith("\\"):  # "\ No newline at end of file"
            i += 1
            continue
        body.append(line)
        i += 1
    return body, i


@dataclass
class _Event:
    kind: str      # "+", "-" or " "
    old_no: int
    new_no: int
    text: str


def _build_hunks(
    path: str,
    header: str,
    old_start: int,
    new_start: int,
    body: Iterable[str],
    is_new_file: bool,
    is_deleted_file: bool,
    gap: int,
) -> list[Hunk]:
    events: list[_Event] = []
    old_no, new_no = old_start, new_start
    for line in body:
        kind, text = (line[:1] or " "), line[1:]
        if kind == "+":
            events.append(_Event("+", old_no, new_no, text))
            new_no += 1
        elif kind == "-":
            events.append(_Event("-", old_no, new_no, text))
            old_no += 1
        else:
            events.append(_Event(" ", old_no, new_no, text))
            old_no += 1
            new_no += 1

    grouped: list[Hunk] = []
    for group in split_on_context(events, gap):
        added = {e.new_no: e.text for e in group if e.kind == "+"}
        removed = {e.old_no: e.text for e in group if e.kind == "-"}
        if added:
            first_new, last_new = min(added), max(added)
        else:
            # Pure deletion: anchor at the seam so a reviewer can find the spot.
            first_new = last_new = max(group[0].new_no - 1, 1)
        grouped.append(
            Hunk(
                path=path,
                header=header,
                new_start=first_new,
                new_end=last_new,
                added_lines=added,
                removed_lines=removed,
                old_start=group[0].old_no,
                old_end=group[-1].old_no,
                is_new_file=is_new_file,
                is_deleted_file=is_deleted_file,
            )
        )
    out: list[Hunk] = []
    for hunk in grouped:
        out.extend(split_on_definitions(hunk))
    return out


def split_on_definitions(hunk: Hunk) -> list[Hunk]:
    """Split a block of added lines wherever a new top-level definition starts.

    Context-based splitting cannot see inside a wholly new block: when a commit
    appends two functions, the blank lines between them are themselves added
    lines, so there is no context to break on. Reviewers reason in units of
    definitions, and so should the residual set -- otherwise one unverifiable
    function drags its well-tested neighbour into review with it.
    """
    if not hunk.path.endswith(".py") or len(hunk.added_lines) < 2:
        return [hunk]
    linenos = hunk.added_linenos
    segments: list[list[int]] = [[]]
    for lineno in linenos:
        text = hunk.added_lines[lineno]
        starts_def = bool(_DEF_RE.match(text))
        previous_blank = bool(segments[-1]) and not hunk.added_lines[segments[-1][-1]].strip()
        if starts_def and previous_blank:
            while segments[-1] and not hunk.added_lines[segments[-1][-1]].strip():
                segments[-1].pop()  # trailing blanks belong to neither side
            segments.append([])
        segments[-1].append(lineno)
    segments = [seg for seg in segments if seg]
    if len(segments) < 2:
        return [hunk]
    out: list[Hunk] = []
    for i, seg in enumerate(segments):
        out.append(
            Hunk(
                path=hunk.path,
                header=hunk.header,
                new_start=seg[0],
                new_end=seg[-1],
                added_lines={ln: hunk.added_lines[ln] for ln in seg},
                removed_lines=hunk.removed_lines if i == 0 else {},
                old_start=hunk.old_start,
                old_end=hunk.old_end,
                is_new_file=hunk.is_new_file,
                is_deleted_file=hunk.is_deleted_file,
            )
        )
    return out


def split_on_context(events: list[_Event], gap: int) -> list[list[_Event]]:
    """Group changed lines, breaking wherever more than `gap` context lines run.

    git's default -U3 routinely glues unrelated edits into one hunk: a one-line
    guard change and a brand new function 8 lines below arrive as a single
    block. Reviewing (and verifying) at that granularity is useless, because one
    unverifiable line would drag an entire file's worth of proved lines into the
    residual set. Python separates top-level definitions with two blank lines,
    so a default gap of 1 splits almost exactly on definition boundaries.
    """
    groups: list[list[_Event]] = []
    current: list[_Event] = []
    context_run = 0
    for event in events:
        if event.kind == " ":
            if current:
                context_run += 1
                if context_run > gap:
                    groups.append(current)
                    current = []
                    context_run = 0
        else:
            context_run = 0
            current.append(event)
    if current:
        groups.append(current)
    return groups
