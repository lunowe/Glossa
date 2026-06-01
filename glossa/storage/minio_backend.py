import asyncio
import io
from typing import TYPE_CHECKING

from minio import Minio
from minio.error import S3Error

from glossa.storage.base import StorageBackend

if TYPE_CHECKING:
    from glossa.config import Settings


DEFAULT_SCHEMA = """# Schema

Default Glossa space schema. Edit this file to teach the LLM how to
maintain *this* wiki: entity types, page naming, ingest workflow, tone.
"""

DEFAULT_INDEX = "# Index\n\nNo pages yet.\n"
DEFAULT_LOG = "# Log\n\n"


class MinioStorageBackend(StorageBackend):
    def __init__(self, settings: "Settings"):
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self._bucket = settings.minio_bucket

    def _key(self, space_id: str, path: str) -> str:
        return f"{space_id}/{path.lstrip('/')}"

    async def ensure_bucket(self) -> None:
        await asyncio.to_thread(self._ensure_bucket_sync)

    def _ensure_bucket_sync(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    async def init_space(self, space_id: str, schema_markdown: str | None = None) -> None:
        await self.write_page(space_id, "schema.md", schema_markdown or DEFAULT_SCHEMA)
        await self.write_page(space_id, "index.md", DEFAULT_INDEX)
        await self.write_page(space_id, "log.md", DEFAULT_LOG)

    async def read_page(self, space_id: str, path: str) -> str:
        return await asyncio.to_thread(self._read_sync, space_id, path)

    def _read_sync(self, space_id: str, path: str) -> str:
        try:
            response = self._client.get_object(self._bucket, self._key(space_id, path))
            try:
                return response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
        except S3Error as e:
            if e.code == "NoSuchKey":
                return ""
            raise

    async def write_page(self, space_id: str, path: str, content: str) -> None:
        await asyncio.to_thread(self._write_sync, space_id, path, content)

    def _write_sync(self, space_id: str, path: str, content: str) -> None:
        data = content.encode("utf-8")
        self._client.put_object(
            self._bucket,
            self._key(space_id, path),
            io.BytesIO(data),
            length=len(data),
            content_type="text/markdown",
        )

    async def delete_page(self, space_id: str, path: str) -> None:
        await asyncio.to_thread(self._delete_sync, space_id, path)

    def _delete_sync(self, space_id: str, path: str) -> None:
        self._client.remove_object(self._bucket, self._key(space_id, path))

    async def list_pages(self, space_id: str, prefix: str = "pages/") -> list[str]:
        return await asyncio.to_thread(self._list_sync, space_id, prefix)

    def _list_sync(self, space_id: str, prefix: str) -> list[str]:
        key_prefix = self._key(space_id, prefix)
        results: list[str] = []
        for obj in self._client.list_objects(self._bucket, prefix=key_prefix, recursive=True):
            if obj.object_name:
                results.append(obj.object_name[len(f"{space_id}/") :])
        return results

    async def write_asset(self, space_id: str, path: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._write_asset_sync, space_id, path, data, content_type)

    def _write_asset_sync(self, space_id: str, path: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            self._bucket,
            self._key(space_id, path),
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    async def read_asset(self, space_id: str, path: str) -> bytes:
        return await asyncio.to_thread(self._read_asset_sync, space_id, path)

    def _read_asset_sync(self, space_id: str, path: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, self._key(space_id, path))
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as e:
            if e.code == "NoSuchKey":
                raise FileNotFoundError(f"asset not found: {self._key(space_id, path)}") from e
            raise
