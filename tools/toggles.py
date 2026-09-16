"""Toggle diagnostics for 3dmigoto mod ini files.

Reports identical or gated toggle states and missing namespace
dependencies, and parses persisted toggle values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from .backups import included_ini_files
from .fixer import cached_ini_parse

_SECTION_RE = re.compile(r"^\[(.+)]$")
_VAR_RE = re.compile(r"^\$(\w+)\s*=\s*(.+)$")
_ATOM_RE = re.compile(r"^\$(\w+)\s*(==|!=)\s*(.+)$")
_RUN_RE = re.compile(r"^run\s*=\s*(.+)$")
_NS_TOKEN_RE = re.compile(r"\$\\(\w+)")
_RAW_VAR_RE = re.compile(r"\$([A-Za-z_]\w*)")
_CachedWalk = tuple[
    list[tuple[str, str, frozenset[str]]],
    list[tuple[str, str, str]],
]


@dataclass(frozen=True)
class ToggleWarning:
    """A finding about one toggle.

    Severity is "identical", "gated" or "dep".
    """

    severity: str
    message: str
    toggle_label: str = ""


@dataclass
class _ChainFrame:
    """One open if/elif/else chain while evaluating a section body.

    ``status``/``branch_gates`` describe the branch currently open; the other
    fields say whether an earlier branch took and collect the chain's gate pieces."""

    status: bool | None
    branch_gates: frozenset[str]
    took_low: bool
    took_high: bool
    chain_gates: set[str]


def _split_cycle(value: str) -> tuple[str, ...]:
    """Split a comma-separated cycle value into stripped non-empty entries."""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _iter_sections(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(name, body_lines)`` for every ``[section]`` header in the text."""
    name: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        header = _SECTION_RE.match(line.strip())
        if header is not None:
            if name is not None:
                yield name, body
            name = header.group(1)
            body = []
        elif name is not None:
            body.append(line)
    if name is not None:
        yield name, body


def _parse_toggle_defs(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Collect ``(section_name, var_name, cycle)`` from every ``[Key*]`` section.

    A section counts when its name starts with "key" case-insensitively; the
    variable comes from the first ``$var = value`` line or the section is skipped."""
    defs: list[tuple[str, str, tuple[str, ...]]] = []
    for name, body in _iter_sections(text):
        if not name.lower().startswith("key"):
            continue
        for line in body:
            match = _VAR_RE.match(line.strip())
            if match is None:
                continue
            defs.append((name, match.group(1), _split_cycle(match.group(2))))
            break
    return defs


def _normalize_statement(line: str) -> str | None:
    """Normalize a body line into a lowercase statement, or None.

    Blank lines and full-line ``;`` or ``//`` comments yield None; a trailing
    inline ``;`` comment is dropped and internal whitespace collapsed."""
    stripped = line.strip()
    if stripped.startswith((";", "//")):
        return None
    collapsed = " ".join(stripped.split(";", 1)[0].split())
    if not collapsed:
        return None
    return collapsed.lower()


def _control_token(statement: str) -> tuple[str, str] | None:
    """Classify a statement as a control token, or None.

    Returns ``(kind, condition)`` with kind "if", "elif", "else" or "endif"
    (condition empty for "else"/"endif"); the keyword must be the whole first word."""
    head, _, rest = statement.partition(" ")
    if head == "if" or head == "elif":
        return (head, rest) if rest else None
    if head == "else":
        if rest == "if" or rest.startswith("if "):
            condition = rest[2:].strip()
            return ("elif", condition) if condition else None
        return "else", ""
    if head == "endif":
        return "endif", ""
    return None


def _eval_condition(condition: str, env: dict[str, str]) -> tuple[bool | None, frozenset[str]]:
    """Evaluate a condition three-valuedly against ``env``.

    Returns ``(status, gates)``: True/False when decided, None when unknown
    variables or unparsable atoms; ``gates`` is a disjunction of ``&&`` atom pieces."""
    pieces: set[str] = set()
    unresolved = False
    for disjunct in condition.split("||"):
        atoms: list[tuple[str, str, str, str]] = []
        unparsed = False
        for raw in disjunct.split("&&"):
            atom = raw.strip()
            match = _ATOM_RE.match(atom)
            if match is None:
                unparsed = True
                continue
            atoms.append(
                (atom, match.group(1).lower(), match.group(2), match.group(3))
            )
        unknown: list[str] = []
        holds = True
        for atom, var, op, rhs in atoms:
            value = env.get(var)
            if value is None:
                unknown.append(atom)
                continue
            equal = value.strip().lower() == rhs.strip().lower()
            atom_holds = equal if op == "==" else not equal
            if atom_holds:
                continue
            holds = False
            break
        if not holds:
            continue
        if unparsed:
            unresolved = True
            pieces.add(disjunct.strip())
            continue
        if not unknown:
            return True, frozenset()
        unresolved = True
        pieces.add(" && ".join(unknown))
    if not unresolved:
        return False, frozenset()
    return None, frozenset(pieces)


def _compile_body(lines: list[str]) -> list[tuple[str, str]]:
    """Compile one section body's raw lines into reusable walk items.

    Each non-comment, non-blank line becomes ``("stmt", statement)`` or the
    control token ``_control_token`` reports, so a body classifies once."""
    items: list[tuple[str, str]] = []
    for line in lines:
        statement = _normalize_statement(line)
        if statement is None:
            continue
        token = _control_token(statement)
        if token is None:
            items.append(("stmt", statement))
        else:
            items.append(token)
    return items


def _walk_compiled(
    items: list[tuple[str, str]],
    env: dict[str, str],
) -> list[tuple[str, frozenset[str]]]:
    """Walk one compiled section body and return its reachable statements.

    Returns ``(statement, gates)`` for statements definitely active or held back
    only by unknown variables (``gates``: ``&&``-joined pieces); never raises."""
    active: list[tuple[str, frozenset[str]]] = []
    stack: list[_ChainFrame] = []
    for kind, payload in items:
        if kind == "stmt":
            statement = payload
            if not stack:
                active.append((statement, frozenset()))
                continue
            if any(frame.status is False for frame in stack):
                continue
            if all(frame.status is True for frame in stack):
                active.append((statement, frozenset()))
                continue
            combos: list[list[str]] = [[]]
            for frame in stack:
                if frame.status is None:
                    combos = [
                        combo + [piece]
                        for combo in combos
                        for piece in sorted(frame.branch_gates)
                    ]
            active.append(
                (statement, frozenset(" && ".join(combo) for combo in combos))
            )
            continue
        condition = payload
        if kind == "if":
            status, gates = _eval_condition(condition, env)
            stack.append(
                _ChainFrame(
                    status=status,
                    branch_gates=gates,
                    took_low=status is True,
                    took_high=status is not False,
                    chain_gates=set(gates) if status is None else set(),
                )
            )
            continue
        if kind == "endif":
            if stack:
                stack.pop()
            continue
        if not stack:
            continue
        frame = stack[-1]
        if kind == "elif":
            if frame.took_low:
                frame.status = False
                frame.branch_gates = frozenset()
                continue
            status, gates = _eval_condition(condition, env)
            if status is False:
                frame.status = False
                frame.branch_gates = frozenset()
                continue
            if frame.took_high:
                frame.status = None
                branch_gates = set(frame.chain_gates)
                if status is None:
                    branch_gates.update(gates)
                    frame.chain_gates.update(gates)
                frame.branch_gates = frozenset(branch_gates)
                continue
            frame.status = status
            frame.branch_gates = gates
            if status:
                frame.took_low = True
                frame.took_high = True
            elif status is None:
                frame.took_high = True
                frame.chain_gates.update(gates)
            continue
        if frame.took_low:
            frame.status = False
            frame.branch_gates = frozenset()
        elif frame.took_high:
            frame.status = None
            frame.branch_gates = frozenset(frame.chain_gates)
        else:
            frame.status = True
            frame.branch_gates = frozenset()
            frame.took_low = True
            frame.took_high = True
    return active


def _active_statements(
    lines: list[str],
    env: dict[str, str],
) -> list[tuple[str, frozenset[str]]]:
    """Walk one section body and return its reachable statements.

    Returns ``(statement, gates)`` for statements definitely active or held back
    only by unknown variables (``gates``: ``&&``-joined pieces); never raises."""
    return _walk_compiled(_compile_body(lines), env)


def parse_persisted_text(text: str) -> dict[str, dict[str, str]]:
    """Parse persisted-value lines into ``{ini_path: {var_name: value}}``.

    Lines look like ``\\mods\\test\\sample.ini\\body = 0`` with an optional leading
    ``$``; the path is lowercased with ``/`` separators, garbage lines skipped."""
    result: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip().removeprefix("$")
        if " = " not in stripped:
            continue
        key, _, value = stripped.partition(" = ")
        key = key.strip()
        value = value.strip()
        if "\\" not in key:
            continue
        path, _, var = key.rpartition("\\")
        path = path.replace("\\", "/").lower().lstrip("/")
        if not path or not var:
            continue
        result.setdefault(path, {})[var] = value
    return result


def read_persisted_values(mods_root: Path) -> dict[str, dict[str, str]]:
    """Read persisted values from ``d3dx_user.ini`` next to the mods root.

    A missing or unreadable file yields an empty mapping instead of an exception."""
    target = mods_root.parent / "d3dx_user.ini"
    if not target.is_file():
        return {}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return {}
    return parse_persisted_text(content)


def _persisted_entry(
    persisted: Mapping[str, Mapping[str, str]],
    path: str,
    var: str,
) -> tuple[str, str] | None:
    """Find ``(original_key, value)`` for a lowercased variable on one path."""
    values = persisted.get(path)
    if not values:
        return None
    for key, value in values.items():
        if key.lower() == var:
            return key, value
    return None


def _humanize_atom(atom: str) -> str:
    """Speak one gate atom plainly ("body is 3", "body is not 0"), passing
    unparsable text through with variable dollars stripped for display
    ("$is_dragging" reads "is_dragging")."""
    match = _ATOM_RE.match(atom)
    if match is None:
        return re.sub(r"\$(\w+)", r"\1", atom)
    var, op, value = match.group(1), match.group(2), match.group(3)
    return f"{var} is not {value}" if op == "!=" else f"{var} is {value}"


def _humanize_disjunct(disjunct: str) -> str:
    """Speak one gate disjunct plainly.

    Atoms keep their reading order, consecutive equalities on one variable merge,
    phrases join with "and"; unparsable atoms pass through raw, dollars stripped."""
    atoms: list[tuple[str, str, str]] = []
    for raw in disjunct.split("&&"):
        atom = raw.strip()
        match = _ATOM_RE.match(atom)
        atoms.append(
            (atom, "", "") if match is None
            else (match.group(1), match.group(2), match.group(3))
        )
    group_order: dict[tuple[str, str], int] = {}
    for atom in atoms:
        group_order.setdefault((atom[0], atom[1]), len(group_order))
    atoms.sort(key=lambda atom: (group_order[(atom[0], atom[1])], atom[2]))
    phrases: list[str] = []
    index = 0
    while index < len(atoms):
        var, op, value = atoms[index]
        if op == "==":
            values = [value]
            while index + 1 < len(atoms) and atoms[index + 1][:2] == (var, "=="):
                index += 1
                values.append(atoms[index][2])
            if len(values) == 1:
                phrases.append(f"{var} is {values[0]}")
            else:
                phrases.append(f"{var} is {', '.join(values[:-1])} or {values[-1]}")
        elif op == "!=":
            phrases.append(f"{var} is not {value}")
        else:
            phrases.append(_humanize_atom(var))
        index += 1
    return " and ".join(phrases)


def _humanize_gates(pieces: Iterable[str]) -> str:
    """Speak a gate's disjunction of conjunction pieces plainly.

    Single equality atoms on one and the same variable merge into one phrase
    ("body is 3, 4, 5 or 6"); otherwise the disjuncts join with "or"."""
    ordered = sorted(pieces)
    humanized = [_humanize_disjunct(piece) for piece in ordered]
    atoms: list[tuple[str, str]] = []
    for piece in ordered:
        match = _ATOM_RE.match(piece)
        if "&&" in piece or match is None or match.group(2) != "==":
            return " or ".join(humanized)
        atoms.append((match.group(1).lower(), match.group(3)))
    if len({var for var, _value in atoms}) != 1:
        return " or ".join(humanized)
    var = atoms[0][0]
    values = [value for _var, value in atoms]
    if len(values) == 1:
        return f"{var} is {values[0]}"
    return f"{var} is {', '.join(values[:-1])} or {values[-1]}"


def _gate_variable_names(pieces: Iterable[str]) -> str:
    """List the distinct variables gate pieces mention, at most six names.

    Names come from parseable atoms and raw ``$var`` references, lowercased,
    deduped and sorted; beyond six the list ends with ", …" after the sixth."""
    names: set[str] = set()
    for piece in pieces:
        for match in _ATOM_RE.finditer(piece):
            names.add(match.group(1).lower())
        for match in _RAW_VAR_RE.finditer(piece):
            names.add(match.group(1).lower())
    ordered = sorted(names)
    if len(ordered) > 6:
        return ", ".join(ordered[:6]) + ", …"
    return ", ".join(ordered)


def analyze_toggle_warnings(
    texts: Sequence[tuple[str, str]],
    defined_namespaces: AbstractSet[str],
    persisted: Mapping[str, Mapping[str, str]] | None = None,
) -> list[ToggleWarning]:
    """Analyze every toggle definition of one mod and report findings.

    ``texts`` holds ``(rel_posix_lowercase_path, ini_text)`` pairs; each toggle is
    evaluated state by state into deduped "identical", "gated" and "dep" findings."""
    if persisted is None:
        persisted = {}
    defined = {name.lower() for name in defined_namespaces}
    compiled_sections: list[
        tuple[str, str, list[tuple[str, str]], str, set[str]]
    ] = []
    for path, text in texts:
        for name, body in _iter_sections(text):
            if name.lower().startswith("key"):
                continue
            items = _compile_body(body)
            joined = "".join(payload for _kind, payload in items)
            tokens = {
                match.group(1).lower() for match in _RAW_VAR_RE.finditer(joined)
            }
            compiled_sections.append((path, name, items, joined, tokens))
    findings: list[ToggleWarning] = []
    seen: set[str] = set()
    dep_refs: list[tuple[str, str, str]] = []
    static_results: dict[int, _CachedWalk] = {}
    entry_yields: dict[tuple[str, str], list[tuple[int, str, frozenset[str]]]] = {}

    def add(severity: str, message: str, label: str) -> None:
        if message not in seen:
            seen.add(message)
            findings.append(
                ToggleWarning(severity=severity, message=message, toggle_label=label)
            )

    for text in texts:
        for key_section, var_name, cycle in _parse_toggle_defs(text[1]):
            label = f"[{key_section}]"
            var_key = var_name.lower()
            static_pairs: set[tuple[str, str, frozenset[str]]] = set()
            plan: list[tuple[str, str, list[tuple[str, str]], _CachedWalk | None]] = []
            for index, (path, name, items, _joined, tokens) in enumerate(
                compiled_sections
            ):
                if var_key in tokens:
                    plan.append((path, name, items, None))
                    continue
                cached = static_results.get(index)
                if cached is None:
                    active = _walk_compiled(items, {})
                    triples: list[tuple[str, str, frozenset[str]]] = []
                    cands: list[tuple[str, str, str]] = []
                    for statement, gates in active:
                        triples.append((name, statement, gates))
                        entry_yields.setdefault((name, statement), []).append(
                            (index, path, gates)
                        )
                        run_match = _RUN_RE.match(statement)
                        if run_match is not None:
                            _, _, tail = run_match.group(1).partition("commandlist\\")
                            namespace = tail.split("\\", 1)[0]
                            if namespace:
                                cands.append(
                                    ("run", namespace.lower(), run_match.group(1))
                                )
                        for ns_match in _NS_TOKEN_RE.finditer(statement):
                            cands.append(
                                (
                                    "use",
                                    ns_match.group(1).lower(),
                                    ns_match.group(1).lower(),
                                )
                            )
                    cached = (triples, cands)
                    static_results[index] = cached
                plan.append((path, name, items, cached))
                static_pairs.update(cached[0])
            fingerprints: list[frozenset[tuple[str, str, frozenset[str]]]] = []
            state_gates: list[dict[tuple[str, str], frozenset[str]]] = []
            state_paths: list[dict[tuple[str, str], list[str]]] = []
            for state_index, state in enumerate(cycle):
                env = {var_key: state}
                dyn_pairs: set[tuple[str, str, frozenset[str]]] = set()
                dyn_gates: dict[tuple[str, str], set[str]] = {}
                dyn_path_yields: dict[tuple[str, str], list[tuple[int, str]]] = {}
                for index, (path, name, items, static_cached) in enumerate(plan):
                    if static_cached is not None:
                        if state_index == 0:
                            dep_refs.extend(static_cached[1])
                        continue
                    for statement, gates in _walk_compiled(items, env):
                        entry = (name, statement)
                        dyn_pairs.add((name, statement, gates))
                        dyn_gates.setdefault(entry, set()).update(gates)
                        dyn_path_yields.setdefault(entry, []).append((index, path))
                        run_match = _RUN_RE.match(statement)
                        if run_match is not None:
                            _, _, tail = run_match.group(1).partition("commandlist\\")
                            namespace = tail.split("\\", 1)[0]
                            if namespace:
                                dep_refs.append(
                                    ("run", namespace.lower(), run_match.group(1))
                                )
                        for ns_match in _NS_TOKEN_RE.finditer(statement):
                            dep_refs.append(
                                (
                                    "use",
                                    ns_match.group(1).lower(),
                                    ns_match.group(1).lower(),
                                )
                            )
                gates_map: dict[tuple[str, str], frozenset[str]] = {}
                paths_map: dict[tuple[str, str], list[str]] = {}
                for entry, gates in dyn_gates.items():
                    extra_gates: set[str] = set()
                    extra_yields: list[tuple[int, str]] = []
                    sources = entry_yields.get(entry)
                    if sources is not None:
                        for src_index, src_path, src_gates in sources:
                            if plan[src_index][3] is not None:
                                extra_gates.update(src_gates)
                                extra_yields.append((src_index, src_path))
                    gates_map[entry] = frozenset(gates | extra_gates)
                    yields = dyn_path_yields[entry]
                    if extra_yields:
                        yields = sorted(yields + extra_yields)
                    paths: list[str] = []
                    for _index, yield_path in yields:
                        if yield_path not in paths:
                            paths.append(yield_path)
                    paths_map[entry] = paths
                fingerprints.append(frozenset(static_pairs | dyn_pairs))
                state_gates.append(gates_map)
                state_paths.append(paths_map)
            groups: dict[frozenset[tuple[str, str, frozenset[str]]], list[str]] = {}
            for state, fingerprint in zip(cycle, fingerprints):
                groups.setdefault(fingerprint, []).append(state)
            for group in groups.values():
                if len(group) >= 2:
                    add(
                        "identical",
                        f"{label}: states {', '.join(group)} do the same thing",
                        label,
                    )
            for i, state_a in enumerate(cycle):
                for j in range(i + 1, len(cycle)):
                    state_b = cycle[j]
                    delta = sorted(fingerprints[i] ^ fingerprints[j])
                    if not delta:
                        continue
                    pair_gates: set[str] = set()
                    persisted_vars: dict[str, tuple[str, str]] = {}
                    all_gated = True
                    all_persisted = True
                    for triple in delta:
                        owner = i if triple in fingerprints[i] else j
                        entry_key = (triple[0], triple[1])
                        gates = state_gates[owner][entry_key]
                        if not gates:
                            all_gated = False
                            break
                        pair_gates.update(gates)
                        paths = state_paths[owner].get(entry_key, [])
                        for piece in sorted(gates):
                            for atom_text in piece.split("&&"):
                                match = _ATOM_RE.match(atom_text.strip())
                                if match is None:
                                    all_persisted = False
                                    continue
                                var = match.group(1).lower()
                                hits = 0
                                for path in paths:
                                    persisted_entry = _persisted_entry(
                                        persisted, path, var
                                    )
                                    if persisted_entry is None:
                                        continue
                                    hits += 1
                                    persisted_vars.setdefault(var, persisted_entry)
                                if hits != len(paths):
                                    all_persisted = False
                    if not all_gated:
                        continue
                    gate_text = _humanize_gates(pair_gates)
                    if len(gate_text) > 100:
                        head = (
                            f"{label}: states {state_a} and {state_b} only differ "
                            "depending on other conditions "
                            f"({_gate_variable_names(pair_gates)})"
                        )
                    else:
                        head = (
                            f"{label}: states {state_a} and {state_b} only differ "
                            f"when {gate_text}"
                        )
                    if all_persisted:
                        resolved = {
                            var: value
                            for var, (_key, value) in persisted_vars.items()
                        }
                        gate_holds = False
                        for piece in sorted(pair_gates):
                            conjunct_holds = True
                            for atom_text in piece.split("&&"):
                                match = _ATOM_RE.match(atom_text.strip())
                                if match is None:
                                    conjunct_holds = False
                                    break
                                value = resolved.get(match.group(1).lower())
                                if value is None:
                                    conjunct_holds = False
                                    break
                                equal = (
                                    value.strip().lower()
                                    == match.group(3).strip().lower()
                                )
                                holds = equal if match.group(2) == "==" else not equal
                                if not holds:
                                    conjunct_holds = False
                                    break
                            if conjunct_holds:
                                gate_holds = True
                                break
                        if gate_holds:
                            continue
                        var_clause = ", ".join(
                            f"{var} is {value}"
                            for var, (_key, value) in persisted_vars.items()
                        )
                        add(
                            "gated",
                            (
                                f"{head}, but {var_clause}, so right now they do "
                                "the same thing"
                            ),
                            label,
                        )
                        continue
                    covered: dict[str, set[str]] = {}
                    for piece in sorted(pair_gates):
                        for atom_text in piece.split("&&"):
                            match = _ATOM_RE.match(atom_text.strip())
                            if match is None or match.group(2) != "==":
                                continue
                            rhs = match.group(3).strip().lower()
                            if rhs in {"0", "1"}:
                                covered.setdefault(match.group(1).lower(), set()).add(
                                    rhs
                                )
                    if any(
                        "0" in values and "1" in values for values in covered.values()
                    ):
                        continue
                    add("gated", head, label)
    emitted: set[tuple[str, str]] = set()
    for kind, key, value in dep_refs:
        if key in defined or (kind, key) in emitted:
            continue
        emitted.add((kind, key))
        if kind == "run":
            add("dep", f"runs {value}, but no mod in your folder provides {key}", "")
        else:
            add("dep", f"uses {value}, but no mod in your folder provides it", "")
    return findings


_NAMESPACE_DECL_RE = re.compile(r"^\s*namespace\s*=\s*(\w+)", re.IGNORECASE)


def _collect_text_namespaces(text: str, section_names: Iterable[str]) -> set[str]:
    """Namespace names one ini text defines, lowercase.

    Section names split on ``\\`` contribute their first component (plus a
    ``CommandList\\`` second); ``namespace = X`` declaration lines add ``X``."""
    names: set[str] = set()
    for section_name in section_names:
        parts = section_name.split("\\")
        if parts[0]:
            names.add(parts[0].lower())
        if parts[0].lower() == "commandlist" and len(parts) > 1 and parts[1]:
            names.add(parts[1].lower())
    for line in text.splitlines():
        match = _NAMESPACE_DECL_RE.match(line)
        if match is not None:
            names.add(match.group(1).lower())
    return names


def _collect_framework_namespaces(mods_root: Path) -> set[str]:
    """Namespace names defined by the launcher framework beside the mods root.

    Collects ``mods_root.parent``'s ``d3dx.ini`` plus every ``*.ini`` under its
    ``Core`` folder; unreadable or unparseable files contribute nothing."""
    framework_root = mods_root.parent
    candidates = [framework_root / "d3dx.ini"]
    candidates.extend((framework_root / "Core").rglob("*.ini"))
    names: set[str] = set()
    for path in sorted(candidates):
        try:
            parsed = cached_ini_parse(path)
        except (OSError, ValueError):
            continue
        if parsed is None:
            continue
        text, _hints, sections = parsed
        names |= _collect_text_namespaces(
            text, (section.name for section in sections)
        )
    return names


def _collect_root_namespaces(mods_root: Path) -> set[str]:
    """Namespace names defined by every included .ini under ``mods_root``.

    Reads go through the memoized ini parse; failing files contribute nothing."""
    names: set[str] = set()
    for path in included_ini_files(mods_root):
        try:
            parsed = cached_ini_parse(path)
        except (OSError, ValueError):
            continue
        if parsed is None:
            continue
        text, _hints, sections = parsed
        names |= _collect_text_namespaces(
            text, (section.name for section in sections)
        )
    return names


def compute_mod_toggle_warnings(
    mod_dir: Path,
    mods_root: Path | None,
) -> list[ToggleWarning]:
    """Analyze one mod's toggles on demand for the Mod info dialog.

    Mirrors the analyze-time wiring against the whole mods root; a missing
    ``mods_root`` or any failure yields ``[]`` instead of raising."""
    if mods_root is None:
        return []
    try:
        texts: list[tuple[str, str]] = []
        for path in included_ini_files(mod_dir):
            parsed = cached_ini_parse(path)
            if parsed is None:
                continue
            texts.append(
                (path.relative_to(mods_root).as_posix().lower(), parsed[0])
            )
        if not texts:
            return []
        defined = _collect_root_namespaces(mods_root)
        defined |= _collect_framework_namespaces(mods_root)
        return analyze_toggle_warnings(
            texts, defined, read_persisted_values(mods_root)
        )
    except Exception:  # noqa: BLE001
        return []
