#!/usr/bin/env python3
"""Inspect the decrypted PSP EBOOT for strings, imports, and embedded TX assets."""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dump_static_textures as texture_dump  # noqa: E402


DEFAULT_TARGETS = [
    "軍事",
    "終了",
    "開発",
    "生産",
    "外交",
    "収入",
    "索敵",
    "部隊リスト",
    "人物リスト",
    "兵器リスト",
    "開発プランリスト",
    "対象エリアを選択して下さい",
    "通常戦闘",
    "敵開発技術入手",
    "MS技術LV",
    "MA技術LV",
    "兵器名",
    "エリア",
    "システム",
]


@dataclass
class Section:
    name: str
    type_id: int
    addr: int
    offset: int
    size: int


def read_c_string(data: bytes, offset: int, limit: int = 256) -> str:
    end = data.find(b"\0", offset, offset + limit)
    if end < 0:
        return ""
    raw = data[offset:end]
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return raw.decode("ascii", errors="replace")


def parse_elf_sections(data: bytes) -> list[Section]:
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("expected little-endian ELF32")

    (
        _e_type,
        _e_machine,
        _e_version,
        _e_entry,
        _e_phoff,
        e_shoff,
        _e_flags,
        _e_ehsize,
        _e_phentsize,
        _e_phnum,
        e_shentsize,
        e_shnum,
        e_shstrndx,
    ) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)

    raw_sections = [
        struct.unpack_from("<IIIIIIIIII", data, e_shoff + index * e_shentsize)
        for index in range(e_shnum)
    ]
    shstr = raw_sections[e_shstrndx]
    shstr_data = data[shstr[4] : shstr[4] + shstr[5]]

    sections: list[Section] = []
    for raw in raw_sections:
        name_offset, type_id, _flags, addr, offset, size, _link, _info, _align, _entsize = raw
        name = read_c_string(shstr_data, name_offset)
        sections.append(Section(name=name, type_id=type_id, addr=addr, offset=offset, size=size))
    return sections


def va_to_offset(sections: list[Section], va: int) -> int | None:
    for section in sections:
        if section.type_id == 8:
            continue
        if section.addr <= va < section.addr + section.size:
            return section.offset + (va - section.addr)
    return None


def offset_to_va(sections: list[Section], offset: int) -> int | None:
    for section in sections:
        if section.type_id == 8:
            continue
        if section.offset <= offset < section.offset + section.size:
            return section.addr + (offset - section.offset)
    return None


def section_by_name(sections: list[Section], name: str) -> Section | None:
    return next((section for section in sections if section.name == name), None)


def raw_jis(value: str) -> bytes:
    encoded = value.encode("iso2022_jp")
    output = bytearray()
    index = 0
    while index < len(encoded):
        if encoded[index] == 0x1B and index + 2 < len(encoded):
            index += 3
            continue
        output.append(encoded[index])
        index += 1
    return bytes(output)


def target_variants(value: str) -> list[tuple[str, bytes]]:
    variants: list[tuple[str, bytes]] = []
    for encoding in ("utf-8", "cp932", "euc_jp", "utf-16le", "utf-16be"):
        try:
            variants.append((encoding, value.encode(encoding)))
        except UnicodeEncodeError:
            pass

    try:
        jis = raw_jis(value)
    except UnicodeEncodeError:
        return variants

    if len(jis) % 2:
        return variants

    pairs = [jis[index : index + 2] for index in range(0, len(jis), 2)]
    jis_0 = bytes(byte - 0x20 for byte in jis)
    jis_1 = bytes(byte - 0x1F for byte in jis)
    variants.extend(
        [
            ("raw_jis", jis),
            ("jis_kuten_0based", jis_0),
            ("jis_kuten_1based", jis_1),
            ("raw_jis_u16le", b"".join(pair[::-1] for pair in pairs)),
            (
                "jis_kuten_0based_u16le",
                b"".join(bytes((pair[1] - 0x20, pair[0] - 0x20)) for pair in pairs),
            ),
            (
                "jis_kuten_1based_u16le",
                b"".join(bytes((pair[1] - 0x1F, pair[0] - 0x1F)) for pair in pairs),
            ),
        ]
    )
    return variants


def find_all(data: bytes, needle: bytes, limit: int = 20) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while len(offsets) < limit:
        offset = data.find(needle, cursor)
        if offset < 0:
            break
        offsets.append(offset)
        cursor = offset + 1
    return offsets


def print_imports(data: bytes, sections: list[Section]) -> None:
    lib_stub = section_by_name(sections, ".lib.stub")
    if lib_stub is None:
        print("\nImports: .lib.stub not found")
        return

    print("\nImports:")
    for offset in range(lib_stub.offset, lib_stub.offset + lib_stub.size, 20):
        name_va, version, flags, stub_size, count, nid_va, text_va = struct.unpack_from(
            "<IHHHHII", data, offset
        )
        name_offset = va_to_offset(sections, name_va)
        name = read_c_string(data, name_offset) if name_offset is not None else "?"
        nid_offset = va_to_offset(sections, nid_va)
        nids: list[str] = []
        if nid_offset is not None:
            for index in range(min(count, 8)):
                nids.append(f"0x{struct.unpack_from('<I', data, nid_offset + index * 4)[0]:08x}")
        suffix = "..." if count > 8 else ""
        print(
            f"- {name}: version=0x{version:04x} flags=0x{flags:04x} "
            f"stub_size={stub_size} count={count} text_va=0x{text_va:08x} "
            f"nids=[{', '.join(nids)}{suffix}]"
        )


def print_target_hits(data: bytes, sections: list[Section], targets: list[str]) -> None:
    print("\nTarget String Hits:")
    for target in targets:
        seen: set[bytes] = set()
        hits: list[tuple[str, list[int]]] = []
        for name, needle in target_variants(target):
            if needle in seen:
                continue
            seen.add(needle)
            offsets = find_all(data, needle, limit=10)
            if offsets:
                hits.append((name, offsets))
        print(f"- {target}:")
        if not hits:
            print("  no exact encoding/JIS/kuten match")
            continue
        for name, offsets in hits:
            rendered = ", ".join(
                f"off=0x{offset:x}/va={offset_to_va(sections, offset) and hex(offset_to_va(sections, offset))}"
                for offset in offsets
            )
            print(f"  {name}: {rendered}")


def print_magic_summary(data: bytes) -> None:
    print("\nMagic Offsets:")
    for magic in (
        texture_dump.PSET_MAGIC,
        texture_dump.MRG_MAGIC,
        texture_dump.TX_MAGIC,
        texture_dump.PL_MAGIC,
    ):
        offsets = find_all(data, magic, limit=64)
        sample = ", ".join(hex(offset) for offset in offsets[:12])
        suffix = "..." if len(offsets) > 12 else ""
        print(f"- {magic!r}: count_at_least={len(offsets)} sample=[{sample}{suffix}]")


def scan_valid_segments(data: bytes) -> tuple[list[texture_dump.Segment], list[texture_dump.Segment]]:
    tx_segments: list[texture_dump.Segment] = []
    pl_segments: list[texture_dump.Segment] = []

    cursor = 0
    while True:
        offset = data.find(texture_dump.TX_MAGIC, cursor)
        if offset < 0:
            break
        if texture_dump.valid_tx_at(data, offset):
            tx_segments.append(
                texture_dump.declared_segment(data, offset, f"/scan/{len(tx_segments)}", "/scan", len(tx_segments))
            )
        cursor = offset + 1

    cursor = 0
    while True:
        offset = data.find(texture_dump.PL_MAGIC, cursor)
        if offset < 0:
            break
        if texture_dump.valid_pl_at(data, offset):
            pl_segments.append(
                texture_dump.declared_segment(data, offset, f"/pl/{len(pl_segments)}", "/pl", len(pl_segments))
            )
        cursor = offset + 1

    return tx_segments, pl_segments


def choose_nearest_palette(
    tx_segment: texture_dump.Segment,
    pl_segments: list[texture_dump.Segment],
) -> texture_dump.Segment | None:
    if not pl_segments:
        return None
    previous = [segment for segment in pl_segments if segment.offset < tx_segment.offset]
    following = [segment for segment in pl_segments if segment.offset > tx_segment.offset]
    if previous and following:
        if tx_segment.offset - previous[-1].offset <= following[0].offset - tx_segment.offset:
            return previous[-1]
        return following[0]
    if previous:
        return previous[-1]
    return following[0] if following else None


def print_embedded_tx_summary(data: bytes, dump_root: Path | None, sheet_path: Path | None) -> None:
    tx_segments, pl_segments = scan_valid_segments(data)
    dims = Counter(
        (
            texture_dump.read_u16(segment.data, 8),
            texture_dump.read_u16(segment.data, 10),
        )
        for segment in tx_segments
    )
    print("\nEmbedded TX/PL:")
    print(f"- valid TX segments: {len(tx_segments)}")
    print(f"- valid PL segments: {len(pl_segments)}")
    for (width, height), count in sorted(dims.items()):
        print(f"  {width}x{height}: {count}")

    if dump_root is None and sheet_path is None:
        return

    rendered: list[tuple[str, Image.Image]] = []
    if dump_root is not None and dump_root.exists():
        import shutil

        shutil.rmtree(dump_root)
    if dump_root is not None:
        dump_root.mkdir(parents=True, exist_ok=True)

    for index, tx_segment in enumerate(tx_segments):
        pl_segment = choose_nearest_palette(tx_segment, pl_segments)
        if pl_segment is None:
            continue
        width = texture_dump.read_u16(tx_segment.data, 8)
        height = texture_dump.read_u16(tx_segment.data, 10)
        raw_profile = texture_dump.raw_palette_profile(pl_segment.data)
        pattern = texture_dump.infer_pattern(width, height, tx_segment.path, raw_profile)
        palette_order = texture_dump.palette_order_for_pattern(pattern, raw_profile)
        try:
            image, palette_colors, bpp = texture_dump.decode_tx_pl(
                tx_segment.data,
                pl_segment.data,
                palette_order=palette_order,
            )
        except ValueError:
            continue

        digest = hashlib.sha1(
            width.to_bytes(2, "little") + height.to_bytes(2, "little") + image.tobytes()
        ).hexdigest()
        category = texture_dump.classify_texture(image, palette_colors, bpp, pattern=pattern)
        name = f"{index:03d}-{digest[:12]}_{width}x{height}_tx{tx_segment.offset:x}_pl{pl_segment.offset:x}.png"
        if dump_root is not None:
            output = dump_root / category / name
            output.parent.mkdir(parents=True, exist_ok=True)
            image.save(output)
        flattened = Image.new("RGBA", image.size, (18, 18, 18, 255))
        flattened.alpha_composite(image.convert("RGBA"))
        rendered.append((name, flattened))

    if sheet_path is None or not rendered:
        return

    cols = 5
    cell_w = 260
    cell_h = 150
    rows = (len(rendered) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (30, 30, 30, 255))
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(rendered):
        scale = max(1, min(8, min(180 // max(1, image.width), 80 // max(1, image.height))))
        preview = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
        x = (index % cols) * cell_w
        y = (index // cols) * cell_h
        sheet.alpha_composite(preview, (x + 4, y + 4))
        draw.text((x + 4, y + 92), name[:36], fill=(255, 255, 255, 255))
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sheet_path)
    print(f"- embedded texture sheet: {sheet_path}")


def print_ascii_pointer_hits(data: bytes, sections: list[Section]) -> None:
    labels = ["NO", "WEAPON", "CHARA", "AREA", "TYPE", "MOVE START", "ALL SET", "SYSTEM.BIN"]
    print("\nASCII Label Pointer Hits:")
    for label in labels:
        label_bytes = label.encode("ascii")
        offsets = find_all(data, label_bytes, limit=20)
        rows: list[str] = []
        for offset in offsets:
            va = offset_to_va(sections, offset)
            if va is None:
                continue
            pointer_offsets = find_all(data, struct.pack("<I", va), limit=8)
            if pointer_offsets:
                rows.append(
                    f"label_off=0x{offset:x}/va=0x{va:x} ptrs="
                    + ",".join(hex(item) for item in pointer_offsets)
                )
        print(f"- {label}:")
        if rows:
            for row in rows:
                print(f"  {row}")
        else:
            print("  no direct pointer hit")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eboot", default="results/ULJS00178_EBOOT.BIN", help="decrypted EBOOT ELF")
    parser.add_argument(
        "--targets",
        help="comma-separated Japanese/UI strings to search. Defaults to known missing main UI labels.",
    )
    parser.add_argument("--dump-embedded-textures", help="optional output directory for valid embedded TX images")
    parser.add_argument("--sheet-out", help="optional embedded TX contact sheet path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    eboot_path = Path(args.eboot)
    data = eboot_path.read_bytes()
    sections = parse_elf_sections(data)
    targets = [item.strip() for item in args.targets.split(",")] if args.targets else DEFAULT_TARGETS
    targets = [item for item in targets if item]

    print(f"EBOOT: {eboot_path}")
    print(f"Size: {len(data)} bytes")
    print("\nSections:")
    for section in sections:
        if section.size:
            print(
                f"- {section.name or '<null>'}: addr=0x{section.addr:08x} "
                f"offset=0x{section.offset:x} size=0x{section.size:x} type={section.type_id}"
            )

    print_imports(data, sections)
    print_target_hits(data, sections, targets)
    print_magic_summary(data)
    print_embedded_tx_summary(
        data,
        Path(args.dump_embedded_textures) if args.dump_embedded_textures else None,
        Path(args.sheet_out) if args.sheet_out else None,
    )
    print_ascii_pointer_hits(data, sections)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
