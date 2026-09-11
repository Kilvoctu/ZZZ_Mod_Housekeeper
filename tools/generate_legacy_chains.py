"""Dev-only generator: harvest legacy hash-update chains from the 100 reference
per-character modules of ZZZ-Mod-Fixer into data/legacy_chains.json (mock-importing ``get_hash_commands``, dropping keys coupled to buffer/vertex/index content or to destructive comment/remove classes, and pairing ``log``/``update_hash`` entries into (from, to, label) transitions kept per character).

The companion exclusion file data/buffer_coupled_hashes.json records the buffer-coupled keys for the tools/pcdata.py gap-fill. Run:  python tools/generate_legacy_chains.py
"""

import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.model import HASH_FULL_RE, normalize_name

REFERENCE_DIR = Path(
    r"G:\randomDevStuff\ZZZ-Mod-Fixer\Source Codes\Assets\PlayerCharacterPYData"
)
OUTPUT_PATH = ROOT / "data" / "legacy_chains.json"
BUFFER_COUPLED_OUTPUT_PATH = ROOT / "data" / "buffer_coupled_hashes.json"

COMMAND_PARAMS = [
    "log",
    "update_hash",
    "comment_sections",
    "comment_commandlists",
    "remove_section",
    "remove_indexed_sections",
    "capture_section",
    "create_new_section",
    "transfer_indexed_sections",
    "multiply_section_if_missing",
    "add_ib_check_if_missing",
    "add_section_if_missing",
    "zzz_13_remap_texcoord",
    "zzz_12_shrink_texcoord_color",
    "update_buffer_blend_indices",
]

ARROW_RE = re.compile(r"^(?P<va>[\d.?]+[A-Za-z]*)\s*(?:->|→)\s*(?P<vb>[\d.?]+[A-Za-z]*)\s*:\s*(?P<desc>.*)$")
RANGE_RE = re.compile(r"^(?P<va>[\d.?]+[A-Za-z]*)\s*-\s*(?P<vb>[\d.?]+[A-Za-z]*)\s*:")
SINGLE_RE = re.compile(r"^(?P<v>[\d.]+[A-Za-z]*)\s*:")

BUFFER_COUPLED = {
    "zzz_13_remap_texcoord",
    "zzz_12_shrink_texcoord_color",
    "update_buffer_blend_indices",
    "transfer_indexed_sections",
    "remove_indexed_sections",
    "capture_section",
    "create_new_section",
}

DESTRUCTIVE = {"comment_sections", "comment_commandlists", "remove_section"}

CHAIN_CLASSES = ("log", "update_hash")


def load_module(path: Path):
    """Mock-import a reference module and return (commands, CHARACTER_INFO)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot create import spec for {path.name}")
    module = importlib.util.module_from_spec(spec)
    shims = {name: type(name, (), {}) for name in COMMAND_PARAMS}
    spec.loader.exec_module(module)
    commands = module.get_hash_commands(**shims)
    return commands, getattr(module, "CHARACTER_INFO", {})


def harvest_key(key: str, entries: list[tuple[type, tuple[Any, ...]]]) -> tuple[list[tuple[str, str, str]], str | None]:
    """Walk one hash key's command list in order.

    Returns (transitions, skip_reason): transitions are (from, to, label) triples and skip_reason is None when the key yielded transitions (or was a pure tolerated no-op) and one of "buffer-coupled"/"non-chain"/"undated"/"malformed" when the whole key must be dropped; arrow logs "A -> B: ..." and dash-range logs "A - B: ..." arm or re-arm pending as label "A -> B" (ranges normalized to the arrow form), and single-version logs "A: ..." arm pending only when nothing is armed yet.
    """
    for entry in entries:
        cls_name = getattr(entry[0], "__name__", None)
        if cls_name in BUFFER_COUPLED:
            return [], "buffer-coupled"
        if cls_name in DESTRUCTIVE:
            return [], "non-chain"

    transitions: list[tuple[str, str, str]] = []
    pending: tuple[str, tuple[str, ...]] | None = None
    for entry in entries:
        cls_name = entry[0].__name__
        if cls_name not in CHAIN_CLASSES:
            continue
        args = entry[1] if len(entry) > 1 else ()
        if cls_name == "log":
            text = args[0] if len(args) == 1 and isinstance(args[0], str) else None
            if isinstance(text, str):
                arrow = ARROW_RE.match(text) or RANGE_RE.match(text)
                if arrow:
                    pending = ("arrow", (arrow.group("va"), arrow.group("vb")))
                elif pending is None:
                    single = SINGLE_RE.match(text)
                    if single:
                        pending = ("single", (single.group("v"),))
            continue
        if (
            len(args) != 1
            or not isinstance(args[0], str)
            or not HASH_FULL_RE.match(args[0])
        ):
            return [], "malformed"
        if pending is None:
            return [], "undated"
        kind, versions = pending
        label = f"{versions[0]} -> {versions[1]}" if kind == "arrow" else versions[0]
        transitions.append((key.lower(), args[0].lower(), label))
        pending = None
    return transitions, None


def main() -> int:
    transitions: list[dict] = []
    skipped = Counter()
    buffer_coupled: set[str] = set()
    walked = errored = 0

    for path in sorted(REFERENCE_DIR.glob("*.py")):
        if path.stem == "__init__":
            continue
        walked += 1
        try:
            commands, info = load_module(path)
        except Exception as exc:  # noqa: BLE001
            errored += 1
            print(f"[error] {path.name}: {exc!r}")
            continue
        char = info.get("name") or path.stem
        characters = [normalize_name(char)]
        for key, entries in commands.items():
            if not isinstance(key, str) or not HASH_FULL_RE.match(key):
                continue
            harvested, reason = harvest_key(key, entries)
            if reason:
                skipped[reason] += 1
                if reason == "buffer-coupled":
                    buffer_coupled.add(key.lower())
                continue
            for from_hash, to_hash, label in harvested:
                transitions.append(
                    {
                        "from": from_hash,
                        "to": to_hash,
                        "characters": characters,
                        "label": label,
                    }
                )

    transitions.sort(
        key=lambda t: (t["from"], t["to"], t["label"], t["characters"])
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(transitions, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    buffer_coupled_hashes = sorted(buffer_coupled)
    with BUFFER_COUPLED_OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(buffer_coupled_hashes, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"modules walked: {walked} (errors: {errored})")
    print(
        "keys skipped:",
        {reason: skipped[reason] for reason in
         ("buffer-coupled", "non-chain", "undated", "malformed")},
    )
    print(f"distinct buffer-coupled hashes dropped: {len(buffer_coupled_hashes)}")
    print(f"transitions harvested: {len(transitions)}")
    print(f"distinct labels: {len({t['label'] for t in transitions})}")
    print(f"distinct from-hashes: {len({t['from'] for t in transitions})}")

    by_from_label: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(dict)
    for t in transitions:
        targets = by_from_label[(t["from"], t["label"])]
        targets.setdefault(t["to"], []).extend(t["characters"])
    conflicts = {k: v for k, v in by_from_label.items() if len(v) > 1}
    print(f"conflicts (same from+label -> different to): {len(conflicts)}")
    for (from_hash, label), targets in sorted(conflicts.items()):
        rendered = " | ".join(
            f"{to_hash} [{', '.join(sorted(chars))}]"
            for to_hash, chars in sorted(targets.items())
        )
        print(f"  {from_hash} ({label}): {rendered}")

    print(f"wrote {OUTPUT_PATH} ({len(transitions)} transitions)")
    print(
        f"wrote {BUFFER_COUPLED_OUTPUT_PATH} ({len(buffer_coupled_hashes)} hashes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
