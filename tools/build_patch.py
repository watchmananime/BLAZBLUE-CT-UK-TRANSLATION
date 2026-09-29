#!/usr/bin/env python3
"""Build a drop-in Ukrainian patch from bundled PAC bases and PO catalogs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent
TRANSLATION_DIR = TOOLS_DIR.parent
BASE_DIR = TRANSLATION_DIR / "base"
sys.path.insert(0, str(TOOLS_DIR))
ATF_MAGIC = 0x00465441


class BuildError(RuntimeError):
    pass


def parse_atf(data: bytes) -> list[tuple[str, str]]:
    if len(data) < 0x50 or u32(data, 0) != ATF_MAGIC:
        raise BuildError("not a supported ATF text file")

    params = [
        tuple(u32(data, 0x10 + index * 0x10 + field * 4) for field in range(3))
        for index in range(4)
    ]
    text_offset, text_count, _ = params[0]
    strings_offset, strings_count, _ = params[1]
    ascii_offset, _, _ = params[2]
    utf16_offset, _, _ = params[3]
    text_headers = [
        (u32(data, text_offset + index * 0x30), u32(data, text_offset + index * 0x30 + 4))
        for index in range(text_count)
    ]
    string_headers = [
        (u32(data, strings_offset + index * 0x10), u32(data, strings_offset + index * 0x10 + 4))
        for index in range(strings_count)
    ]

    records: list[tuple[str, str]] = []
    for name_index, text_index in text_headers:
        if name_index >= len(string_headers) or text_index >= len(string_headers):
            raise BuildError("ATF text header refers to a missing string header")
        name_top, name_len = string_headers[name_index]
        value_top, value_len = string_headers[text_index]
        name_size = max(0, name_len - 1)
        name_bytes = data[ascii_offset + name_top : ascii_offset + name_top + name_size]
        value_start = utf16_offset + value_top * 2
        value_bytes = data[value_start : value_start + value_len * 2]
        if len(name_bytes) != name_size or len(value_bytes) != value_len * 2:
            raise BuildError("ATF string points outside the file")
        key = name_bytes.decode("ascii", errors="replace")
        value = value_bytes.decode("utf-16-le").replace("\r\n", "\n").replace("\r", "\n")
        records.append((key, value))
    return records


def read_translations(path: Path) -> dict[tuple[str, str], str]:
    translations: dict[tuple[str, str], str] = {}
    values = {"msgctxt": "", "msgid": "", "msgstr": ""}
    active_field: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if not raw_line:
            if values["msgctxt"]:
                translations[(values["msgctxt"], values["msgid"])] = values["msgstr"]
            values = {"msgctxt": "", "msgid": "", "msgstr": ""}
            active_field = None
            continue
        match = re.match(r'^(msgctxt|msgid|msgstr) (".*")$', raw_line)
        if match:
            active_field = match.group(1)
            values[active_field] = ast.literal_eval(match.group(2))
        elif raw_line.startswith('"') and active_field is not None:
            values[active_field] += ast.literal_eval(raw_line)
    return translations


@dataclass(frozen=True)
class FpacEntry:
    name: str
    data: bytes
    table_offset: int
    name_size: int


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def align(value: int, boundary: int = 0x10) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def read_fpac(data: bytes) -> tuple[int, list[FpacEntry]]:
    if data[:4] != b"FPAC" or len(data) < 0x20:
        raise BuildError("not an FPAC archive")
    data_start, total_size, count, _unknown, name_size = struct.unpack_from(
        "<5I", data, 4
    )
    if total_size != len(data):
        raise BuildError(f"FPAC size mismatch: header={total_size}, actual={len(data)}")
    if not count:
        return data_start, []
    if data_start < 0x20 or (data_start - 0x20) % count:
        raise BuildError("invalid FPAC entry table")
    entry_size = (data_start - 0x20) // count
    if entry_size < name_size + 12:
        raise BuildError("invalid FPAC entry size")

    entries: list[FpacEntry] = []
    for index in range(count):
        top = 0x20 + index * entry_size
        raw_name = data[top : top + name_size].split(b"\0", 1)[0]
        name = raw_name.decode("ascii")
        relative_offset = u32(data, top + name_size + 4)
        size = u32(data, top + name_size + 8)
        start = data_start + relative_offset
        end = start + size
        if end > len(data):
            raise BuildError(f"FPAC member outside archive: {name}")
        entries.append(FpacEntry(name, data[start:end], top, name_size))
    return data_start, entries


def rebuild_fpac(original: bytes, replacements: dict[str, bytes]) -> bytes:
    data_start, entries = read_fpac(original)
    missing = set(replacements) - {entry.name for entry in entries}
    if missing:
        raise BuildError(f"missing FPAC members: {', '.join(sorted(missing))}")

    header = bytearray(original[:data_start])
    payload = bytearray()
    for index, entry in enumerate(entries):
        if index:
            payload.extend(b"\0" * (align(len(payload)) - len(payload)))
        member = replacements.get(entry.name, entry.data)
        struct.pack_into("<I", header, entry.table_offset + entry.name_size + 4, len(payload))
        struct.pack_into("<I", header, entry.table_offset + entry.name_size + 8, len(member))
        payload.extend(member)

    result = header + payload
    struct.pack_into("<I", result, 8, len(result))
    return bytes(result)


def patch_nested_fpac(
    data: bytes,
    patch_atf,
    patch_member,
    path: tuple[str, ...] = (),
) -> tuple[bytes, int]:
    _data_start, entries = read_fpac(data)
    replacements: dict[str, bytes] = {}
    changes = 0
    for entry in entries:
        child_path = (*path, entry.name)
        replacement = entry.data
        member_changes = 0
        if entry.data[:4] == b"FPAC":
            replacement, member_changes = patch_nested_fpac(
                entry.data, patch_atf, patch_member, child_path
            )
        elif entry.data[:4] == b"ATF\0":
            replacement, member_changes = patch_atf(entry.data, child_path)
        else:
            replacement, member_changes = patch_member(entry.data, child_path)
        if replacement != entry.data:
            replacements[entry.name] = replacement
        changes += member_changes
    if not replacements:
        return data, changes
    return rebuild_fpac(data, replacements), changes


def translations_for(path: Path) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    exact = read_translations(path)
    by_key: dict[str, str] = {}
    conflicts: set[str] = set()
    for (key, _source), translated in exact.items():
        if not translated:
            continue
        if key in by_key and by_key[key] != translated:
            conflicts.add(key)
        else:
            by_key[key] = translated
    for key in conflicts:
        by_key.pop(key, None)
    return exact, by_key


def rebuild_atf(
    original: bytes,
    exact: dict[tuple[str, str], str],
    by_key: dict[str, str],
    speaker_names: dict[str, str],
) -> tuple[bytes, int]:
    if len(original) < 0x50 or u32(original, 0) != ATF_MAGIC:
        raise BuildError("not a supported ATF file")

    text_offset, text_count, _text_size = struct.unpack_from("<3I", original, 0x10)
    strings_offset, strings_count, _strings_size = struct.unpack_from(
        "<3I", original, 0x20
    )
    _ascii_offset, _ascii_count, _ascii_size = struct.unpack_from("<3I", original, 0x30)
    utf16_offset, _utf16_count, _utf16_size = struct.unpack_from("<3I", original, 0x40)

    records = parse_atf(original)
    if len(records) != text_count:
        raise BuildError("ATF record count changed while parsing")

    values: dict[int, str] = {}
    changed = 0
    for index, (key, source) in enumerate(records):
        _name_index, value_index = struct.unpack_from(
            "<2I", original, text_offset + index * 0x30
        )
        translated = exact.get((key, source))
        if translated is None:
            translated = by_key.get(key)
        if translated is None and key.startswith("NAME_"):
            translated = speaker_names.get(source)
        value = translated if translated else source
        # ATF stores Windows newlines in its UTF-16 string pool.
        value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        previous = values.get(value_index)
        if previous is not None and previous != value:
            raise BuildError(f"shared ATF string has conflicting values: {key}")
        values[value_index] = value
        if translated and translated != source:
            changed += 1

    result = bytearray(original[:utf16_offset])
    pool = bytearray()
    for string_index in range(strings_count):
        if string_index not in values:
            continue
        value_bytes = values[string_index].encode("utf-16-le")
        value_units = len(value_bytes) // 2
        top = strings_offset + string_index * 0x10
        struct.pack_into("<II", result, top, len(pool) // 2, value_units)
        pool.extend(value_bytes)
        pool.extend(b"\0\0")

    result.extend(pool)
    struct.pack_into("<III", result, 0x40, utf16_offset, len(pool) // 2, len(pool))
    return bytes(result), changed


def build_text_pac(
    source: Path,
    po_path: Path,
    destination: Path,
    speaker_names: dict[str, str],
) -> int:
    exact, by_key = translations_for(po_path)

    def patch_atf(data: bytes, _path: tuple[str, ...]) -> tuple[bytes, int]:
        return rebuild_atf(data, exact, by_key, speaker_names)

    def patch_member(data: bytes, _path: tuple[str, ...]) -> tuple[bytes, int]:
        return data, 0

    rebuilt, changed = patch_nested_fpac(source.read_bytes(), patch_atf, patch_member)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(rebuilt)
    # Full structural validation after writing.
    patch_nested_fpac(destination.read_bytes(), lambda d, p: (d, 0), patch_member)
    return changed


def rebuild_menu_table(
    original: bytes,
    exact: dict[tuple[str, str], str],
    by_key: dict[str, str],
) -> tuple[bytes, int]:
    lines = original.decode("utf-16").splitlines()
    if len(lines) < 4 or lines[0] != "Reserve00" or lines[2] != "Reserve01":
        raise BuildError("unexpected gr_johchu menu table")
    changed = 0
    index = 4
    while index < len(lines):
        if not lines[index] or lines[index].startswith("//"):
            index += 1
            continue
        if index + 1 >= len(lines):
            raise BuildError(f"menu key without value: {lines[index]}")
        key, source = lines[index], lines[index + 1]
        translated = exact.get((key, source))
        if translated is None:
            translated = by_key.get(key)
        if translated and translated != source:
            lines[index + 1] = translated.replace("\r\n", "\n").replace("\r", "\n")
            changed += 1
        index += 2
    return ("\r\n".join(lines) + "\r\n").encode("utf-16"), changed


def build_menu_pac(
    source: Path,
    po_path: Path,
    destination: Path,
) -> int:
    exact, by_key = translations_for(po_path)

    def patch_atf(data: bytes, _path: tuple[str, ...]) -> tuple[bytes, int]:
        return data, 0

    def patch_member(data: bytes, path: tuple[str, ...]) -> tuple[bytes, int]:
        if path[-3:] == ("font.pac", "text.fontpac", "text.txt"):
            return rebuild_menu_table(data, exact, by_key)
        return data, 0

    rebuilt, changed = patch_nested_fpac(source.read_bytes(), patch_atf, patch_member)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(rebuilt)
    return changed


def write_manifest(root: Path) -> None:
    lines = []
    for path in sorted((root / "data").rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new, empty patch directory")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=BASE_DIR,
        help="PAC source tree (defaults to the bundled base directory)",
    )
    args = parser.parse_args()
    source_dir = args.source_dir.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    base_data = source_dir / "data"
    if not base_data.is_dir():
        parser.error(f"PAC source tree is missing: {base_data}")
    shutil.copytree(base_data, output / "data", dirs_exist_ok=True)

    speaker_exact, _speaker_by_key = translations_for(
        TRANSLATION_DIR / "uk/story/speakers.po"
    )
    speaker_names = {
        source: translated
        for (_key, source), translated in speaker_exact.items()
        if translated
    }
    total_records = 0
    story_source = source_dir / "data/Story/Text/eng"
    for source in sorted(story_source.glob("*.pac")):
        po_stem = "storytext_mainrg" if source.stem.startswith("storytext_ex") else source.stem
        po_path = TRANSLATION_DIR / f"uk/story/{po_stem}.po"
        if not po_path.exists():
            continue
        destination = output / "data/Story/Text/eng" / source.name
        changed = build_text_pac(
            source,
            po_path,
            destination,
            speaker_names,
        )
        total_records += changed

    system_source = source_dir / "data/ETC/Text_Eng"
    for po_path in sorted((TRANSLATION_DIR / "uk/system").glob("*.po")):
        source = system_source / f"{po_path.stem}.pac"
        if not source.exists():
            raise BuildError(f"missing source PAC for {po_path.name}: {source}")
        destination = output / "data/ETC/Text_Eng" / source.name
        changed = build_text_pac(
            source,
            po_path,
            destination,
            {},
        )
        total_records += changed

    menu_po = TRANSLATION_DIR / "uk/menu/menu.po"
    menu_source = source_dir / "data/gr/gr_johchu_eng.pac"
    menu_destination = output / "data/gr/gr_johchu_eng.pac"
    menu_changed = build_menu_pac(
        menu_source,
        menu_po,
        menu_destination,
    )
    total_records += menu_changed

    write_manifest(output)
    print(f"Built patch: {output}")
    print(f"Translated records applied: {total_records}")
    patch_file_count = sum(
        1 for p in (output / "data").rglob("*") if p.is_file()
    )
    print(f"Files: {patch_file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
