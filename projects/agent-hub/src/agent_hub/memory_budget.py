"""Bound complete memory responses. Reference token counts are never provider billing."""

import base64
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

RANK_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"
    r" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)


def serialized(value):
    # The MCP bridge uses the same compact, non-ASCII-escaped JSON representation.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=4)
def tokenizer(filename):
    if not filename:
        return None
    try:
        import tiktoken

        raw = Path(filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != RANK_HASH:
            return None
        ranks = {
            base64.b64decode(token): int(rank)
            for token, rank in (line.split() for line in raw.splitlines() if line)
        }
        return tiktoken.Encoding(
            name="cl100k_base", pat_str=PATTERN, mergeable_ranks=ranks, special_tokens={}
        )
    except (ImportError, OSError, ValueError):
        return None


class Counter:
    def __init__(self, filename=None):
        self.encoding = tokenizer(
            filename if filename is not None else os.getenv("HUB_MEMORY_TOKENIZER_FILE", "")
        )
        self.method = "cl100k_base" if self.encoding else "utf8-bytes-conservative-fallback"

    def count(self, value):
        text = serialized(value)
        return len(self.encoding.encode_ordinary(text)) if self.encoding else len(text.encode())

    def measure(self, value, maximum):
        value["budget"] = {
            "limit": maximum,
            "used": 0,
            "serialized_bytes": 0,
            "method": self.method,
            "scope": "complete-compact-json-response",
            "provider_usage": "unknown",
        }
        # Include the measurement itself, including changing number widths.
        for _ in range(12):
            before = dict(value["budget"])
            value["budget"]["used"] = self.count(value)
            value["budget"]["serialized_bytes"] = len(serialized(value).encode())
            if value["budget"] == before:
                return value["budget"]["used"] <= maximum
        return False
