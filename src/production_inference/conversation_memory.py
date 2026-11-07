import hashlib
import json

import redis.asyncio as redis

CONVERSATION_TTL_SECONDS = 30 * 60
MAX_CONVERSATION_TURNS = 10
MAX_CONVERSATION_MESSAGES = MAX_CONVERSATION_TURNS * 2


def conversation_history_key(
    tenant_id: str,
    subject: str,
    allowed_severities: list[str],
    conversation_id: str,
) -> str:
    scope = json.dumps(
        [tenant_id, subject, sorted(allowed_severities), conversation_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return f"chat_history:v1:{digest}"


async def load_conversation_history(
    redis_client: redis.Redis,
    tenant_id: str,
    subject: str,
    allowed_severities: list[str],
    conversation_id: str,
) -> list[dict[str, str]]:
    key = conversation_history_key(
        tenant_id, subject, allowed_severities, conversation_id
    )
    entries = await redis_client.lrange(key, 0, -1)
    history: list[dict[str, str]] = []
    for entry in entries:
        try:
            message = json.loads(entry)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
        ):
            history.append(
                {"role": message["role"], "content": message["content"]}
            )
    return history[-MAX_CONVERSATION_MESSAGES:]


async def append_conversation_turn(
    redis_client: redis.Redis,
    tenant_id: str,
    subject: str,
    allowed_severities: list[str],
    conversation_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    key = conversation_history_key(
        tenant_id, subject, allowed_severities, conversation_id
    )
    messages = [
        json.dumps({"role": "user", "content": user_message}),
        json.dumps({"role": "assistant", "content": assistant_message}),
    ]
    async with redis_client.pipeline(transaction=True) as pipeline:
        pipeline.rpush(key, *messages)
        pipeline.ltrim(key, -MAX_CONVERSATION_MESSAGES, -1)
        pipeline.expire(key, CONVERSATION_TTL_SECONDS)
        await pipeline.execute()
