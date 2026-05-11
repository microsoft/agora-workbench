"""Compatibility re-export for the shared BM25 implementation.

Use :mod:`utilities.bm25` for new imports.
"""

from utilities.bm25 import BM25Index, tokenize

__all__ = ["BM25Index", "tokenize"]
