"""
Central rate-limiter instance.

Uses in-memory storage by default (fine for a single-process deployment).
To switch to Redis, set REDIS_URL in the environment and uncomment the
storage line below — no other code changes required.

    from limits.storage import RedisStorage
    storage = RedisStorage("redis://localhost:6379")
    limiter = Limiter(key_func=get_remote_address, storage_uri=storage)
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
