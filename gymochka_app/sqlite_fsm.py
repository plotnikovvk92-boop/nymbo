"""Shared FSM + renewable per-user event leases for Passenger workers."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, BaseEventIsolation, StorageKey
from config import DB_PATH


def _key(key: StorageKey):
    return json.dumps([key.bot_id, key.chat_id, key.user_id, key.thread_id,
                       key.business_connection_id, key.destiny], separators=(',', ':'))


@asynccontextmanager
async def connection():
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute('PRAGMA busy_timeout=10000')
        await db.execute('CREATE TABLE IF NOT EXISTS fsm (key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT "{}")')
        await db.execute('CREATE TABLE IF NOT EXISTS fsm_locks (key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)')
        await db.commit()
        yield db


class SQLiteStorage(BaseStorage):
    async def set_state(self, key, state=None):
        value = state.state if hasattr(state, 'state') else state
        async with connection() as db:
            await db.execute('INSERT INTO fsm(key,state) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state', (_key(key), value))
            await db.commit()

    async def get_state(self, key):
        async with connection() as db:
            async with db.execute('SELECT state FROM fsm WHERE key=?', (_key(key),)) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def set_data(self, key, data):
        async with connection() as db:
            await db.execute('INSERT INTO fsm(key,data) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data', (_key(key), json.dumps(data)))
            await db.execute('DELETE FROM fsm WHERE key=? AND state IS NULL AND data="{}"', (_key(key),))
            await db.commit()

    async def get_data(self, key):
        async with connection() as db:
            async with db.execute('SELECT data FROM fsm WHERE key=?', (_key(key),)) as cur:
                row = await cur.fetchone()
                return json.loads(row[0]) if row else {}

    async def close(self):
        pass


class SQLiteEventIsolation(BaseEventIsolation):
    @asynccontextmanager
    async def lock(self, key):
        owner, name = str(uuid.uuid4()), _key(key)
        deadline = time.monotonic() + 65
        while True:
            async with connection() as db:
                await db.execute('INSERT INTO fsm_locks(key,owner,expires) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE fsm_locks.expires < ?', (name, owner, time.time()+90, time.time()))
                changed = db.total_changes > 0
                await db.commit()
            if changed: break
            if time.monotonic() > deadline: raise TimeoutError('FSM event lock timeout')
            await asyncio.sleep(0.05)

        async def renew():
            while True:
                await asyncio.sleep(20)
                async with connection() as db:
                    await db.execute('UPDATE fsm_locks SET expires=? WHERE key=? AND owner=?', (time.time()+90, name, owner))
                    await db.commit()
        heartbeat = asyncio.create_task(renew())
        try:
            yield
        finally:
            heartbeat.cancel()
            try: await heartbeat
            except asyncio.CancelledError: pass
            async with connection() as db:
                await db.execute('DELETE FROM fsm_locks WHERE key=? AND owner=?', (name, owner))
                await db.commit()

    async def close(self):
        pass
