from datetime import UTC, datetime

from glossa.models import (
    Job,
    JobKind,
    JobStatus,
    Page,
    PageKind,
    Source,
    SourceIngestionMode,
    Space,
    Webhook,
    WebhookEvent,
)
from glossa.models.source import SourceCreate
from glossa.models.space import SpaceCreate


def test_models_round_trip() -> None:
    now = datetime.now(UTC)

    space = Space(
        id="gls_abc",
        tenant_id="t1",
        name="Test Space",
        slug="test-space",
        bucket_uri="s3://glossa-spaces/gls_abc/",
        created_at=now,
        updated_at=now,
    )
    assert Space.model_validate(space.model_dump()) == space

    source = Source(
        id="src_xyz",
        space_id=space.id,
        title="A source",
        ingestion_mode=SourceIngestionMode.PUSH,
        content_inline="hello",
        created_at=now,
    )
    assert Source.model_validate(source.model_dump()) == source

    page = Page(
        space_id=space.id,
        path="entities/companies/allianz",
        kind=PageKind.ENTITY,
        title="Allianz",
        updated_at=now,
    )
    assert Page.model_validate(page.model_dump()) == page

    job = Job(
        id="job_1",
        space_id=space.id,
        kind=JobKind.INGEST,
        status=JobStatus.QUEUED,
        created_at=now,
    )
    assert Job.model_validate(job.model_dump()) == job

    webhook = Webhook(
        id="wh_1",
        space_id=space.id,
        url="https://example.com/hook",
        events=[WebhookEvent.JOB_COMPLETE],
        secret="s",
        created_at=now,
    )
    assert Webhook.model_validate(webhook.model_dump()) == webhook


def test_source_create_requires_content_for_push() -> None:
    import pytest

    with pytest.raises(ValueError):
        SourceCreate(title="x", ingestion_mode=SourceIngestionMode.PUSH)

    with pytest.raises(ValueError):
        SourceCreate(title="x", ingestion_mode=SourceIngestionMode.PULL)

    SourceCreate(title="x", ingestion_mode=SourceIngestionMode.PUSH, content_inline="hello")


def test_app_imports_cleanly() -> None:
    from glossa.main import app

    routes = [r.path for r in app.routes]
    assert "/healthz" in routes
    assert any(p.startswith("/spaces") for p in routes)
    assert any("/sources" in p for p in routes)
    assert any("/pages" in p for p in routes)
    assert "/jobs/{job_id}" in routes
    assert any("/webhooks" in p for p in routes)


def test_space_create_payload() -> None:
    payload = SpaceCreate(tenant_id="t1", name="My Wiki")
    assert payload.tenant_id == "t1"
    assert payload.name == "My Wiki"
