from dataclasses import dataclass

from glossa.ingest.prompts import SYSTEM_INGEST_EXTRACT, extract_user_prompt
from glossa.llm.base import LLMDriver, LLMMessage
from glossa.utils.json_parse import LLMJSONError, parse
from glossa.utils.slug import slugify


@dataclass
class ExtractedEntity:
    type: str
    title: str
    slug: str
    page_path: str
    relevance: str


@dataclass
class Extraction:
    entities: list[ExtractedEntity]
    source_summary_markdown: str
    log_blurb: str
    usage: dict
    model: str


async def extract_from_source(
    *,
    llm: LLMDriver,
    schema_markdown: str,
    source: dict,
    source_content: str,
    model: str,
) -> Extraction:
    user_prompt = extract_user_prompt(
        schema_markdown=schema_markdown,
        source=source,
        source_content=source_content,
    )
    response = await llm.chat(
        [
            LLMMessage(role="system", content=SYSTEM_INGEST_EXTRACT),
            LLMMessage(role="user", content=user_prompt),
        ],
        temperature=0.2,
    )
    data = parse(response.content)
    if not isinstance(data, dict):
        raise LLMJSONError("extract step expected a JSON object")

    entities_raw = data.get("entities") or []
    entities: list[ExtractedEntity] = []
    for e in entities_raw:
        title = e.get("title")
        if not title:
            continue
        entity_type = e.get("type") or "topic"
        slug = e.get("slug") or slugify(title)
        page_path = e.get("page_path") or f"entities/{slugify(entity_type)}/{slug}"
        entities.append(
            ExtractedEntity(
                type=entity_type,
                title=title,
                slug=slug,
                page_path=page_path.removesuffix(".md"),
                relevance=e.get("relevance") or "",
            )
        )

    return Extraction(
        entities=entities,
        source_summary_markdown=str(data.get("source_summary_markdown") or "").strip(),
        log_blurb=str(data.get("log_blurb") or "ingested source").strip(),
        usage=dict(response.usage or {}),
        model=model,
    )
