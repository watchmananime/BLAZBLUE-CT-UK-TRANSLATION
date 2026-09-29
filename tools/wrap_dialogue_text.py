#!/usr/bin/env python3
"""Wrap Ukrainian dialogue using the advances of the in-game bitmap fonts."""

from __future__ import annotations

import argparse
import ast
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from build_patch import read_fpac


ROOT = Path(__file__).parents[1]
BASE_DIR = ROOT / "base"
TAG_RE = re.compile(r"<[^>]+>")
FIELD_RE = re.compile(r'^(msgctxt|msgid|msgstr) (".*")$')
ASW_MAGIC = b" WSA"
SIMPLE_MAGIC = 5


def po_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def po_field(name: str, value: str) -> list[str]:
    if "\n" not in value:
        return [f'{name} "{po_escape(value)}"']
    lines = [f'{name} ""']
    lines.extend(f'"{po_escape(part)}"' for part in value.splitlines(keepends=True))
    return lines


@dataclass
class AbcFont:
    max_codepoint: int
    mapping: list[int]
    glyphs: list[bytes]


def parse_abc(data: bytes) -> AbcFont:
    if len(data) < 26:
        raise ValueError("truncated ABC font")
    extended = data[:4] == ASW_MAGIC
    if not extended and struct.unpack_from("<I", data)[0] != SIMPLE_MAGIC:
        raise ValueError(f"unsupported ABC magic: {data[:4]!r}")
    max_codepoint = struct.unpack_from("<H", data, 0x14)[0]
    mapping_end = 0x16 + (max_codepoint + 1) * 2
    if mapping_end + 4 > len(data):
        raise ValueError("truncated ABC character map")
    mapping = list(struct.unpack_from(f"<{max_codepoint + 1}H", data, 0x16))
    glyph_count = struct.unpack_from("<I", data, mapping_end)[0]
    glyph_start = mapping_end + 4
    glyph_end = glyph_start + glyph_count * 24
    expected = glyph_end + (glyph_count * 32 if extended else 0)
    if expected != len(data):
        raise ValueError(f"unexpected ABC size: expected {expected}, got {len(data)}")
    glyphs = [
        data[glyph_start + index * 24 : glyph_start + (index + 1) * 24]
        for index in range(glyph_count)
    ]
    return AbcFont(max_codepoint, mapping, glyphs)


@dataclass(frozen=True)
class Target:
    path: Path
    limit: int
    metrics: "FontMetrics"
    maximum_lines: int | None
    reflow_existing_lines: bool


class FontMetrics:
    def __init__(self, font: AbcFont):
        self.font = font
        self.advances = [struct.unpack("<4f4H", glyph)[6] for glyph in font.glyphs]

    def width(self, text: str) -> int:
        result = 0
        for character in TAG_RE.sub("", text):
            codepoint = ord(character)
            if codepoint > self.font.max_codepoint:
                continue
            glyph = self.font.mapping[codepoint]
            if glyph < len(self.advances):
                result += self.advances[glyph]
        return result


def fpac_member(data: bytes, path: tuple[str, ...]) -> bytes:
    for name in path:
        entries = {entry.name: entry.data for entry in read_fpac(data)[1]}
        data = entries[name]
    return data


def load_metrics(pac: Path, abc_path: tuple[str, ...]) -> FontMetrics:
    data = pac.read_bytes()
    abc = fpac_member(data, abc_path)
    return FontMetrics(parse_abc(abc))


def parse_fields(lines: list[str]) -> dict[str, str]:
    values = {"msgctxt": "", "msgid": "", "msgstr": ""}
    active: str | None = None
    for raw_line in lines:
        match = FIELD_RE.match(raw_line)
        if match:
            active = match.group(1)
            values[active] = ast.literal_eval(match.group(2))
        elif raw_line.startswith('"') and active is not None:
            values[active] += ast.literal_eval(raw_line)
    return values


def wrap_line(line: str, limit: int, metrics: FontMetrics) -> list[str]:
    if not line or metrics.width(line) <= limit:
        return [line]

    # A single ordinary space is a safe word boundary. Runs of multiple spaces
    # are deliberately kept inside a unit because they encode Arakune's missing
    # letters and words and must not be collapsed by wrapping.
    units = re.split(r"(?<=\S) (?=\S)", line)
    output: list[str] = []
    current = units[0]
    for unit in units[1:]:
        candidate = current + " " + unit
        if metrics.width(candidate) <= limit:
            current = candidate
        else:
            output.append(current)
            current = unit
    output.append(current)
    return output


def wrap_value(
    value: str, limit: int, metrics: FontMetrics, reflow_existing_lines: bool
) -> str:
    if reflow_existing_lines:
        # Single newlines in the PO files are display wrapping, not paragraph
        # breaks. Reflow them as one line so rerunning this tool cannot wrap an
        # already wrapped line a second time. Keep leading/trailing newlines
        # and blank-line paragraph separators because the story scripts use
        # those for deliberate vertical spacing.
        leading = len(value) - len(value.lstrip("\n"))
        trailing = len(value) - len(value.rstrip("\n"))
        core_end = len(value) - trailing if trailing else len(value)
        core = value[leading:core_end]
        paragraphs = core.split("\n\n")
        wrapped = "\n\n".join(
            "\n".join(wrap_line(paragraph.replace("\n", " "), limit, metrics))
            for paragraph in paragraphs
        )
        return "\n" * leading + wrapped + "\n" * trailing
    output: list[str] = []
    for line in value.split("\n"):
        output.extend(wrap_line(line, limit, metrics))
    return "\n".join(output)


def replace_msgstr(lines: list[str], value: str) -> list[str]:
    start = next(index for index, line in enumerate(lines) if line.startswith("msgstr "))
    end = start + 1
    while end < len(lines) and lines[end].startswith('"'):
        end += 1
    return [*lines[:start], *po_field("msgstr", value), *lines[end:]]


def process(target: Target, apply: bool) -> tuple[int, list[str], list[str]]:
    text = target.path.read_text(encoding="utf-8")
    blocks = text.split("\n\n")
    changed = 0
    overflow: list[str] = []
    too_tall: list[str] = []
    rebuilt: list[str] = []
    for block in blocks:
        lines = block.splitlines()
        values = parse_fields(lines)
        if not values["msgctxt"] or not values["msgstr"]:
            rebuilt.append(block)
            continue
        wrapped = wrap_value(
            values["msgstr"],
            target.limit,
            target.metrics,
            target.reflow_existing_lines,
        )
        if wrapped != values["msgstr"]:
            lines = replace_msgstr(lines, wrapped)
            block = "\n".join(lines)
            changed += 1
        for number, line in enumerate(wrapped.split("\n"), 1):
            width = target.metrics.width(line)
            if width > target.limit:
                overflow.append(
                    f"{target.path.name}:{values['msgctxt']}:{number} ({width}>{target.limit})"
                )
        if target.maximum_lines is not None:
            visible_lines = len(wrapped.split("\n"))
            if visible_lines > target.maximum_lines:
                too_tall.append(
                    f"{target.path.name}:{values['msgctxt']} ({visible_lines} lines)"
                )
        rebuilt.append(block)

    if apply and changed:
        target.path.write_text("\n\n".join(rebuilt), encoding="utf-8", newline="\n")
    return changed, overflow, too_tall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write wrapped PO files")
    args = parser.parse_args()

    arcade_metrics = load_metrics(
        BASE_DIR / "data/ETC/Text_Eng/ArcEveRG.pac",
        ("abc.pac", "BaseFont0.abc"),
    )
    story_metrics = load_metrics(
        BASE_DIR / "data/Story/Text/eng/storytext_mainrg.pac",
        ("font.abc",),
    )
    targets = [
        *[
            Target(path, 806, arcade_metrics, 3, True)
            for path in sorted((ROOT / "uk/system").glob("ArcEve*.po"))
        ],
        Target(ROOT / "uk/system/WinMsg.po", 779, arcade_metrics, 3, True),
        *[
            Target(path, 887, story_metrics, None, True)
            for path in sorted((ROOT / "uk/story").glob("*.po"))
            if path.name != "speakers.po"
        ],
    ]

    total = 0
    overflow: list[str] = []
    too_tall: list[str] = []
    for target in targets:
        changed, file_overflow, file_too_tall = process(target, args.apply)
        if changed:
            print(f"{target.path.relative_to(ROOT)}: {changed}")
        total += changed
        overflow.extend(file_overflow)
        too_tall.extend(file_too_tall)

    print(f"entries_to_wrap={total}")
    print(f"unresolved_overflow={len(overflow)}")
    for item in overflow:
        print(f"OVERFLOW {item}")
    print(f"over_three_arcade_lines={len(too_tall)}")
    for item in too_tall:
        print(f"TALL {item}")
    return 1 if overflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
