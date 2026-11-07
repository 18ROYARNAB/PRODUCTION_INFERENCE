import httpx
from langfuse import observe

from production_inference.config import TEI_URL


@observe
async def generate_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(f"{TEI_URL}/embed", json={"inputs": [text]})
        response.raise_for_status()
        embeddings = response.json()

    if not embeddings or not isinstance(embeddings[0], list):
        raise ValueError("TEI returned an invalid embedding response")
    return embeddings[0]