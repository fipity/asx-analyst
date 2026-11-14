"""Headline normalisation + 64-bit SimHash for near-duplicate detection."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "as", "at", "by", "with", "is", "are",
         "its", "it", "from", "after", "over", "be", "has", "have", "this", "that", "will", "says", "said"}


def normalise_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9$% ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(t: str) -> list[str]:
    return [w for w in normalise_title(t).split() if w not in _STOP and len(w) > 1]


def simhash(text: str) -> int:
    """64-bit SimHash over word unigrams + bigrams. Returned as a signed 64-bit int (fits SQLite)."""
    words = tokens(text)
    feats = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    if not feats:
        return 0
    v = [0] * 64
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = sum(1 << i for i in range(64) if v[i] > 0)
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def is_duplicate(title_a: str, hash_a: int, title_b: str, hash_b: int, max_bits: int = 3,
                 same_source: bool = False) -> bool:
    """Same story: identical normalised title, >= 60% headline word overlap, or near-identical
    headline+summary SimHash. (SimHash alone is unreliable on short headlines.)
    Within one source only identical titles count: similar titles there are a recurring
    series ("5 things to watch on the ASX 200 on Friday"), not copies."""
    if normalise_title(title_a) == normalise_title(title_b):
        return True
    if same_source:
        return False
    ta, tb = set(tokens(title_a)), set(tokens(title_b))
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
        return True
    return bool(hash_a and hash_b) and hamming(hash_a, hash_b) <= max_bits
