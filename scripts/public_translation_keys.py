"""Stable opaque keys used by the public Korean-only translation datasets."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping


TEXTURE_KEY_DOMAIN = b"giren-v-texture-translation-v1\0"
FONTTILE_KEY_DOMAIN = b"giren-v-fonttile-translation-v1\0"
TEXTURE_KEY_COLUMNS = ("source", "tree_path", "offset", "sha1")


def _hash_parts(domain: bytes, parts: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def texture_translation_key(row: Mapping[str, str]) -> str:
    return _hash_parts(
        TEXTURE_KEY_DOMAIN,
        [str(row.get(column, "")) for column in TEXTURE_KEY_COLUMNS],
    )


def fonttile_translation_key(original: str) -> str:
    return _hash_parts(FONTTILE_KEY_DOMAIN, [original])
