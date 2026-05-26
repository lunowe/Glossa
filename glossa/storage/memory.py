"""In-memory StorageBackend. Useful for tests, ephemeral demos, and CI."""

from glossa.storage.base import StorageBackend

DEFAULT_SCHEMA = "# Schema\n\nDefault Glossa space schema.\n"
DEFAULT_INDEX = "# Index\n\nNo pages yet.\n"
DEFAULT_LOG = "# Log\n\n"


class InMemoryStorageBackend(StorageBackend):
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def _key(self, space_id: str, path: str) -> str:
        return f"{space_id}/{path.lstrip('/')}"

    async def ensure_bucket(self) -> None:
        return

    async def init_space(self, space_id: str, schema_markdown: str | None = None) -> None:
        await self.write_page(space_id, "schema.md", schema_markdown or DEFAULT_SCHEMA)
        await self.write_page(space_id, "index.md", DEFAULT_INDEX)
        await self.write_page(space_id, "log.md", DEFAULT_LOG)

    async def read_page(self, space_id: str, path: str) -> str:
        return self.files.get(self._key(space_id, path), "")

    async def write_page(self, space_id: str, path: str, content: str) -> None:
        self.files[self._key(space_id, path)] = content

    async def delete_page(self, space_id: str, path: str) -> None:
        self.files.pop(self._key(space_id, path), None)

    async def list_pages(self, space_id: str, prefix: str = "pages/") -> list[str]:
        full_prefix = self._key(space_id, prefix)
        space_prefix = f"{space_id}/"
        return [key[len(space_prefix) :] for key in self.files if key.startswith(full_prefix)]
