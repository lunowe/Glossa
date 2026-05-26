"""Tenant isolation tests.

For every space-scoped route, prove that:
  - the legitimate owner of the space sees their resource (2xx)
  - a different tenant's bearer token gets 404 (NOT 403 — we don't want to
    leak whether the resource exists)

The setup creates two tenants (Alice and Bob), one API key each, and one
space each. Tests run with ``auth_required=True`` so the dependency must
consult the DB for every request (no system-mode bypass).
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from glossa.config import Settings
from glossa.main import app
from glossa.models.api_key import ApiKey, Scope, hash_key
from glossa.models.job import Job, JobKind, JobStatus
from glossa.models.page import Page, PageKind
from glossa.models.source import Source, SourceIngestionMode, SourceStatus
from glossa.models.space import Space, SpaceStats
from glossa.models.tenant import Tenant, TenantPlan, TenantStatus
from glossa.models.webhook import Webhook, WebhookEvent
from glossa.storage.memory import InMemoryStorageBackend


def _settings(auth_required: bool = True) -> Settings:
    return Settings(auth_required=auth_required)


ALICE_TOKEN = "glsk_live_alice_secret_value"
BOB_TOKEN = "glsk_live_bob_secret_value"
ADMIN_TOKEN = "glsk_live_admin_secret_value"

ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}
BOB_HEADERS = {"Authorization": f"Bearer {BOB_TOKEN}"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def storage() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
def client(storage):
    app.state.settings = _settings(auth_required=True)
    app.state.storage = storage
    return TestClient(app)


async def _seed_tenant(db, tenant_id: str) -> Tenant:
    now = datetime.now(UTC)
    tenant = Tenant(
        id=tenant_id,
        name=tenant_id,
        owner_email=f"{tenant_id}@example.com",
        plan=TenantPlan.FREE,
        status=TenantStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    await db.tenants.insert_one(tenant.model_dump())
    return tenant


async def _seed_api_key(
    db,
    *,
    plaintext: str,
    tenant_id: str,
    key_id: str,
    scopes: list[Scope] | None = None,
) -> ApiKey:
    now = datetime.now(UTC)
    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        hashed_key=hash_key(plaintext),
        prefix=plaintext[: len("glsk_live_") + 8],
        scopes=scopes
        if scopes is not None
        else [Scope.SPACES_READ, Scope.SPACES_WRITE, Scope.SOURCES_WRITE, Scope.QUERY, Scope.LINT],
        created_at=now,
    )
    await db.api_keys.insert_one(api_key.model_dump())
    return api_key


async def _seed_space(db, storage, *, space_id: str, tenant_id: str, slug: str) -> Space:
    now = datetime.now(UTC)
    space = Space(
        id=space_id,
        tenant_id=tenant_id,
        name=f"{tenant_id}-space",
        slug=slug,
        bucket_uri=f"mem://{space_id}/",
        stats=SpaceStats(),
        created_at=now,
        updated_at=now,
    )
    await db.spaces.insert_one(space.model_dump())
    await storage.init_space(space_id)
    return space


@pytest.fixture
async def two_tenant_world(mongomock_db, storage):
    """Seed Alice + Bob with one tenant, one key, one space each.

    Yields a dict with the seeded entities so tests can use them.
    """
    await _seed_tenant(mongomock_db, "tnt_alice")
    await _seed_tenant(mongomock_db, "tnt_bob")
    await _seed_api_key(
        mongomock_db,
        plaintext=ALICE_TOKEN,
        tenant_id="tnt_alice",
        key_id="key_alice",
    )
    await _seed_api_key(
        mongomock_db,
        plaintext=BOB_TOKEN,
        tenant_id="tnt_bob",
        key_id="key_bob",
    )
    alice_space = await _seed_space(
        mongomock_db, storage, space_id="gls_alice", tenant_id="tnt_alice", slug="alice-space"
    )
    bob_space = await _seed_space(mongomock_db, storage, space_id="gls_bob", tenant_id="tnt_bob", slug="bob-space")
    return {
        "alice_space": alice_space,
        "bob_space": bob_space,
    }


@pytest.fixture
async def admin_key(mongomock_db):
    """Add an admin API key (under tnt_alice for convenience)."""
    await _seed_api_key(
        mongomock_db,
        plaintext=ADMIN_TOKEN,
        tenant_id="tnt_alice",
        key_id="key_admin",
        scopes=[Scope.ADMIN],
    )


# --- GET /spaces/{id} -----------------------------------------------------------


async def test_get_space_owner_sees_their_space(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "gls_alice"


async def test_get_space_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- PATCH /spaces/{id} ---------------------------------------------------------


async def test_patch_space_owner_can_update(client, two_tenant_world):
    resp = client.patch(
        "/spaces/gls_alice",
        headers=ALICE_HEADERS,
        json={"name": "Updated Alice Space"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Updated Alice Space"


async def test_patch_space_other_tenant_gets_404(client, two_tenant_world):
    resp = client.patch(
        "/spaces/gls_bob",
        headers=ALICE_HEADERS,
        json={"name": "Hijacked"},
    )
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/schema ----------------------------------------------------


async def test_get_schema_owner_can_read(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/schema", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    assert "path" in resp.json()


async def test_get_schema_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/schema", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- PUT /spaces/{id}/schema ----------------------------------------------------


async def test_put_schema_owner_can_write(client, two_tenant_world):
    resp = client.put(
        "/spaces/gls_alice/schema",
        headers=ALICE_HEADERS,
        params={"schema_markdown": "# New schema"},
    )
    assert resp.status_code == 200, resp.text


async def test_put_schema_other_tenant_gets_404(client, two_tenant_world):
    resp = client.put(
        "/spaces/gls_bob/schema",
        headers=ALICE_HEADERS,
        params={"schema_markdown": "# Hijack"},
    )
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/sources (list) --------------------------------------------


async def test_list_sources_owner_succeeds(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/sources", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_list_sources_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/sources", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/sources/{sid} ---------------------------------------------


async def _seed_source(db, space_id: str, source_id: str) -> Source:
    now = datetime.now(UTC)
    source = Source(
        id=source_id,
        space_id=space_id,
        title=f"src in {space_id}",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="hello",
        status=SourceStatus.RECEIVED,
        created_at=now,
    )
    await db.sources.insert_one(source.model_dump())
    return source


async def test_get_source_owner_succeeds(client, mongomock_db, two_tenant_world):
    await _seed_source(mongomock_db, "gls_alice", "src_alice_1")
    resp = client.get("/spaces/gls_alice/sources/src_alice_1", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "src_alice_1"


async def test_get_source_other_tenant_gets_404(client, mongomock_db, two_tenant_world):
    await _seed_source(mongomock_db, "gls_bob", "src_bob_1")
    resp = client.get("/spaces/gls_bob/sources/src_bob_1", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- POST /spaces/{id}/sources/{sid}/ingest -------------------------------------


async def test_ingest_source_owner_succeeds(client, mongomock_db, two_tenant_world):
    await _seed_source(mongomock_db, "gls_alice", "src_alice_ing")
    resp = client.post(
        "/spaces/gls_alice/sources/src_alice_ing/ingest",
        headers=ALICE_HEADERS,
    )
    # 200 or 402 (quota) are both legitimate; the point is we passed tenant check.
    assert resp.status_code in (200, 402), resp.text


async def test_ingest_source_other_tenant_gets_404(client, mongomock_db, two_tenant_world):
    await _seed_source(mongomock_db, "gls_bob", "src_bob_ing")
    resp = client.post(
        "/spaces/gls_bob/sources/src_bob_ing/ingest",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/pages -----------------------------------------------------


async def test_list_pages_owner_succeeds(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/pages", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_list_pages_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/pages", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/index -----------------------------------------------------


async def test_get_index_owner_succeeds(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/index", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_get_index_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/index", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/log -------------------------------------------------------


async def test_get_log_owner_succeeds(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/log", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_get_log_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/log", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- POST /spaces/{id}/query ----------------------------------------------------


async def test_query_other_tenant_gets_404(client, two_tenant_world):
    resp = client.post(
        "/spaces/gls_bob/query",
        headers=ALICE_HEADERS,
        json={"question": "What is this?"},
    )
    assert resp.status_code == 404, resp.text


async def test_query_owner_passes_tenant_check(client, two_tenant_world, monkeypatch):
    """The legitimate owner gets past the tenant check.

    We don't want to drive a real LLM call here — we just want to confirm
    the tenant gate doesn't reject. Stub ``answer_question`` so the route
    returns a deterministic response.
    """
    from glossa import query as query_mod
    from glossa.routes import query as query_route

    async def _stub(**kwargs):
        return query_mod.QueryResponse(
            answer="stubbed",
            pages_consulted=[],
            cited_pages=[],
            cited_sources=[],
        )

    monkeypatch.setattr(query_route, "answer_question", _stub)

    resp = client.post(
        "/spaces/gls_alice/query",
        headers=ALICE_HEADERS,
        json={"question": "What is this?"},
    )
    assert resp.status_code == 200, resp.text


# --- POST /spaces/{id}/lint -----------------------------------------------------


async def test_lint_owner_succeeds(client, two_tenant_world):
    resp = client.post("/spaces/gls_alice/lint", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_lint_other_tenant_gets_404(client, two_tenant_world):
    resp = client.post("/spaces/gls_bob/lint", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /spaces/{id}/webhooks --------------------------------------------------


async def test_list_webhooks_owner_succeeds(client, two_tenant_world):
    resp = client.get("/spaces/gls_alice/webhooks", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_list_webhooks_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/spaces/gls_bob/webhooks", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /jobs/{job_id} ---------------------------------------------------------


async def _seed_job(db, *, job_id: str, space_id: str) -> Job:
    now = datetime.now(UTC)
    job = Job(
        id=job_id,
        space_id=space_id,
        kind=JobKind.INGEST,
        inputs={},
        status=JobStatus.QUEUED,
        created_at=now,
    )
    await db.jobs.insert_one(job.model_dump())
    return job


async def test_get_job_owner_succeeds(client, mongomock_db, two_tenant_world):
    await _seed_job(mongomock_db, job_id="job_alice", space_id="gls_alice")
    resp = client.get("/jobs/job_alice", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "job_alice"


async def test_get_job_other_tenant_gets_404(client, mongomock_db, two_tenant_world):
    await _seed_job(mongomock_db, job_id="job_bob", space_id="gls_bob")
    resp = client.get("/jobs/job_bob", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- GET /tenants/{tenant_id}/usage ---------------------------------------------


async def test_get_tenant_usage_owner_succeeds(client, two_tenant_world):
    resp = client.get("/tenants/tnt_alice/usage", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_get_tenant_usage_other_tenant_gets_404(client, two_tenant_world):
    resp = client.get("/tenants/tnt_bob/usage", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text


# --- POST /spaces (create) ------------------------------------------------------


async def test_post_spaces_uses_auth_tenant(client, two_tenant_world):
    resp = client.post(
        "/spaces",
        headers=ALICE_HEADERS,
        json={"name": "Alice's New Space"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "tnt_alice"
    assert body["name"] == "Alice's New Space"


async def test_post_spaces_non_admin_cannot_set_other_tenant_id(client, two_tenant_world):
    resp = client.post(
        "/spaces",
        headers=ALICE_HEADERS,
        json={"name": "Hijack", "tenant_id": "tnt_bob"},
    )
    assert resp.status_code == 400, resp.text


async def test_post_spaces_non_admin_setting_own_tenant_id_succeeds(client, two_tenant_world):
    """A non-admin echoing their own tenant_id is fine."""
    resp = client.post(
        "/spaces",
        headers=ALICE_HEADERS,
        json={"name": "Alice OK", "tenant_id": "tnt_alice"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "tnt_alice"


async def test_post_spaces_admin_can_set_other_tenant_id(client, two_tenant_world, admin_key):
    resp = client.post(
        "/spaces",
        headers=ADMIN_HEADERS,
        json={"name": "Admin Created", "tenant_id": "tnt_bob"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "tnt_bob"


# --- GET /spaces (list) ---------------------------------------------------------


async def test_list_spaces_only_returns_own_tenant(client, two_tenant_world):
    resp = client.get("/spaces", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text
    spaces = resp.json()
    assert len(spaces) == 1
    assert spaces[0]["id"] == "gls_alice"
    assert spaces[0]["tenant_id"] == "tnt_alice"


async def test_list_spaces_admin_sees_all(client, two_tenant_world, admin_key):
    resp = client.get("/spaces", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    spaces = resp.json()
    space_ids = {s["id"] for s in spaces}
    assert "gls_alice" in space_ids
    assert "gls_bob" in space_ids


async def test_list_spaces_admin_can_filter(client, two_tenant_world, admin_key):
    resp = client.get("/spaces", headers=ADMIN_HEADERS, params={"tenant_id": "tnt_bob"})
    assert resp.status_code == 200, resp.text
    spaces = resp.json()
    assert len(spaces) == 1
    assert spaces[0]["id"] == "gls_bob"


async def test_list_spaces_non_admin_ignores_tenant_filter_param(client, two_tenant_world):
    """A non-admin passing ``?tenant_id=tnt_bob`` still only sees their own."""
    resp = client.get("/spaces", headers=ALICE_HEADERS, params={"tenant_id": "tnt_bob"})
    assert resp.status_code == 200, resp.text
    spaces = resp.json()
    # Either empty or only Alice's spaces — must not include Bob's.
    for s in spaces:
        assert s["tenant_id"] == "tnt_alice"


# --- Pages /spaces/{id}/pages/{path} --------------------------------------------


async def _seed_page(db, space_id: str, path: str) -> Page:
    now = datetime.now(UTC)
    page = Page(
        space_id=space_id,
        path=path,
        kind=PageKind.ENTITY,
        title="A Page",
        updated_at=now,
    )
    await db.pages.insert_one(page.model_dump())
    return page


async def test_get_page_owner_succeeds(client, mongomock_db, storage, two_tenant_world):
    await _seed_page(mongomock_db, "gls_alice", "entities/companies/acme")
    await storage.write_page("gls_alice", "pages/entities/companies/acme.md", "# Acme")
    resp = client.get(
        "/spaces/gls_alice/pages/entities/companies/acme",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200, resp.text


async def test_get_page_other_tenant_gets_404(client, mongomock_db, storage, two_tenant_world):
    await _seed_page(mongomock_db, "gls_bob", "entities/companies/widget")
    await storage.write_page("gls_bob", "pages/entities/companies/widget.md", "# Widget")
    resp = client.get(
        "/spaces/gls_bob/pages/entities/companies/widget",
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 404, resp.text


# --- Webhooks (delete) ----------------------------------------------------------


async def _seed_webhook(db, *, webhook_id: str, space_id: str) -> Webhook:
    now = datetime.now(UTC)
    webhook = Webhook(
        id=webhook_id,
        space_id=space_id,
        url="https://example.com/hook",
        events=[WebhookEvent.JOB_COMPLETE],
        secret="secret",
        active=True,
        created_at=now,
    )
    await db.webhooks.insert_one(webhook.model_dump())
    return webhook


async def test_delete_webhook_owner_succeeds(client, mongomock_db, two_tenant_world):
    await _seed_webhook(mongomock_db, webhook_id="wh_alice", space_id="gls_alice")
    resp = client.delete("/spaces/gls_alice/webhooks/wh_alice", headers=ALICE_HEADERS)
    assert resp.status_code == 200, resp.text


async def test_delete_webhook_other_tenant_gets_404(client, mongomock_db, two_tenant_world):
    await _seed_webhook(mongomock_db, webhook_id="wh_bob", space_id="gls_bob")
    resp = client.delete("/spaces/gls_bob/webhooks/wh_bob", headers=ALICE_HEADERS)
    assert resp.status_code == 404, resp.text
