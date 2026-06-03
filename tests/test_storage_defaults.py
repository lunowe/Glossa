from glossa.storage.defaults import DEFAULT_SCHEMA
from glossa.storage.memory import InMemoryStorageBackend


def test_default_schema_is_operational_contract():
    assert "## Layers" in DEFAULT_SCHEMA
    assert "## Page Types" in DEFAULT_SCHEMA
    assert "`notes/<slug>`" in DEFAULT_SCHEMA
    assert "[[summaries/src-...]]" in DEFAULT_SCHEMA


async def test_memory_storage_uses_strong_default_schema():
    storage = InMemoryStorageBackend()
    await storage.init_space("gls_schema")

    schema = await storage.read_page("gls_schema", "schema.md")

    assert schema == DEFAULT_SCHEMA
    assert "Read `index.md` first" in schema
