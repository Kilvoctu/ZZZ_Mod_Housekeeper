"""Parse user quick hash-patch files from the app data folder.

Every *.txt directly inside data/ (not subfolders) may list quick hash renames, one per line ("old to new"; the separator may be "to", "->" or "→"); blank lines and lines starting with "#" are skipped, malformed lines are ignored, and later lines and later files override earlier ones.
"""

import re
from pathlib import Path

from .model import HASH_FULL_RE, ChangeEntry
from .repo import project_root

_ARROW_SPLIT_RE = re.compile(r"\s*(?:->|→|\bto\b)\s*")


def user_patches_dir() -> Path:
    """App data folder holding user quick patch files: <project root>/data."""
    return project_root() / "data"


def parse_user_patches(text: str) -> dict[str, ChangeEntry]:
    """Parse one patch file's text into {old_hash: ChangeEntry}, keyed by from_hash.

    Each valid line is one "old to new" rename with role "user"; malformed lines are skipped and later lines override earlier ones.
    """
    patches: dict[str, ChangeEntry] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = _ARROW_SPLIT_RE.split(stripped, maxsplit=1)
        if len(parts) != 2:
            continue
        old = parts[0].strip()
        new = parts[1].strip()
        if not HASH_FULL_RE.fullmatch(old) or not HASH_FULL_RE.fullmatch(new):
            continue
        patches[old.lower()] = ChangeEntry(
            from_hash=old.lower(), to_hash=new.lower(), role="user"
        )
    return patches


def load_user_patches() -> dict[str, ChangeEntry]:
    """Parse every top-level data/*.txt into one patch map; later files win."""
    patches: dict[str, ChangeEntry] = {}
    for path in sorted(user_patches_dir().glob("*.txt"), key=lambda item: str(item)):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gbk", errors="replace")
        patches.update(parse_user_patches(text))
    return patches
