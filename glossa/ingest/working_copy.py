"""In-memory working copy of wiki pages for one ingest (maintainer) run.

The maintainer agent's edit tools mutate this copy; nothing touches storage until
the deterministic flush after the agent finishes. Pages are held as full markdown
(frontmatter + body); section/substring edits split via
:mod:`glossa.utils.frontmatter` and :mod:`glossa.utils.md_sections`.

Holding edits in memory lets multiple edits to one page coalesce into a single
write, lets validation run on the final state, and keeps quota enforcement and
``source_refs``/``updated_at`` bookkeeping in deterministic code (the flush),
never in the model.
"""

from typing import TYPE_CHECKING

from glossa.ingest import page_writer

if TYPE_CHECKING:
    from glossa.storage.base import StorageBackend


class WorkingCopy:
    """A lazily-loaded, mutable view of a space's pages for one ingest run."""

    def __init__(self, storage: "StorageBackend", space_id: str) -> None:
        self._storage = storage
        self._space_id = space_id
        self._current: dict[str, str] = {}  # path -> full markdown ("" == absent)
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()
        self._created: set[str] = set()
        self._meta: dict[str, dict] = {}  # path -> {"kind": ..., "title": ...}
        self.edited_bytes: int = 0

    async def load(self, path: str) -> str:
        """Return the current working content for ``path`` (loading from storage once)."""
        if path not in self._loaded:
            existing = await page_writer.read_existing_page(self._storage, self._space_id, path)
            self._current[path] = existing or ""
            self._loaded.add(path)
        return self._current[path]

    def exists(self, path: str) -> bool:
        """True if the (already-loaded) page has content."""
        return bool(self._current.get(path))

    def put(
        self,
        path: str,
        content: str,
        *,
        created: bool = False,
        kind: str | None = None,
        title: str | None = None,
    ) -> None:
        """Record an edited/created page in the working copy and mark it dirty."""
        self._current[path] = content
        self._loaded.add(path)
        self._dirty.add(path)
        if created:
            self._created.add(path)
        if kind or title:
            meta = self._meta.setdefault(path, {})
            if kind:
                meta["kind"] = kind
            if title:
                meta["title"] = title
        self.edited_bytes += len(content.encode("utf-8"))

    def content(self, path: str) -> str:
        """The current working content for an already-loaded path."""
        return self._current.get(path, "")

    def meta(self, path: str) -> dict:
        return self._meta.get(path, {})

    def is_created(self, path: str) -> bool:
        return path in self._created

    @property
    def dirty(self) -> set[str]:
        """Paths created or edited during this run."""
        return self._dirty
