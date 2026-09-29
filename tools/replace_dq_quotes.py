#!/usr/bin/env python3
"""Replace paired <DQ> markers with Ukrainian guillemets in translated text."""

from __future__ import annotations

from pathlib import Path

from wrap_dialogue_text import parse_fields, replace_msgstr


ROOT = Path(__file__).parents[1]


def replace_pairs(value: str) -> tuple[str, int]:
    marker_count = value.count("<DQ>")
    if marker_count % 2:
        raise ValueError(f"unpaired <DQ> marker in {value!r}")

    parts = value.split("<DQ>")
    output = parts[0]
    for index, part in enumerate(parts[1:], 1):
        output += ("«" if index % 2 else "»") + part
    return output, marker_count


def process(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    blocks = text.split("\n\n")
    changed_entries = 0
    replaced_markers = 0
    rebuilt: list[str] = []

    for block in blocks:
        lines = block.splitlines()
        fields = parse_fields(lines)
        value = fields["msgstr"]
        if not fields["msgctxt"] or "<DQ>" not in value:
            rebuilt.append(block)
            continue

        value, count = replace_pairs(value)
        rebuilt.append("\n".join(replace_msgstr(lines, value)))
        changed_entries += 1
        replaced_markers += count

    if changed_entries:
        path.write_text("\n\n".join(rebuilt), encoding="utf-8", newline="\n")
    return changed_entries, replaced_markers


def main() -> int:
    targets = [
        *sorted((ROOT / "uk/system").glob("ArcEve*.po")),
        *sorted((ROOT / "uk/story").glob("*.po")),
    ]
    total_entries = 0
    total_markers = 0
    for path in targets:
        entries, markers = process(path)
        if entries:
            print(f"{path.relative_to(ROOT)}: entries={entries}, markers={markers}")
        total_entries += entries
        total_markers += markers

    print(f"changed_entries={total_entries}")
    print(f"replaced_markers={total_markers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
