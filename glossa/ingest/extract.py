"""Single-shot extraction: read one source, return structured entities + summary.

Runs ``extract_agent`` (Pydantic AI structured output) and normalizes the result
into the dataclasses the ingest workflow consumes. This is the planning seed for
the agentic maintainer step.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from glossa.ingest.agents import ExtractionOut, extract_agent
from glossa.ingest.prompts import extract_user_prompt
from glossa.llm import usage_to_dict
from glossa.utils.slug import slugify

if TYPE_CHECKING:
    from pydantic_ai.models import Model


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
    model: "Model",
    model_settings: dict,
    provider: str,
    model_name: str,
    schema_markdown: str,
    source: dict,
    source_content: str,
) -> Extraction:
    user_prompt = extract_user_prompt(
        schema_markdown=schema_markdown,
        source=source,
        source_content=source_content,
    )
    result = await extract_agent.run(user_prompt, model=model, model_settings=model_settings)
    data: ExtractionOut = result.output

    entities: list[ExtractedEntity] = []
    for e in data.entities:
        if not e.title:
            continue
        entity_type = e.type or "topic"
        slug = e.slug or slugify(e.title)
        page_path = (e.page_path or f"entities/{slugify(entity_type)}/{slug}").removesuffix(".md")
        entities.append(
            ExtractedEntity(
                type=entity_type,
                title=e.title,
                slug=slug,
                page_path=page_path,
                relevance=e.relevance or "",
            )
        )

    return Extraction(
        entities=entities,
        source_summary_markdown=(data.source_summary_markdown or "").strip(),
        log_blurb=(data.log_blurb or "ingested source").strip(),
        usage=usage_to_dict(result.usage, provider=provider),
        model=model_name,
    )
