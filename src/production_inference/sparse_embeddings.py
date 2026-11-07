import asyncio
from functools import lru_cache

from fastembed import SparseTextEmbedding
from qdrant_client import models


@lru_cache(maxsize=1)
def get_bm25_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(model_name="Qdrant/bm25")


def _embed_query(text: str) -> models.SparseVector:
    embedding = next(iter(get_bm25_model().query_embed(text)), None)
    if embedding is None:
        raise ValueError("BM25 returned no query embedding")

    indices = embedding.indices.tolist()
    values = embedding.values.tolist()
    if len(indices) != len(values):
        raise ValueError("BM25 returned an invalid sparse query embedding")

    return models.SparseVector(indices=indices, values=values)


async def generate_sparse_embedding(text: str) -> models.SparseVector:
    return await asyncio.to_thread(_embed_query, text)
