import tempfile
from pathlib import Path
import unittest

from pi_agent_py.storage.memory import InMemorySessionStore
from pi_agent_py.storage.jsonl import JsonlSessionStore
from session_store_contract import SessionStoreContract


class MemoryStoreContractTests(SessionStoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemorySessionStore()

    async def make_store(self):
        return self.store


class JsonlStoreContractTests(SessionStoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JsonlSessionStore(Path(self.directory.name) / "sessions.jsonl")

    async def make_store(self):
        return self.store
