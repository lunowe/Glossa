"""Test fixtures for Glossa.

Swaps in mongomock-motor for the global DB client and an in-memory storage
backend, so tests don't need MinIO or Mongo running.
"""

import pytest
from mongomock_motor import AsyncMongoMockClient

from glossa.db import client as db_client
from glossa.storage.memory import InMemoryStorageBackend


@pytest.fixture(autouse=True)
def mongomock_db():
    mock_client = AsyncMongoMockClient()
    db_client._client = mock_client
    db_client._db = mock_client["glossa_test"]
    yield db_client._db
    db_client._client = None
    db_client._db = None


@pytest.fixture
def storage():
    return InMemoryStorageBackend()


@pytest.fixture
def settings(monkeypatch):
    from glossa.config import Settings

    monkeypatch.setenv("GLOSSA_DEFAULT_LLM_ENDPOINT", "http://test/v1")
    monkeypatch.setenv("GLOSSA_DEFAULT_LLM_API_KEY", "test-key")
    return Settings()
