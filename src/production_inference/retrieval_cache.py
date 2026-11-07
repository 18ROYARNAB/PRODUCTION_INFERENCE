import hashlib
import json
from collections.abc import Awaitable, Callable

import redis.asyncio as redis
from langfuse import observe

SEARCH_CONTEXT_CACHE_TTL_SECONDS = 120

@observe
def search_context_cache_key(
    query: str, tenant_id: str, allowed_severities: list[str]
) -> str:
    scope = json.dumps(
        [query, tenant_id, sorted(allowed_severities)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return f"search_context:v1:{digest}"

@observe
async def get_or_load_search_context(
    redis_client: redis.Redis,
    query: str,
    tenant_id: str,
    allowed_severities: list[str],
    loader: Callable[[], Awaitable[list[str]]],
) -> tuple[list[str], bool]:
    cache_key = search_context_cache_key(query, tenant_id, allowed_severities)
    cached = await redis_client.get(cache_key)
    if cached is not None:
        try:
            context = json.loads(cached)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            await redis_client.delete(cache_key)
        else:
            if isinstance(context, list) and all(
                isinstance(item, str) for item in context
            ):
                return context, True
            await redis_client.delete(cache_key)

    context = await loader()
    await redis_client.setex(
        cache_key,
        SEARCH_CONTEXT_CACHE_TTL_SECONDS,
        json.dumps(context),
    )
    return context, False
