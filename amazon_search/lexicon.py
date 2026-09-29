"""Shopping-domain lexicon: synonyms, abbreviations, broader terms and a spelling vocabulary."""

from __future__ import annotations

import difflib
import json
from importlib import resources
from pathlib import Path
from typing import Iterable, Optional, Union

from .text import STOPWORDS, stem, tokenize

Phrase = tuple[str, ...]  # a phrase as a tuple of stemmed tokens


def _key(text: str) -> Phrase:
    return tuple(stem(t) for t in tokenize(text))


class Lexicon:
    def __init__(
        self,
        synonyms: list[list[str]],
        abbreviations: dict[str, str],
        broader: dict[str, str],
        vocabulary: Iterable[str] = (),
    ) -> None:
        self._synonyms: dict[Phrase, list[str]] = {}
        for group in synonyms:
            for phrase in group:
                alts = self._synonyms.setdefault(_key(phrase), [])
                alts.extend(p for p in group if _key(p) != _key(phrase) and p not in alts)
        # Abbreviations only expand (short -> long); contracting "bluetooth" to "bt" makes worse queries.
        self._abbrev = {_key(short): long for short, long in abbreviations.items() if _key(short) != _key(long)}
        self._broader = {_key(k): v for k, v in broader.items()}
        self._narrower: dict[Phrase, list[str]] = {}
        for specific, general in broader.items():
            self._narrower.setdefault(_key(general), []).append(specific)

        self.vocabulary: set[str] = set()
        words: list[str] = list(vocabulary)
        for group in synonyms:
            words.extend(group)
        words.extend(abbreviations)
        words.extend(abbreviations.values())
        words.extend(broader)
        words.extend(broader.values())
        self.learn(words)
        self._learned: set[str] = set()

    # ------------------------------------------------------------ construction

    @classmethod
    def load(cls, path: Optional[Union[str, Path]] = None) -> "Lexicon":
        if path is None:
            raw = resources.files("amazon_search.data").joinpath("lexicon.json").read_text()
        else:
            raw = Path(path).read_text()
        data = json.loads(raw)
        return cls(
            synonyms=data.get("synonyms", []),
            abbreviations=data.get("abbreviations", {}),
            broader=data.get("broader", {}),
            vocabulary=data.get("vocabulary", []),
        )

    def learn(self, texts: Iterable[str], remember: bool = False) -> None:
        """Add words (e.g. from result titles) to the spelling vocabulary."""
        for text in texts:
            for tok in tokenize(text):
                if tok.isalpha() and len(tok) > 2 and tok not in self.vocabulary:
                    self.vocabulary.add(tok)
                    if remember:
                        self._learned.add(tok)

    def load_learned(self, path: Union[str, Path]) -> None:
        p = Path(path)
        if p.is_file():
            try:
                self.learn(json.loads(p.read_text()), remember=True)
            except (ValueError, OSError):
                pass  # a corrupt cache is not worth failing a search over

    def save_learned(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(self._learned)))

    # ------------------------------------------------------------ lookups

    def synonyms(self, phrase: Union[str, Phrase]) -> list[str]:
        key = phrase if isinstance(phrase, tuple) else _key(phrase)
        return list(self._synonyms.get(key, []))

    def abbreviation(self, phrase: Union[str, Phrase]) -> Optional[str]:
        key = phrase if isinstance(phrase, tuple) else _key(phrase)
        return self._abbrev.get(key)

    def broader(self, phrase: Union[str, Phrase]) -> Optional[str]:
        key = phrase if isinstance(phrase, tuple) else _key(phrase)
        return self._broader.get(key)

    def narrower(self, phrase: Union[str, Phrase]) -> list[str]:
        """More specific kinds of ``phrase`` ("headphones" -> "earbuds", "gaming headset", ...)."""
        key = phrase if isinstance(phrase, tuple) else _key(phrase)
        return list(self._narrower.get(key, []))

    def related_stems(self, token: str) -> set[str]:
        """Single-word stems that mean roughly the same as ``token`` (used for scoring)."""
        out: set[str] = set()
        key = (stem(token),)
        alts = self._synonyms.get(key, []) + self._narrower.get(key, [])
        if key in self._abbrev:
            alts.append(self._abbrev[key])
        for alt in alts:
            alt_key = _key(alt)
            if len(alt_key) == 1:
                out.add(alt_key[0])
        return out

    def known(self, word: str) -> bool:
        return word in self.vocabulary or stem(word) in self.vocabulary or word in STOPWORDS

    def correct(self, word: str) -> Optional[str]:
        """Return a likely intended spelling for ``word``, or None if it looks fine / unknown."""
        if not word.isalpha() or len(word) < 4 or self.known(word):
            return None
        for match in difflib.get_close_matches(word, self.vocabulary, n=3, cutoff=0.8):
            # "iphone" is not a typo of "phone", nor "ereader" of "reader": a known word with extra
            # letters glued on the front is usually a brand or compound, so leave it alone.
            if match in word and not word.startswith(match):
                continue
            return match
        return None
