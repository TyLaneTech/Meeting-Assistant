"""Standalone test: text_embeddings.unload() releases the model and encode()
degrades honestly (returns None) rather than crashing.
Run: .venv/bin/python tests/test_embeddings_unload.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml import text_embeddings


def test_unload_releases_model_and_encode_degrades():
    text_embeddings._model = object()   # stand in for a loaded SentenceTransformer
    assert text_embeddings.is_ready()
    text_embeddings.unload()
    assert text_embeddings._model is None, "_model should be None after unload"
    assert not text_embeddings.is_ready()
    assert text_embeddings.encode("hello") is None, "encode must degrade to None, never raise"
    text_embeddings.unload()  # idempotent


if __name__ == "__main__":
    test_unload_releases_model_and_encode_degrades()
    print("OK test_embeddings_unload")
