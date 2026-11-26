"""Find where a codebase touches a flag and which events it sends.

This is a plain text scan, not a parser. It is deliberately conservative: it
reports what it saw with file and line, and leaves the judgement of what to
change to the person or coding agent reading the report.
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", "vendor", ".referee", ".idea",
    ".vscode", "coverage", ".pytest_cache", ".mypy_cache", ".tox", "out",
}
SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".java", ".kt", ".go", ".dart", ".rb", ".php", ".cs", ".swift", ".rs",
    ".html", ".htm", ".astro",
}
MAX_FILE_BYTES = 1_000_000

# SDK calls that evaluate a flag AND record the exposure, by SDK.
EXPOSING_CALLS = (
    "get_experiment_flag", "getExperimentFlag", "GetExperimentFlag", "useExperiment",
    "track_exposure_event", "trackExposureEvent", "TrackExposureEvent",
)
# SDK calls that read a flag without recording who saw which version.
PLAIN_READ_CALLS = (
    "get_feature_value", "is_feature_enabled", "get_flag", "getValue", "hasFeature",
    "getFeatureValue", "isFeatureEnabled", "GetFeatureValue", "IsFeatureEnabled",
    "useFlags", "getFeatureFlagValue", "hasFeatureFlag", "get_value",
)
_CALL_NAME = r"(?:track_event|trackEvent|TrackEvent)"
# A plain first argument before the event name, as Go and Java have: an
# identifier, or a call with at most one level of nested parentheses.
_LEADING_ARGUMENT = r"(?:[A-Za-z_][\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s*,\s*)?"
_TRACK_PATTERNS = (
    # trackEvent("name"), track_event(event="name"), TrackEvent(ctx, "name"), trackEvent<T>("name")
    re.compile(
        r"\b" + _CALL_NAME + r"\s*(?:<[^<>()]*>)?\s*\(\s*" + _LEADING_ARGUMENT
        + r"""(?:event\s*=\s*)?(['"`])([^'"`\n]+)\1"""
    ),
    # track_event(identifier=u, event="name"): the name given by keyword, later in the call
    re.compile(r"\b" + _CALL_NAME + r"""\s*\([^()]{0,300}?\bevent\s*=\s*(['"])([^'"\n]+)\1"""),
    # Ruby without parentheses: track_event 'name', identifier: u
    re.compile(r"""\btrack_event[ \t]+(['"])([^'"\n]+)\1"""),
)
_ANY_TRACK_CALL = re.compile(r"\b" + _CALL_NAME + r"\s*(?:<[^<>()]*>)?\s*[\( ]")
_SERVER_KEY = re.compile(r"\bser\.[A-Za-z0-9]{16,}\b")


@dataclass
class Reference:
    file: str
    line: int
    text: str
    kind: str  # "exposing", "plain_read" or "mention"

    def to_dict(self) -> Dict[str, object]:
        return {"file": self.file, "line": self.line, "kind": self.kind, "text": self.text}


@dataclass
class ScanResult:
    root: str
    files_scanned: int = 0
    files_skipped: int = 0
    unnamed_tracks: List[Reference] = field(default_factory=list)
    unnamed_reads: List[Reference] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    tracked_events: Dict[str, List[Reference]] = field(default_factory=dict)
    values_seen: Dict[str, List[Reference]] = field(default_factory=dict)
    leaked_keys: List[Reference] = field(default_factory=list)

    @property
    def exposing(self) -> List[Reference]:
        return [r for r in self.references if r.kind == "exposing"]

    @property
    def plain_reads(self) -> List[Reference]:
        return [r for r in self.references if r.kind == "plain_read"]

    def to_dict(self) -> Dict[str, object]:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "references": [r.to_dict() for r in self.references],
            "tracked_events": {
                name: [r.to_dict() for r in refs]
                for name, refs in sorted(self.tracked_events.items())
            },
            "values_seen": {
                value: [r.to_dict() for r in refs]
                for value, refs in sorted(self.values_seen.items())
            },
        }


def iter_source_files(root: Path) -> Iterable[Path]:
    for directory, subdirs, files in os.walk(str(root)):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".tox"))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                # Regular files only: a symlink could point outside the tree,
                # and a FIFO or device would block the scan.
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def count_skipped(root: Path) -> int:
    """Source files left out of a scan for being too large or not regular files."""
    skipped = 0
    for directory, subdirs, files in os.walk(str(root)):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(directory) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    skipped += 1
            except OSError:
                skipped += 1
    return skipped


def _quoted(value: str) -> "re.Pattern[str]":
    # A string literal in any of the three quote styles. Doubled backticks are
    # prose markup (docstrings, Markdown), not code, so they do not count.
    return re.compile(r"""(?<!`)(['"`])%s\1(?!`)""" % re.escape(value))


# A name bound to nothing but a string: FLAG = "signup_form", const EVENT = 'x';
_CONSTANT = re.compile(
    r"""(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var|final|static|public|private|val)?[ \t]*"""
    r"""(?:[A-Za-z_][\w<>\[\]]*[ \t]+)?([A-Za-z_]\w*)[ \t]*(?::[^=\n]{1,40})?=[ \t]*(['"`])([^'"`\n]{1,200})\2[ \t]*;?[ \t]*$"""
)
_ARGUMENT_NAME = r"((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)"
_TRACK_BY_NAME = re.compile(
    # The first one or two plain-name arguments of a track call. Which of them
    # is the event (Go and Java put a context first) is settled by looking the
    # names up among the string constants.
    r"\b" + _CALL_NAME + r"\s*(?:<[^<>()]*>)?\s*\(\s*(?:event\s*=\s*)?" + _ARGUMENT_NAME
    + r"\s*(?:,\s*(?:event\s*=\s*)?" + _ARGUMENT_NAME + r")?\s*[,)]"
)


def _read_by_name() -> "re.Pattern[str]":
    names = "|".join(re.escape(n) for n in EXPOSING_CALLS + PLAIN_READ_CALLS)
    return re.compile(
        r"\b(" + names + r")\s*(?:<[^<>()]*>)?\s*\(\s*" + _LEADING_ARGUMENT + r"(?:feature_name\s*=\s*)?" + _ARGUMENT_NAME + r"\s*[,)]"
    )


def scan(root: Path, flag: str, values: Optional[Iterable[str]] = None) -> ScanResult:
    """Scan ``root`` for uses of ``flag``, tracked events and variant values.

    ``values`` are the strings the flag can serve; seeing them as string
    literals in a file that uses the flag is the evidence that the code
    handles each version. Comments and docstrings are blanked out first, so a
    commented-out call never counts as instrumentation. A flag or event name
    held in a simple constant (``FLAG = "signup_form"``) is followed to the
    calls that use the constant.
    """
    root = Path(root)
    result = ScanResult(root=str(root))
    flag_literal = _quoted(flag)
    value_patterns = {value: _quoted(value) for value in (values or []) if value and value != flag}
    result.files_skipped = count_skipped(root)

    # Pass 1: read the files that could matter, strip their comments, and
    # collect the names bound to string constants anywhere in the tree.
    files = []
    constants: Dict[str, Set[str]] = {}
    for path in iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            result.files_skipped += 1
            continue
        result.files_scanned += 1
        if flag not in text and "rack" not in text and "ser." not in text and "xperiment" not in text:
            continue  # nothing this scan looks for
        code = strip_comments(text, path.suffix.lower())
        files.append((str(path.relative_to(root)), text, code))
        for match in _CONSTANT.finditer(code):
            constants.setdefault(match.group(1), set()).add(match.group(3))
    flag_names = {name for name, literals in constants.items() if flag in literals}
    read_by_name = _read_by_name()

    # Pass 2: find the reads and the event calls.
    for relative, text, code in files:
        mentions_flag = flag in text
        lines = text.splitlines()
        code_lines = code.splitlines()
        code_lines += [""] * (len(lines) - len(code_lines))
        starts = _line_starts(code)

        def line_of(position: int) -> int:
            return bisect.bisect_right(starts, position)

        def snippet_at(number: int) -> str:
            return lines[number - 1].strip()[:200] if 0 < number <= len(lines) else ""

        # Event calls may span lines, so they are matched on the whole file.
        seen = set()
        for pattern in _TRACK_PATTERNS:
            for match in pattern.finditer(code):
                number = line_of(match.start())
                if (number, match.group(2)) in seen:
                    continue
                seen.add((number, match.group(2)))
                result.tracked_events.setdefault(match.group(2), []).append(
                    Reference(relative, number, snippet_at(number), "track")
                )
        for match in _TRACK_BY_NAME.finditer(code):
            # track_event(SIGNUP_DONE, ...): follow the constant if there is one.
            number = line_of(match.start())
            literals: Set[str] = set()
            for group in (1, 2):
                if match.group(group) and not literals:
                    literals = constants.get(match.group(group).split(".")[-1], set())
            for literal in sorted(literals):
                if (number, literal) not in seen:
                    seen.add((number, literal))
                    result.tracked_events.setdefault(literal, []).append(
                        Reference(relative, number, snippet_at(number), "track")
                    )
        named_lines = {number for number, _ in seen}
        for match in _ANY_TRACK_CALL.finditer(code):
            number = line_of(match.start())
            if number not in named_lines and not _DEFINITION.search(code_lines[number - 1] if number <= len(code_lines) else ""):
                # An event sent under a name this scan cannot read: a variable
                # or an expression.
                result.unnamed_tracks.append(Reference(relative, number, snippet_at(number), "track"))

        # Reads of the flag through a constant: get_experiment_flag(FLAG, ...)
        constant_reads = set()
        for match in read_by_name.finditer(code):
            number = line_of(match.start())
            name = match.group(2).split(".")[-1]
            exposing = match.group(1) in EXPOSING_CALLS
            if name in flag_names:
                constant_reads.add(number)
                result.references.append(
                    Reference(relative, number, snippet_at(number), "exposing" if exposing else "plain_read")
                )
            elif exposing and name not in constants and name not in ("self", "this"):
                result.unnamed_reads.append(Reference(relative, number, snippet_at(number), "exposing"))

        for number, line in enumerate(lines, start=1):
            snippet = line.strip()[:200]
            live = code_lines[number - 1]
            if _SERVER_KEY.search(live):
                result.leaked_keys.append(Reference(relative, number, "(server-side key in source)", "secret"))
            if not mentions_flag:
                continue
            found = flag_literal.search(live)
            if found and number not in constant_reads:
                # The call the literal belongs to is the nearest SDK call before
                # it, on this line or the few above (calls are often split).
                before = "\n".join(code_lines[max(0, number - 6):number - 1] + [live[:found.start()]])
                nearest, kind = -1, "mention"
                for names, label in ((EXPOSING_CALLS, "exposing"), (PLAIN_READ_CALLS, "plain_read")):
                    for name in names:
                        for match in re.finditer(r"\b%s\b" % re.escape(name), before):
                            if match.start() > nearest:
                                nearest, kind = match.start(), label
                # A closed statement between the call and the literal means they are unrelated.
                if nearest >= 0 and ";" in before[nearest:].replace(";\n", ";") and before[nearest:].count("(") <= before[nearest:].count(")"):
                    kind = "mention"
                if kind == "mention" and _CONSTANT.search(live):
                    kind = "constant"  # where the name is defined, not a read
                result.references.append(Reference(relative, number, snippet, kind))
            elif not found and flag_literal.search(line):
                # Only inside a comment or docstring: worth listing, never instrumentation.
                result.references.append(Reference(relative, number, snippet, "mention"))
            for value, pattern in value_patterns.items():
                if pattern.search(live):
                    result.values_seen.setdefault(value, []).append(Reference(relative, number, snippet, "value"))
    result.references.sort(key=lambda r: (r.file, r.line))
    for references in result.tracked_events.values():
        references.sort(key=lambda r: (r.file, r.line))
    return result


# "def track_event(" / "function trackEvent(": a definition, not a call that sends something.
_DEFINITION = re.compile(r"\b(?:def|function|func|fun)\s+" + _CALL_NAME + r"\b")


def _line_starts(text: str) -> List[int]:
    starts = [0]
    position = text.find("\n")
    while position != -1:
        starts.append(position + 1)
        position = text.find("\n", position + 1)
    return starts


_HASH_COMMENTS = {".py", ".rb"}
_TRIPLE_QUOTES = {".py", ".java", ".kt", ".swift", ".dart"}
# What may come just before a // that starts a comment. This is what keeps the
# // in a URL (after ":" or "=") in markup, JSX text or a regex from being
# taken for one.
_BEFORE_LINE_COMMENT = set(" \t\n;{}),")
_NO_SLASH_COMMENTS = {".py", ".rb"}
_MARKUP = {".html", ".htm", ".vue", ".svelte", ".astro"}


def strip_comments(text: str, suffix: str) -> str:
    """Replace comments (and Python docstrings) with spaces, keeping every
    line break, so line numbers and string literals in real code survive.

    A small scanner, not a parser: it tracks whether it is inside a string
    literal so that ``"http://x"`` or ``"#fff"`` is not mistaken for a comment.
    """
    hash_comments = suffix in _HASH_COMMENTS
    slash_comments = suffix not in _NO_SLASH_COMMENTS
    markup = suffix in _MARKUP
    triple_quotes = suffix in _TRIPLE_QUOTES
    out: List[str] = []
    index, length, quote = 0, len(text), ""

    def blank(start: int, end: int) -> None:
        out.append("".join(ch if ch == "\n" else " " for ch in text[start:end]))

    while index < length:
        ch = text[index]
        if quote:
            if ch == "\\" and index + 1 < length:
                out.append(text[index:index + 2])
                index += 2
                continue
            if ch == quote or (ch == "\n" and quote != "`"):
                quote = ""  # closed, or an unterminated one-line string ends with its line
            out.append(ch)
            index += 1
            continue
        if triple_quotes and (text.startswith('"""', index) or text.startswith("'''", index)):
            delimiter, end = text[index:index + 3], index + 3
            while end < length and not text.startswith(delimiter, end):
                end += 2 if text[end] == "\\" else 1  # an escaped quote does not close it
            end = min(length, end + 3)
            blank(index, end)
            index = end
            continue
        end = -1
        if hash_comments and ch == "#":
            end = text.find("\n", index)
            end = length if end < 0 else end
        elif slash_comments and text.startswith("//", index) and (index == 0 or text[index - 1] in _BEFORE_LINE_COMMENT):
            end = text.find("\n", index)
            end = length if end < 0 else end
        elif slash_comments and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
        elif markup and text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            end = length if end < 0 else end + 3
        if end >= 0:
            blank(index, end)
            index = end
            continue
        if ch in "\"'`":
            quote = ch
        out.append(ch)
        index += 1
    return "".join(out)


def _is_call(text: str) -> bool:
    """True when the line looks like code calling the SDK, not prose about it."""
    stripped = text.lstrip()
    return bool(re.search(r"[.\w]\s*(?:track_event|trackEvent|TrackEvent)\s*\(", text)) and not stripped.startswith(("#", "//", "*", "``", '"""'))


def flags_in_code(root: Path, flag_names: Iterable[str]) -> Dict[str, List[Reference]]:
    """For many flags at once: where each is mentioned as a string literal."""
    names: Set[str] = set(flag_names)
    found: Dict[str, List[Reference]] = {name: [] for name in names}
    if not names:
        return found
    pattern = re.compile(
        r"""(?<!`)(['"`])(%s)\1(?!`)""" % "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    )
    root = Path(root)
    for path in iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not any(name in text for name in names):
            continue
        relative = str(path.relative_to(root))
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                found[match.group(2)].append(
                    Reference(relative, number, line.strip()[:200], "mention")
                )
    return found


def same_but_for_spelling(name: str, candidates: Iterable[str]) -> Optional[str]:
    """A candidate that differs only in case or punctuation: certainly the same name, misspelt."""
    target = _squash(name)
    for candidate in candidates:
        if candidate != name and _squash(candidate) == target:
            return candidate
    return None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def closest(name: str, candidates: Iterable[str]) -> Optional[str]:
    """A candidate that is probably the same name spelled differently."""
    squash = _squash
    target = squash(name)
    best: Optional[str] = None
    best_distance = 3
    for candidate in candidates:
        if candidate == name:
            continue
        other = squash(candidate)
        if other == target:
            return candidate
        distance = _edit_distance(target, other, best_distance)
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _edit_distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) >= limit:
        return limit
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return min(previous[-1], limit)
