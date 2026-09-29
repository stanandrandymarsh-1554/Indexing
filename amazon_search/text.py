"""Small text utilities: tokenizing, light stemming and stopwords."""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    "a an and the for with of to in on by at from or is are this that best top good new "
    "cheap cheapest buy sale deal deals my your our".split()
)

_TOKEN_RE = re.compile(r"[a-z]+|\d+(?:\.\d+)?")


def tokenize(text: str) -> list[str]:
    """Lowercase and split into alphabetic and numeric runs ("usb-c 16GB" -> usb, c, 16, gb)."""
    return _TOKEN_RE.findall(text.lower().replace("&", " and ").replace("'", ""))


def stem(word: str) -> str:
    """Very light English plural stemmer, enough to make "batteries" match "battery"."""
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def pluralize(word: str) -> str:
    if not word.isalpha() or len(word) <= 2:
        return word
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("ch", "sh", "x", "ss", "z")):
        return word + "es"
    if word.endswith("s"):
        return word
    return word + "s"


def content_words(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in STOPWORDS]
