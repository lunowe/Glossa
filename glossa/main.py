from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from glossa import __version__
from glossa.activity.middleware import ActivityMiddleware
from glossa.auth import get_auth_context
from glossa.config import get_settings
from glossa.dashboard import routes as dashboard_routes
from glossa.db.client import close_db, init_db
from glossa.oauth import register_default_strategies
from glossa.routes import activity, admin, api_keys, auth, jobs, lint, pages, query, sources, spaces, usage, webhooks
from glossa.storage.minio_backend import MinioStorageBackend


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db(settings)
    app.state.settings = settings
    app.state.storage = MinioStorageBackend(settings)
    await app.state.storage.ensure_bucket()
    register_default_strategies(settings)
    yield
    await close_db()


app = FastAPI(
    title="Glossa",
    version=__version__,
    description="LLM-maintained wikis as a service. Markdown is the contract.",
    lifespan=lifespan,
)

app.add_middleware(ActivityMiddleware)

_auth = [Depends(get_auth_context)]

app.include_router(spaces.router, dependencies=_auth)
app.include_router(sources.router, dependencies=_auth)
app.include_router(pages.router, dependencies=_auth)
app.include_router(jobs.router, dependencies=_auth)
app.include_router(webhooks.router, dependencies=_auth)
app.include_router(query.router, dependencies=_auth)
app.include_router(lint.router, dependencies=_auth)
app.include_router(usage.tenant_router, dependencies=_auth)
app.include_router(usage.space_router, dependencies=_auth)
app.include_router(admin.router, dependencies=_auth)
app.include_router(api_keys.router, dependencies=_auth)
app.include_router(activity.router, dependencies=_auth)

app.include_router(auth.router)
app.include_router(dashboard_routes.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
