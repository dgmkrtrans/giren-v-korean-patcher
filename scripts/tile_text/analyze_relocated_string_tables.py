#!/usr/bin/env python3
import csv
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path


PT_LOAD = 1
SHT_NOBITS = 8


@dataclass(frozen=True)
class Section:
    index: int
    name: str
    sh_type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    align: int
    entsize: int


@dataclass(frozen=True)
class Reloc:
    section: str
    record_off: int
    target_section: str
    target_vma: int
    target_file_off: int
    r_type: int
    sym: int
    value: int


class Elf32:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if self.data[:4] != b"\x7fELF" or self.data[4] != 1 or self.data[5] != 1:
            raise ValueError("expected little-endian ELF32")
        hdr = struct.unpack_from("<16sHHIIIIIHHHHHH", self.data, 0)
        (
            _ident,
            self.e_type,
            self.e_machine,
            self.e_version,
            self.entry,
            self.phoff,
            self.shoff,
            self.flags,
            self.ehsize,
            self.phentsize,
            self.phnum,
            self.shentsize,
            self.shnum,
            self.shstrndx,
        ) = hdr
        self.program_headers = [
            struct.unpack_from("<IIIIIIII", self.data, self.phoff + i * self.phentsize)
            for i in range(self.phnum)
        ]
        self.sections = self._read_sections()
        self.by_name = {s.name: s for s in self.sections}

    def _read_sections(self):
        raw = [
            struct.unpack_from("<IIIIIIIIII", self.data, self.shoff + i * self.shentsize)
            for i in range(self.shnum)
        ]
        shstr = raw[self.shstrndx]
        names = self.data[shstr[4] : shstr[4] + shstr[5]]

        def name_at(off):
            end = names.find(b"\0", off)
            return names[off:end].decode("ascii", "replace")

        out = []
        for i, vals in enumerate(raw):
            name, sh_type, flags, addr, off, size, link, info, align, entsize = vals
            out.append(Section(i, name_at(name), sh_type, flags, addr, off, size, link, info, align, entsize))
        return out

    def vma_to_off(self, vma):
        for p_type, off, vaddr, _paddr, filesz, _memsz, _flags, _align in self.program_headers:
            if p_type == PT_LOAD and vaddr <= vma < vaddr + filesz:
                return off + (vma - vaddr)
        return None

    def off_to_vma(self, off):
        for p_type, p_off, vaddr, _paddr, filesz, _memsz, _flags, _align in self.program_headers:
            if p_type == PT_LOAD and p_off <= off < p_off + filesz:
                return vaddr + (off - p_off)
        return None

    def section_for_vma(self, vma):
        best = None
        for sec in self.sections:
            if sec.sh_type == SHT_NOBITS:
                continue
            if sec.addr <= vma < sec.addr + sec.size:
                if best is None or sec.size < best.size:
                    best = sec
        return best

    def section_for_off(self, off):
        best = None
        for sec in self.sections:
            if sec.sh_type == SHT_NOBITS:
                continue
            if sec.offset <= off < sec.offset + sec.size:
                if best is None or sec.size < best.size:
                    best = sec
        return best

    def relocations(self):
        relocs = []
        for relsec in self.sections:
            if not relsec.name.startswith(".rel.") or relsec.size == 0:
                continue
            target = self.by_name.get(relsec.name[4:])
            if not target:
                continue
            for pos in range(relsec.offset, relsec.offset + relsec.size, 8):
                r_off, r_info = struct.unpack_from("<II", self.data, pos)
                if r_off < target.size:
                    loc_vma = target.addr + r_off
                else:
                    loc_vma = r_off
                loc_off = self.vma_to_off(loc_vma)
                if loc_off is None:
                    continue
                value = struct.unpack_from("<I", self.data, loc_off)[0]
                relocs.append(
                    Reloc(relsec.name, pos, target.name, loc_vma, loc_off, r_info & 0xFF, r_info >> 8, value)
                )
        return relocs


def read_zero_terminated(data, off, limit):
    end_limit = min(len(data), off + limit)
    end = data.find(b"\0", off, end_limit)
    if end < 0:
        end = end_limit
    return data[off:end]


def printable_ratio(text):
    good = 0
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\t\r\n" or (cat and cat[0] != "C"):
            good += 1
    return good / max(1, len(text))


def has_japanese(text):
    return any(
        ("\u3040" <= ch <= "\u309f")
        or ("\u30a0" <= ch <= "\u30ff")
        or ("\uff61" <= ch <= "\uff9f")
        or ("\u3400" <= ch <= "\u9fff")
        for ch in text
    )


def looks_like_utf8_mojibake(text):
    markers = ("繧", "繝", "縺", "荳", "譁", "邱", "鬆")
    return sum(text.count(marker) for marker in markers) >= 2


def decode_cp932(data, off, limit=256):
    raw = read_zero_terminated(data, off, limit)
    if len(raw) < 2:
        return None
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        return None
    try:
        utf8_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        utf8_text = ""
    if utf8_text and has_japanese(utf8_text) and looks_like_utf8_mojibake(text):
        return None
    if printable_ratio(text) < 0.95:
        return None
    return raw, text


def escaped(text):
    return text.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def runs_from_relocated_rodata_pointers(elf):
    rodata_names = {".rodata", ".rodata.0001", ".rodata.0002", ".rodata.0003"}
    entries = []
    for rel in elf.relocations():
        if rel.target_section != ".data" or rel.r_type != 2:
            continue
        pointed_sec = elf.section_for_vma(rel.value)
        if not pointed_sec or pointed_sec.name not in rodata_names:
            continue
        target_off = elf.vma_to_off(rel.value)
        decoded = decode_cp932(elf.data, target_off)
        if not decoded:
            continue
        raw, text = decoded
        entries.append(
            {
                "pointer_vma": rel.target_vma,
                "pointer_file_off": rel.target_file_off,
                "reloc_record_off": rel.record_off,
                "raw_pointer_value": rel.value,
                "target_vma": rel.value,
                "target_file_off": target_off,
                "target_section": pointed_sec.name,
                "text_raw": raw,
                "text": text,
                "has_japanese": has_japanese(text),
            }
        )
    entries.sort(key=lambda e: e["pointer_vma"])

    runs = []
    cur = []
    for e in entries:
        if cur and e["pointer_vma"] != cur[-1]["pointer_vma"] + 4:
            runs.append(cur)
            cur = []
        cur.append(e)
    if cur:
        runs.append(cur)

    filtered = []
    for run in runs:
        jp_count = sum(1 for e in run if e["has_japanese"])
        if jp_count == 0:
            continue
        if len(run) == 1 and not run[0]["has_japanese"]:
            continue
        filtered.append(run)
    return filtered


def add_capacity(run):
    unique_targets = sorted({e["target_file_off"] for e in run})
    next_by_target = {}
    for i, off in enumerate(unique_targets):
        later = [x for x in unique_targets[i + 1 :] if x > off]
        next_by_target[off] = later[0] if later else None
    for e in run:
        next_off = next_by_target[e["target_file_off"]]
        raw_len = len(e["text_raw"])
        if next_off is None:
            slot = ""
            cap = ""
            slack = ""
            next_hex = ""
        else:
            slot = next_off - e["target_file_off"]
            cap = max(0, slot - 1)
            slack = cap - raw_len
            next_hex = f"0x{next_off:07x}"
        e["next_target_file_off_in_run"] = next_hex
        e["slot_bytes_to_next_target"] = slot
        e["max_bytes_before_next_target"] = cap
        e["slack_bytes_before_next_target"] = slack


def display_runs_from_runs(runs):
    out = []
    for run in runs:
        current = []
        for e in run:
            if not e["has_japanese"]:
                if current:
                    out.append(current)
                    current = []
                continue
            if current and e["pointer_vma"] != current[-1]["pointer_vma"] + 4:
                out.append(current)
                current = []
            current.append(e)
        if current:
            out.append(current)
    return out


def collect_data_inline_unit_name_table(elf):
    # This table was identified from the repeated 0x50-byte records in .data.
    # The short unit name is an inline cp932 field at record +0x0c.
    start = 0x00161600
    stride = 0x50
    count = 579
    name_offset = 0x0C
    slot_bytes = 0x0C
    rows = []
    for idx in range(count):
        record_vma = start + idx * stride
        field_vma = record_vma + name_offset
        field_off = elf.vma_to_off(field_vma)
        if field_off is None:
            continue
        raw = elf.data[field_off : field_off + slot_bytes]
        nul = raw.find(b"\0")
        text_raw = raw[:nul] if nul >= 0 else raw
        try:
            text = text_raw.decode("cp932")
        except UnicodeDecodeError:
            text = ""
        rows.append(
            {
                "table_name": "data_unit_records_short_name",
                "entry_index": idx,
                "record_file_off": f"0x{record_vma:07x}",
                "record_vma": f"0x{record_vma:08x}",
                "string_file_off": f"0x{field_off:07x}",
                "string_vma": f"0x{field_vma:08x}",
                "record_stride_bytes": stride,
                "field_offset_in_record": name_offset,
                "slot_bytes_including_nul": slot_bytes,
                "max_bytes_before_nul": slot_bytes - 1,
                "byte_len": len(text_raw),
                "char_len": len(text),
                "slack_bytes_before_nul": (slot_bytes - 1) - len(text_raw),
                "text": escaped(text),
            }
        )
    return rows


def write_tsv(path, rows, fields):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fields, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    elf = Elf32(Path("EBOOT.BIN"))
    runs = runs_from_relocated_rodata_pointers(elf)
    for run in runs:
        add_capacity(run)
    display_runs = display_runs_from_runs(runs)
    data_inline_rows = collect_data_inline_unit_name_table(elf)

    table_rows = []
    pointer_rows = []
    for idx, run in enumerate(runs, 1):
        targets = sorted({e["target_file_off"] for e in run})
        deltas = [b - a for a, b in zip(targets, targets[1:]) if b > a]
        common_delta = max(set(deltas), key=deltas.count) if deltas else ""
        sample = " | ".join(escaped(e["text"])[:40] for e in run[:5])
        table_rows.append(
            {
                "table_id": idx,
                "pointer_file_off_start": f"0x{run[0]['pointer_file_off']:07x}",
                "pointer_file_off_end": f"0x{run[-1]['pointer_file_off']:07x}",
                "pointer_vma_start": f"0x{run[0]['pointer_vma']:08x}",
                "pointer_vma_end": f"0x{run[-1]['pointer_vma']:08x}",
                "entry_count": len(run),
                "unique_target_count": len(targets),
                "target_file_off_min": f"0x{min(targets):07x}",
                "target_file_off_max": f"0x{max(targets):07x}",
                "common_target_stride_bytes": common_delta,
                "sample": sample,
            }
        )
        for ordinal, e in enumerate(run):
            row = {
                "table_id": idx,
                "entry_index": ordinal,
                "pointer_file_off": f"0x{e['pointer_file_off']:07x}",
                "pointer_vma": f"0x{e['pointer_vma']:08x}",
                "reloc_record_off": f"0x{e['reloc_record_off']:07x}",
                "raw_pointer_value": f"0x{e['raw_pointer_value']:08x}",
                "target_file_off": f"0x{e['target_file_off']:07x}",
                "target_vma": f"0x{e['target_vma']:08x}",
                "target_section": e["target_section"],
                "byte_len": len(e["text_raw"]),
                "char_len": len(e["text"]),
                "next_target_file_off_in_run": e["next_target_file_off_in_run"],
                "slot_bytes_to_next_target": e["slot_bytes_to_next_target"],
                "max_bytes_before_next_target": e["max_bytes_before_next_target"],
                "slack_bytes_before_next_target": e["slack_bytes_before_next_target"],
                "text": escaped(e["text"]),
            }
            pointer_rows.append(row)

    display_table_rows = []
    display_pointer_rows = []
    for idx, run in enumerate(display_runs, 1):
        targets = sorted({e["target_file_off"] for e in run})
        deltas = [b - a for a, b in zip(targets, targets[1:]) if b > a]
        common_delta = max(set(deltas), key=deltas.count) if deltas else ""
        sample = " | ".join(escaped(e["text"])[:40] for e in run[:5])
        display_table_rows.append(
            {
                "display_table_id": idx,
                "pointer_file_off_start": f"0x{run[0]['pointer_file_off']:07x}",
                "pointer_file_off_end": f"0x{run[-1]['pointer_file_off']:07x}",
                "pointer_vma_start": f"0x{run[0]['pointer_vma']:08x}",
                "pointer_vma_end": f"0x{run[-1]['pointer_vma']:08x}",
                "entry_count": len(run),
                "unique_target_count": len(targets),
                "target_file_off_min": f"0x{min(targets):07x}",
                "target_file_off_max": f"0x{max(targets):07x}",
                "common_target_stride_bytes": common_delta,
                "sample": sample,
            }
        )
        for ordinal, e in enumerate(run):
            display_pointer_rows.append(
                {
                    "display_table_id": idx,
                    "entry_index": ordinal,
                    "pointer_file_off": f"0x{e['pointer_file_off']:07x}",
                    "pointer_vma": f"0x{e['pointer_vma']:08x}",
                    "reloc_record_off": f"0x{e['reloc_record_off']:07x}",
                    "raw_pointer_value": f"0x{e['raw_pointer_value']:08x}",
                    "target_file_off": f"0x{e['target_file_off']:07x}",
                    "target_vma": f"0x{e['target_vma']:08x}",
                    "target_section": e["target_section"],
                    "byte_len": len(e["text_raw"]),
                    "char_len": len(e["text"]),
                    "next_target_file_off_in_run": e["next_target_file_off_in_run"],
                    "slot_bytes_to_next_target": e["slot_bytes_to_next_target"],
                    "max_bytes_before_next_target": e["max_bytes_before_next_target"],
                    "slack_bytes_before_next_target": e["slack_bytes_before_next_target"],
                    "text": escaped(e["text"]),
                }
            )

    write_tsv(
        "relocated_pointer_tables.tsv",
        table_rows,
        [
            "table_id",
            "pointer_file_off_start",
            "pointer_file_off_end",
            "pointer_vma_start",
            "pointer_vma_end",
            "entry_count",
            "unique_target_count",
            "target_file_off_min",
            "target_file_off_max",
            "common_target_stride_bytes",
            "sample",
        ],
    )
    write_tsv(
        "relocated_string_pointers.tsv",
        pointer_rows,
        [
            "table_id",
            "entry_index",
            "pointer_file_off",
            "pointer_vma",
            "reloc_record_off",
            "raw_pointer_value",
            "target_file_off",
            "target_vma",
            "target_section",
            "byte_len",
            "char_len",
            "next_target_file_off_in_run",
            "slot_bytes_to_next_target",
            "max_bytes_before_next_target",
            "slack_bytes_before_next_target",
            "text",
        ],
    )
    write_tsv(
        "relocated_display_pointer_tables.tsv",
        display_table_rows,
        [
            "display_table_id",
            "pointer_file_off_start",
            "pointer_file_off_end",
            "pointer_vma_start",
            "pointer_vma_end",
            "entry_count",
            "unique_target_count",
            "target_file_off_min",
            "target_file_off_max",
            "common_target_stride_bytes",
            "sample",
        ],
    )
    write_tsv(
        "relocated_display_string_pointers.tsv",
        display_pointer_rows,
        [
            "display_table_id",
            "entry_index",
            "pointer_file_off",
            "pointer_vma",
            "reloc_record_off",
            "raw_pointer_value",
            "target_file_off",
            "target_vma",
            "target_section",
            "byte_len",
            "char_len",
            "next_target_file_off_in_run",
            "slot_bytes_to_next_target",
            "max_bytes_before_next_target",
            "slack_bytes_before_next_target",
            "text",
        ],
    )
    write_tsv(
        "data_inline_string_tables.tsv",
        [
            {
                "table_name": "data_unit_records_short_name",
                "record_file_off_start": data_inline_rows[0]["record_file_off"],
                "record_file_off_end": data_inline_rows[-1]["record_file_off"],
                "record_vma_start": data_inline_rows[0]["record_vma"],
                "record_vma_end": data_inline_rows[-1]["record_vma"],
                "entry_count": len(data_inline_rows),
                "record_stride_bytes": data_inline_rows[0]["record_stride_bytes"],
                "field_offset_in_record": data_inline_rows[0]["field_offset_in_record"],
                "slot_bytes_including_nul": data_inline_rows[0]["slot_bytes_including_nul"],
                "max_bytes_before_nul": data_inline_rows[0]["max_bytes_before_nul"],
                "sample": " | ".join(row["text"] for row in data_inline_rows[:8]),
            }
        ],
        [
            "table_name",
            "record_file_off_start",
            "record_file_off_end",
            "record_vma_start",
            "record_vma_end",
            "entry_count",
            "record_stride_bytes",
            "field_offset_in_record",
            "slot_bytes_including_nul",
            "max_bytes_before_nul",
            "sample",
        ],
    )
    write_tsv(
        "data_inline_strings.tsv",
        data_inline_rows,
        [
            "table_name",
            "entry_index",
            "record_file_off",
            "record_vma",
            "string_file_off",
            "string_vma",
            "record_stride_bytes",
            "field_offset_in_record",
            "slot_bytes_including_nul",
            "max_bytes_before_nul",
            "byte_len",
            "char_len",
            "slack_bytes_before_nul",
            "text",
        ],
    )
    print(f"kept relocated string pointer tables: {len(table_rows)}")
    print(f"kept relocated string pointers: {len(pointer_rows)}")
    print(f"kept display string pointer tables: {len(display_table_rows)}")
    print(f"kept display string pointers: {len(display_pointer_rows)}")
    print(f"kept .data inline strings: {len(data_inline_rows)}")
    print("wrote relocated_* and data_inline_* TSV files")


if __name__ == "__main__":
    main()
