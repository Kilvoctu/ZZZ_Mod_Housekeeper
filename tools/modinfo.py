"""Derive readable mod info from 3DMigoto .ini files.

Single-pass parser collecting key bindings for [Key*] sections,
texture-swap bindings and TextureOverride hashes for the "Mod info" dialog.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .backups import included_ini_files
from .fixer import read_ini_text

_SECTION_RE = re.compile(r"^\[(.+)]$")
_KEY_RE = re.compile(r"^key\s*=\s*(.+)$", re.IGNORECASE)
_BACK_RE = re.compile(r"^back\s*=\s*(.+)$", re.IGNORECASE)
_VAR_RE = re.compile(r"^\$(\w+)\s*=\s*(.+)$")
_STATE_NAME_RE = re.compile(r"^\s*;\s*\[([^]]+)]")
_SWAP_RE = re.compile(r"^(ps-t\d+)\s*=\s*(.+)$")
_HASH_RE = re.compile(r"^\s*hash\s*=\s*(.+)$")
_TEXTUREOVERRIDE_PREFIX = "textureoverride"
_KEY_SECTION_PREFIX = "key"
_LABEL_PREFIXES = ("swapswapvar", "keyswapvar", "keyswap", "swapvar", "key", "swap")
_FUNCTION_KEY_RE = re.compile(r"^VK_F([1-9]|1[0-9]|2[0-4])$")
_NUMPAD_RE = re.compile(r"^VK_NUMPAD([0-9])$")
_SINGLE_CHAR_RE = re.compile(r"^VK_([0-9A-Za-z])$")

_FRIENDLY_KEYS = {
    "VK_UP": "↑",
    "UP": "↑",
    "VK_DOWN": "↓",
    "DOWN": "↓",
    "VK_LEFT": "←",
    "LEFT": "←",
    "VK_RIGHT": "→",
    "RIGHT": "→",
    "VK_CONTROL": "Ctrl",
    "VK_LCONTROL": "Ctrl",
    "VK_RCONTROL": "Ctrl",
    "CTRL": "Ctrl",
    "VK_SHIFT": "Shift",
    "VK_LSHIFT": "Shift",
    "VK_RSHIFT": "Shift",
    "SHIFT": "Shift",
    "VK_MENU": "Alt",
    "VK_LMENU": "Alt",
    "VK_RMENU": "Alt",
    "ALT": "Alt",
    "VK_LBUTTON": "LMB",
    "VK_RBUTTON": "RMB",
    "VK_MBUTTON": "MMB",
    "VK_XBUTTON1": "M4",
    "VK_XBUTTON2": "M5",
    "VK_PRIOR": "PgUp",
    "VK_NEXT": "PgDn",
    "VK_INSERT": "Ins",
    "VK_DELETE": "Del",
    "VK_HOME": "Home",
    "VK_END": "End",
    "VK_SPACE": "Space",
    "VK_RETURN": "Enter",
    "VK_ESCAPE": "Esc",
    "VK_TAB": "Tab",
    "VK_BACK": "Backspace",
    "VK_CAPITAL": "Caps",
    "VK_SCROLL": "Scroll",
    "VK_NUMLOCK": "NumLock",
    "VK_SNAPSHOT": "PrtSc",
    "VK_PAUSE": "Pause",
    "VK_ADD": "Num +",
    "VK_SUBTRACT": "Num -",
    "VK_MULTIPLY": "Num *",
    "VK_DIVIDE": "Num /",
    "VK_DECIMAL": "Num .",
}


@dataclass(frozen=True)
class KeyToggle:
    """One [Key*] toggle: cleaned label, friendly key combo and its cycle.

    Covers `key =` and `back =` rows; back bindings carry a " (Back)" suffix.
    """

    key: str
    label: str
    key_display: str
    variable: str | None
    cycle: tuple[str, ...]
    state_names: tuple[str, ...]
    section: str


@dataclass(frozen=True)
class TextureSwap:
    """One texture-slot binding inside a section."""

    section: str
    slot: str
    resource: str


@dataclass(frozen=True)
class Override:
    """One TextureOverride section and its (lowercased) hash."""

    section: str
    hash_value: str | None


@dataclass(frozen=True)
class ModIniInfo:
    """All mod-info facts derived from one .ini file."""

    path: str
    toggles: list[KeyToggle] = field(default_factory=list)
    swaps: list[TextureSwap] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)


def _split_cycle(value: str) -> tuple[str, ...]:
    return tuple(token.strip() for token in value.split(",") if token.strip())


def _friendly_key(token: str) -> str:
    upper = token.upper()
    if upper in _FRIENDLY_KEYS:
        return _FRIENDLY_KEYS[upper]
    function_match = _FUNCTION_KEY_RE.fullmatch(upper)
    if function_match is not None:
        return "F" + function_match.group(1)
    numpad_match = _NUMPAD_RE.fullmatch(upper)
    if numpad_match is not None:
        return "NUM " + numpad_match.group(1)
    single_match = _SINGLE_CHAR_RE.fullmatch(upper)
    if single_match is not None:
        return upper[3]
    if upper.startswith("XB_"):
        return "XB " + upper[3:].replace("_", " ")
    if upper.startswith("PS_"):
        return "PS " + upper[3:].replace("_", " ")
    return upper


def _key_display(value: str) -> str:
    tokens = [
        token
        for token in value.split()
        if not token.lower().startswith("no_")
    ]
    if not tokens:
        return ""
    return "+".join(_friendly_key(token) for token in tokens)


def _toggle_label(section: str) -> str:
    lower = section.lower()
    for prefix in _LABEL_PREFIXES:
        if lower.startswith(prefix):
            remainder = section[len(prefix):]
            break
    else:
        remainder = section
    label = remainder.strip("_").strip()
    return label if label else "Toggle"


def parse_ini_info(text: str, display_path: str = "") -> ModIniInfo:
    """Single-pass mod-info facts for one decoded .ini text."""
    states: list[str] = []
    seen_states: set[str] = set()
    swaps: list[TextureSwap] = []
    overrides: list[Override] = []
    pending: list[tuple[str, str | None, tuple[str, ...], str, bool]] = []
    current: str | None = None
    key_values: list[str] = []
    back_values: list[str] = []
    variable: str | None = None
    cycle: tuple[str, ...] = ()
    hash_value: str | None = None

    def emit_pending(key_section: str) -> None:
        if not key_section.lower().startswith(_KEY_SECTION_PREFIX):
            return
        for key_value in key_values:
            pending.append((key_value, variable, cycle, key_section, False))
        for back_value in back_values:
            pending.append((back_value, variable, cycle, key_section, True))

    for raw in text.split("\n"):
        line = raw.strip()
        section_match = _SECTION_RE.match(line)
        if section_match is not None:
            if current is not None:
                if current.lower().startswith(_TEXTUREOVERRIDE_PREFIX):
                    overrides.append(Override(section=current, hash_value=hash_value))
                emit_pending(current)
            current = section_match.group(1)
            key_values = []
            back_values = []
            variable = None
            cycle = ()
            hash_value = None
            continue
        state_match = _STATE_NAME_RE.match(line)
        if state_match is not None:
            name = state_match.group(1)
            if name not in seen_states:
                seen_states.add(name)
                states.append(name)
            continue
        if current is None:
            continue
        key_match = _KEY_RE.match(line)
        if key_match is not None:
            key_values.append(key_match.group(1).strip())
            continue
        back_match = _BACK_RE.match(line)
        if back_match is not None:
            back_values.append(back_match.group(1).strip())
            continue
        var_match = _VAR_RE.match(line)
        if var_match is not None and variable is None:
            variable = "$" + var_match.group(1)
            cycle = _split_cycle(var_match.group(2))
            continue
        swap_match = _SWAP_RE.match(line)
        if swap_match is not None:
            swaps.append(
                TextureSwap(
                    section=current,
                    slot=swap_match.group(1),
                    resource=swap_match.group(2).strip(),
                )
            )
            continue
        if hash_value is None:
            hash_match = _HASH_RE.match(line)
            if hash_match is not None:
                hash_value = re.sub(r"\s+", "", hash_match.group(1).strip().lower())

    if current is not None:
        if current.lower().startswith(_TEXTUREOVERRIDE_PREFIX):
            overrides.append(Override(section=current, hash_value=hash_value))
        emit_pending(current)

    state_names = tuple(states)
    toggles: list[KeyToggle] = []
    for raw_value, var, cyc, section, is_back in pending:
        display = _key_display(raw_value)
        if display == "":
            continue
        suffix = " (Back)" if is_back else ""
        toggles.append(
            KeyToggle(
                key=raw_value,
                label=_toggle_label(section) + suffix,
                key_display=display,
                variable=var,
                cycle=cyc,
                state_names=state_names,
                section=section,
            )
        )
    return ModIniInfo(path=display_path, toggles=toggles, swaps=swaps, overrides=overrides)


def scan_mod_info(mod_dir: Path) -> list[ModIniInfo]:
    """Mod-info facts for every included .ini under mod_dir, in order.

    Files failing to decode are skipped; display paths are mod_dir-relative posix, falling back to the absolute string path.
    """
    mod_dir = Path(mod_dir)
    infos: list[ModIniInfo] = []
    for path in included_ini_files(mod_dir):
        try:
            text = read_ini_text(path)
        except (ValueError, OSError):
            continue
        try:
            display = path.relative_to(mod_dir).as_posix()
        except ValueError:
            display = str(path)
        infos.append(parse_ini_info(text, display_path=display))
    return infos


def read_mod_author(mod_dir: Path) -> str | None:
    """Author string from mod.json inside mod_dir, or None.

    Returns None for a missing, unparsable or non-object file, or when the author value is missing, empty or "unknown" (case-insensitive).
    """
    mod_dir = Path(mod_dir)
    path = mod_dir / "mod.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    author = data.get("author")
    if not isinstance(author, str):
        return None
    author = author.strip()
    if not author or author.lower() == "unknown":
        return None
    return author
