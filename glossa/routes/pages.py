from fastapi import APIRouter, HTTPException, Request

from glossa.db.client import get_db
from glossa.models.page import Page, PageWithContent

router = APIRouter(prefix="/spaces/{space_id}", tags=["pages"])


@router.get("/pages", response_model=list[Page])
async def list_pages(
    space_id: str,
    kind: str | None = None,
    path_prefix: str | None = None,
    limit: int = 100,
) -> list[Page]:
    db = get_db()
    query: dict = {"space_id": space_id}
    if kind:
        query["kind"] = kind
    if path_prefix:
        query["path"] = {"$regex": f"^{path_prefix}"}
    cursor = db.pages.find(query).limit(limit)
    return [Page.model_validate(doc) async for doc in cursor]


@router.get("/pages/{path:path}", response_model=PageWithContent)
async def get_page(space_id: str, path: str, request: Request) -> PageWithContent:
    db = get_db()
    doc = await db.pages.find_one({"space_id": space_id, "path": path})
    if not doc:
        raise HTTPException(status_code=404, detail="page not found")
    storage_path = path if path.endswith(".md") else f"pages/{path}.md"
    content = await request.app.state.storage.read_page(space_id, storage_path)
    return PageWithContent(**Page.model_validate(doc).model_dump(), content=content)


@router.get("/index")
async def get_index(space_id: str, request: Request) -> dict:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    content = await request.app.state.storage.read_page(space_id, "index.md")
    return {"path": "index.md", "content": content}


@router.get("/log")
async def get_log(space_id: str, request: Request, tail: int | None = None) -> dict:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    content = await request.app.state.storage.read_page(space_id, "log.md")
    if tail and content:
        lines = content.splitlines()
        entry_indices = [i for i, line in enumerate(lines) if line.startswith("## [")]
        if len(entry_indices) > tail:
            content = "\n".join(lines[entry_indices[-tail] :])
    return {"path": "log.md", "content": content}


@router.get("/lint-report")
async def get_lint_report(space_id: str, request: Request) -> dict:
    db = get_db()
    if not await db.spaces.find_one({"id": space_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="space not found")
    content = await request.app.state.storage.read_page(space_id, "lint_report.md")
    if not content:
        raise HTTPException(status_code=404, detail="lint report not found")
    return {"path": "lint_report.md", "content": content}
