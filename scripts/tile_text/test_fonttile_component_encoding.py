import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import fonttile_text_tool as fonttile  # noqa: E402


class FonttileComponentEncodingTest(unittest.TestCase):
    def assert_encoded_hex(self, text: str, expected_hex: str) -> None:
        self.assertEqual(fonttile.encode_font_text(text).hex().upper(), expected_hex)

    def test_demark_source_order_matches_game_name_index_order(self) -> None:
        demark_mappings = [
            mapping
            for mapping in fonttile.KOREAN_ALL_GLYPH_MAPPINGS
            if mapping.mark_type == "demark"
        ]
        sort_keys = [
            fonttile.korean_index_byte_sort_key(mapping.byte)
            for mapping in demark_mappings
        ]
        self.assertEqual(sort_keys, sorted(sort_keys))

        indexed_initials = "가나다라마바사아자차카타파하"
        initial_keys = [
            fonttile.korean_index_byte_sort_key(fonttile.encode_font_text(char)[0])
            for char in indexed_initials
        ]
        self.assertEqual(initial_keys, sorted(initial_keys))

    def test_side_vowel_with_final_uses_side_final_initial_row(self) -> None:
        self.assert_encoded_hex("겔구구", "81EAF782DE82DE")
        self.assert_encoded_hex("렌", "93EAF6")
        self.assert_encoded_hex("켄", "BFEAF6")
        self.assert_encoded_hex("맨", "99E7F6")

    def test_side_vowel_with_final_supports_every_initial_in_source_row(self) -> None:
        expected = {
            "갠": "81E7F6",
            "낸": "87E7F6",
            "댄": "8DE7F6",
            "랜": "93E7F6",
            "맨": "99E7F6",
            "밴": "9FE7F6",
            "샌": "A6E7F6",
            "앤": "ACE7F6",
            "잰": "B3E7F6",
            "챈": "B9E7F6",
            "캔": "BFE7F6",
            "탠": "C5E7F6",
            "팬": "CAE7F6",
            "핸": "CFE7F6",
        }
        for syllable, encoded_hex in expected.items():
            with self.subTest(syllable=syllable):
                self.assert_encoded_hex(syllable, encoded_hex)

    def test_initial_rows_are_classified_from_source_glyphs(self) -> None:
        self.assertEqual(
            [(row.layout, row.has_final) for row in fonttile.KOREAN_INITIAL_COMPONENT_ROWS],
            [
                ("side", False),
                ("side", True),
                ("bottom", False),
                ("bottom", True),
                ("complex", False),
                ("complex", True),
            ],
        )

    def test_initial_selection_does_not_depend_on_source_row_order(self) -> None:
        reordered_rows = tuple(reversed(fonttile.KOREAN_INITIAL_COMPONENT_ROWS))
        with mock.patch.object(fonttile, "KOREAN_INITIAL_COMPONENT_ROWS", reordered_rows):
            self.assert_encoded_hex("겔", "81EAF7")
            self.assert_encoded_hex("켄", "BFEAF6")
            self.assert_encoded_hex("맨", "99E7F6")
            self.assert_encoded_hex("곽", "85EDF5")

    def test_other_initial_layouts_still_use_matching_rows(self) -> None:
        self.assert_encoded_hex("구", "82DE")
        self.assert_encoded_hex("곽", "85EDF5")


if __name__ == "__main__":
    unittest.main()
