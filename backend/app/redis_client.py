# backend/app/redis_client.py — Sistema Dono
import os

import redis.asyncio as redis

_client: redis.Redis | None = None


async def connect() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    return _client


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Cliente Redis ainda não inicializado — chame connect() no startup")
    return _client
