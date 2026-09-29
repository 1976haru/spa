from __future__ import annotations

import re
from collections import Counter

STOPWORDS = {
    "the", "and", "for", "with", "of", "a", "an", "to", "in", "on", "by", "from",
    "new", "pack", "set", "inch", "inches", "ft", "cm", "mm", "black", "white",
}
ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.I)
NUMBER_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:oz|lb|lbs|in|inch|inches|cm|mm|ft|pack|count)?\b", re.I)
TOKEN_RE = re.compile(r"[a-z][a-z'-]{1,}")


def tokenize(text: str, brands: set[str] | None = None) -> list[str]:
    value = ASIN_RE.sub(" ", str(text or ""))
    value = NUMBER_SIZE_RE.sub(" ", value)
    brand_words = {word.lower() for brand in (brands or set()) for word in TOKEN_RE.findall(brand.lower())}
    return [word for word in TOKEN_RE.findall(value.lower()) if word not in STOPWORDS and word not in brand_words]


def normalize_keyword(value: str) -> str:
    return " ".join(tokenize(value))


def extract_ngrams(texts: list[str], *, brands: set[str] | None = None, max_n: int = 4,
                   min_frequency: int = 1) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for text in texts:
        tokens = tokenize(text, brands)
        seen_in_document = set()
        for size in range(1, max_n + 1):
            for start in range(0, len(tokens) - size + 1):
                phrase = " ".join(tokens[start:start + size])
                if len(phrase) >= 3:
                    seen_in_document.add(phrase)
        counts.update(seen_in_document)
    return sorted(
        ((phrase, count) for phrase, count in counts.items() if count >= min_frequency),
        key=lambda pair: (-pair[1], len(pair[0].split()), pair[0]),
    )
