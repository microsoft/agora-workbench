"""
Generic in-process Okapi BM25 index.

Lightweight, dependency-free BM25 implementation over arbitrary document
objects. Callers supply each document together with the text used for
indexing; the document object is opaque to the index and is returned
verbatim in search results.
"""

from __future__ import annotations

import math
import re
from typing import Generic, TypeVar

D = TypeVar("D")


def tokenize(text: str) -> list[str]:
    """Whitespace + punctuation tokenizer with lowercasing.

    Splits on anything that isn't ``[a-z0-9_]`` after lowercasing.
    """
    return re.findall(r"[a-z0-9_]+", text.lower())


class BM25Index(Generic[D]):
    """In-process Okapi BM25 index over arbitrary documents.

    The index is keyed on opaque document objects of type ``D``; the
    indexable text is supplied separately at ``add()`` time. Supports
    incremental additions; the average-document-length statistic is
    recomputed after each add.

    Args:
        k1: BM25 term-frequency saturation parameter. Typical values
            ``[1.2, 2.0]``. Defaults to ``1.5``.
        b: BM25 length-normalization parameter in ``[0, 1]``. ``0``
            disables length normalization, ``1`` fully normalizes.
            Defaults to ``0.75``.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: list[tuple[D, list[str]]] = []
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0

    def __len__(self) -> int:
        return len(self._docs)

    def add(self, doc: D, text: str) -> None:
        """Add a document plus the text used to index it."""
        tokens = tokenize(text)
        self._docs.append((doc, tokens))

        seen: set[str] = set()
        for token in tokens:
            if token not in seen:
                self._df[token] = self._df.get(token, 0) + 1
                seen.add(token)

        total_tokens = sum(len(toks) for _, toks in self._docs)
        self._avgdl = total_tokens / len(self._docs) if self._docs else 0.0

    def search(self, query: str, top_k: int = 5) -> list[tuple[D, float]]:
        """Score all documents against *query* and return the top ``top_k``.

        Returns:
            ``(document, score)`` pairs sorted by descending score. Empty
            list when the index is empty or the query has no tokens.
            Documents with score ``0`` are still included if they fall
            within ``top_k``.
        """
        if not self._docs:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        n = len(self._docs)
        scores: list[tuple[D, float]] = []

        for doc, doc_tokens in self._docs:
            score = 0.0
            dl = len(doc_tokens)

            tf_map: dict[str, int] = {}
            for token in doc_tokens:
                tf_map[token] = tf_map.get(token, 0) + 1

            for qt in query_tokens:
                if qt not in self._df:
                    continue
                df = self._df[qt]
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue

                idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

                if self._avgdl == 0:
                    tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1)
                else:
                    tf_norm = (tf * (self.k1 + 1)) / (
                        tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                    )

                score += idf * tf_norm

            scores.append((doc, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]
