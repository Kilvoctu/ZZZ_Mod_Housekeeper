"""Marker store for cross-mod conflict fixes the app auto-applied on a mod enable.

Conflict fixes wrapping sections in ``if $flag == 0`` guards or unifying ``override_vertex_count``
lines are recorded next to the fix-backup chains so disabling a mod surgically undoes them; applying stays with apply_plan."""

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .backups import (
    _DISABLED_TOGGLE,
    _canonical_dirs,
    _resolve_live_path,
    store_root,
)
from .fixer import _decode_ini, _split_eol, cached_ini_parse

_WRAP_GUARD_RE = re.compile(r"^if \$(\S+) == 0$")


def marker_path(store_dir: Path, mods_dir: Path) -> Path:
    """Marker file: <store root for mods_dir>/_defer_fixes.json."""
    return store_root(store_dir, mods_dir) / "_defer_fixes.json"


def load_state(store_dir: Path, mods_dir: Path) -> dict:
    """Recorded wrap/unify entries; {"wraps": [], "unifies": []} when absent or corrupt."""
    try:
        data = json.loads(marker_path(store_dir, mods_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"wraps": [], "unifies": []}
    if not isinstance(data, dict):
        return {"wraps": [], "unifies": []}
    return {
        "wraps": [entry for entry in data.get("wraps", []) if isinstance(entry, dict)],
        "unifies": [
            entry for entry in data.get("unifies", []) if isinstance(entry, dict)
        ],
    }


def write_state(store_dir: Path, mods_dir: Path, state: dict) -> None:
    """Persist the wrap/unify state as the marker file (sorted keys, two-space indent)."""
    state_path = marker_path(store_dir, mods_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def canonical_file(mods_dir: Path, path: Path) -> str:
    """DISABLED_-stripped posix relative path of an ini under the mods folder.

    Canonical like the store keys (dirs via backups._canonical_dirs) plus the
    filename prefix also stripped, so enable/disable toggles keep one entry."""
    mods_dir, path = Path(mods_dir), Path(path)
    try:
        rel = path.relative_to(mods_dir)
    except ValueError:
        rel = path
    return "/".join(
        (
            *_canonical_dirs(rel.parent.parts),
            rel.name.removeprefix(_DISABLED_TOGGLE),
        )
    )


def record(store_dir: Path, mods_dir: Path, wraps: Sequence, unifies: Sequence) -> None:
    """Append entries derived from deference specs; identical entries are skipped.

    One wrap entry per guard spec and one unify entry per rewrite spec, each timestamped (UTC isoformat) at recording time;
    the state is written back only when something new was added."""
    state = load_state(store_dir, mods_dir)
    applied = datetime.now(timezone.utc).isoformat()
    changed = False
    for spec in wraps:
        entry = {
            "file": canonical_file(mods_dir, spec.file),
            "section": str(spec.section),
            "flag": str(spec.flag).removeprefix("$"),
            "mod": str(spec.mod),
            "other": str(spec.other),
            "applied": applied,
        }
        if _known(state["wraps"], entry):
            continue
        state["wraps"].append(entry)
        changed = True
    for spec in unifies:
        entry = {
            "file": canonical_file(mods_dir, spec.file),
            "section": str(spec.section),
            "line_no": int(spec.line_no),
            "old": str(spec.old),
            "new": str(spec.new),
            "mod": str(spec.mod),
            "others": [str(other) for other in spec.others],
            "applied": applied,
        }
        if _known(state["unifies"], entry):
            continue
        state["unifies"].append(entry)
        changed = True
    if changed:
        write_state(store_dir, mods_dir, state)


def undo_for_mod(
    store_dir: Path, mods_root: Path, mod_name: str, log: Callable[[str], None]
) -> int:
    """Surgically revert the recorded auto-fixes involving one mod; returns edits undone.

    An entry matches when its involved-mods set contains the mod name (DISABLED_ prefix and case ignored);
    undone and stale entries are pruned in place and the pruned state is written back once at the end."""
    state = load_state(store_dir, mods_root)
    target = str(mod_name).strip().removeprefix(_DISABLED_TOGGLE).casefold()
    undone = 0
    changed = False
    for entries, undo_entry in (
        (state["wraps"], _undo_wrap),
        (state["unifies"], _undo_unify),
    ):
        kept: list[dict] = []
        for entry in entries:
            if target not in _involved(entry):
                kept.append(entry)
                continue
            try:
                status = undo_entry(mods_root, entry, log)
            except Exception:  # noqa: BLE001
                log(f"auto-fix error: {entry.get('file', '?')}")
                status = "keep"
            if status == "keep":
                kept.append(entry)
                continue
            changed = True
            if status == "undone":
                undone += 1
        entries[:] = kept
    if changed:
        try:
            write_state(store_dir, mods_root, state)
        except OSError:
            log("auto-fix error: could not update the defer marker store")
    return undone


def _involved(entry: dict) -> set[str]:
    """Mod names an entry touches, casefolded and DISABLED_-stripped.

    Wraps involve the mod/other pair; unifies involve the mod plus its others."""
    names = {str(entry.get("mod", ""))}
    names.add(str(entry.get("other", "")))
    names.update(str(other) for other in entry.get("others", []))
    return {
        name.strip().removeprefix(_DISABLED_TOGGLE).casefold() for name in names
    }


def _known(entries: Sequence[dict], candidate: dict) -> bool:
    """True when an entry already records the candidate (the timestamp excluded)."""
    identity = {key: value for key, value in candidate.items() if key != "applied"}
    return any(
        {key: value for key, value in entry.items() if key != "applied"} == identity
        for entry in entries
    )


def _live_ini(mods_root: Path, file_key: str) -> Path:
    """The current live file for a canonical relative ini path (enabled or DISABLED_)."""
    parts = [part for part in file_key.replace("\\", "/").split("/") if part]
    mods_root = Path(mods_root)
    if not parts:
        return mods_root / file_key
    dirs, name = tuple(parts[:-1]), parts[-1]
    live = _resolve_live_path(mods_root, dirs, name)
    if live.exists():
        return live
    return _resolve_live_path(mods_root, dirs, _DISABLED_TOGGLE + name)


def _find_section(sections, name: str):
    """First parsed section whose name matches (case-insensitive); None when absent."""
    wanted = name.strip().lower()
    for section in sections:
        if section.name.strip().lower() == wanted:
            return section
    return None


def _live_section(
    mods_root: Path, entry: dict, log: Callable[[str], None]
) -> tuple[Path, str, str, object] | None:
    """(live path, text, write encoding, parsed section) for one recorded entry.

    Logs "auto-fix stale" and returns None when the file or the section is gone
    (the caller prunes); read or decode failures raise to the caller's handler."""
    file_key = str(entry.get("file", ""))
    header = f"{file_key} [{entry.get('section', '')}]"
    live = _live_ini(mods_root, file_key)
    if not live.exists():
        log(f"auto-fix stale: {header} file missing")
        return None
    parsed = cached_ini_parse(live)
    if parsed is None:
        raise OSError(f"unreadable ini: {live}")
    text = parsed[0]
    section = _find_section(parsed[2], str(entry.get("section", "")))
    if section is None:
        log(f"auto-fix stale: {header} section missing")
        return None
    return live, text, _decode_ini(live.read_bytes())[1], section


def _undo_wrap(mods_root: Path, entry: dict, log: Callable[[str], None]) -> str:
    """Remove one recorded guard wrap from its live section.

    Returns "undone" when the guard and its trailing endif were removed and
    "stale" (prune) when the file or section no longer shows them."""
    target = _live_section(mods_root, entry, log)
    if target is None:
        return "stale"
    live, text, encoding, section = target
    flag = str(entry.get("flag", "")).removeprefix("$").lower()
    lines = text.splitlines(keepends=True)
    body = [(no, content) for no, content in section.body if no <= section.end]
    guards: list[tuple[int, str]] = []
    for no, content in body:
        match = _WRAP_GUARD_RE.match(content.strip().lower())
        if match is None:
            break
        guards.append((no, match.group(1)))
    endifs: list[int] = []
    for no, content in reversed(body):
        if content.strip().lower() != "endif":
            break
        endifs.append(no)
    guard_no = next((no for no, guard in guards if guard == flag), None)
    if not flag or guard_no is None or not endifs:
        log(f"auto-fix stale: {live} [{section.name}] guard ${flag}")
        return "stale"
    drop = {guard_no, endifs[0]}
    kept = [line for index, line in enumerate(lines, start=1) if index not in drop]
    live.write_bytes("".join(kept).encode(encoding))
    log(f"auto-fix undone: {live} [{section.name}] guard ${flag}")
    return "undone"


def _undo_unify(mods_root: Path, entry: dict, log: Callable[[str], None]) -> str:
    """Restore one recorded override_vertex_count line to its pre-unify text.

    Returns "undone" when the recorded new line was replaced with the old one
    and "stale" (prune) when the file or section no longer shows it."""
    target = _live_section(mods_root, entry, log)
    if target is None:
        return "stale"
    live, text, encoding, section = target
    new = str(entry.get("new", ""))
    old = str(entry.get("old", ""))
    wanted = new.strip().lower()
    lines = text.splitlines(keepends=True)
    body = [(no, content) for no, content in section.body if no <= section.end]
    body_nos = {no for no, _content in body}
    recorded = entry.get("line_no")
    line_no = (
        recorded
        if isinstance(recorded, int)
        and recorded in body_nos
        and _split_eol(lines[recorded - 1])[0].strip().lower() == wanted
        else None
    )
    if line_no is None and wanted:
        for no, content in body:
            if content.strip().lower() == wanted:
                line_no = no
                break
    if line_no is None or not old.strip():
        log(f"auto-fix stale: {live} [{section.name}] unify line")
        return "stale"
    _content, line_eol = _split_eol(lines[line_no - 1])
    lines[line_no - 1] = old + line_eol
    live.write_bytes("".join(lines).encode(encoding))
    log(f"auto-fix undone: {live} [{section.name}] unify")
    return "undone"
