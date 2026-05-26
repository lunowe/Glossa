from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """A space is one bucket-prefix worth of markdown files.

    Paths are relative to the space root, e.g. ``schema.md``, ``index.md``,
    ``log.md``, ``pages/entities/companies/allianz.md``. Implementations are
    responsible for translating these into wherever they actually live.
    """

    @abstractmethod
    async def ensure_bucket(self) -> None: ...

    @abstractmethod
    async def init_space(self, space_id: str, schema_markdown: str | None = None) -> None: ...

    @abstractmethod
    async def read_page(self, space_id: str, path: str) -> str: ...

    @abstractmethod
    async def write_page(self, space_id: str, path: str, content: str) -> None: ...

    @abstractmethod
    async def delete_page(self, space_id: str, path: str) -> None: ...

    @abstractmethod
    async def list_pages(self, space_id: str, prefix: str = "pages/") -> list[str]: ...
