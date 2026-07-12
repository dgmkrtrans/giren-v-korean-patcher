import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import fonttile_text_tool as fonttile  # noqa: E402


class FonttileExternalPoolTest(unittest.TestCase):
    def decrypted_eboot(self) -> Path:
        eboot = PROJECT_ROOT / "results" / "ULJS00178_EBOOT.BIN"
        if not eboot.is_file():
            self.skipTest("user-supplied decrypted EBOOT is not available")
        return eboot

    def test_fixed_slot_rows_share_full_payload_and_use_three_byte_markers(self) -> None:
        eboot = self.decrypted_eboot()
        data = bytearray(eboot.read_bytes())
        allocator = fonttile.RelocatedExternalPoolAllocator(data)
        encoded = fonttile.encode_font_text("코무사이Ⅱ[무사이･후기형타입]")
        rows = [
            fonttile.ApplyRow(
                row_index=index,
                source_path=Path("dummy.dat"),
                offset=index * 4,
                span=4,
                max_bytes=3,
                original_hex="a1a2a3",
                translation="코무사이Ⅱ[무사이･후기형타입]",
                encoded=encoded,
                region="whole",
            )
            for index in (2, 3)
        ]

        resolved, stats = fonttile.install_indirect_string_pool(
            eboot, data, allocator, rows
        )

        self.assertEqual(stats.rows, 2)
        self.assertEqual(stats.unique_payloads, 1)
        self.assertEqual(stats.payload_bytes, len(encoded) + 1)
        self.assertEqual(stats.stub_bytes, 192)
        self.assertEqual(resolved[0].encoded, resolved[1].encoded)
        self.assertEqual(len(resolved[0].encoded), fonttile.INDIRECT_STRING_MARKER_SIZE)
        self.assertEqual(resolved[0].encoded[0], fonttile.INDIRECT_STRING_MARKER)
        relative = resolved[0].encoded[1] | resolved[0].encoded[2] << 8
        payload_offset = fonttile.INDIRECT_STRING_POOL_BASE_OFFSET + relative
        self.assertEqual(
            data[payload_offset : payload_offset + len(encoded) + 1], encoded + b"\0"
        )

    def test_renderer_entries_jump_to_mapped_external_pool_stubs(self) -> None:
        eboot = self.decrypted_eboot()
        data = bytearray(eboot.read_bytes())
        allocator = fonttile.RelocatedExternalPoolAllocator(data)
        row = fonttile.ApplyRow(
            row_index=2,
            source_path=Path("dummy.dat"),
            offset=0,
            span=4,
            max_bytes=3,
            original_hex="a1a2a3",
            translation="무사이",
            encoded=fonttile.encode_font_text("무사이"),
            region="whole",
        )
        fonttile.install_indirect_string_pool(eboot, data, allocator, [row])

        for _kind, entry_offset, expected_words in fonttile.INDIRECT_STRING_ENTRY_PATCHES:
            self.assertNotEqual(
                int.from_bytes(data[entry_offset : entry_offset + 4], "little"),
                expected_words[0],
            )
            self.assertEqual(data[entry_offset + 4 : entry_offset + 8], b"\0" * 4)


if __name__ == "__main__":
    unittest.main()
