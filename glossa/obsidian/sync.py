"""Mirror a Glossa Space into an Obsidian vault.

The mirror is intentionally one-way: Glossa remains the writer/maintainer of
the wiki, while Obsidian is a local markdown browser. Files are written using
Glossa's logical paths, so ``[[entities/...]]`` wikilinks keep working.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from glossa.mcp.client import GlossaClient, GlossaClientError

_WIKILINK_RE = re.compile(r"\[\[([^\]#|]+)(#[^\]|]+)?(\|[^\]]+)?\]\]")


@dataclass(frozen=True)
class SyncResult:
    space_id: str
    vault_path: Path
    files_written: int
    pages_written: int


def rewrite_wikilinks(markdown: str, *, link_prefix: str) -> str:
    """Prefix Glossa logical wikilinks when syncing into a vault subfolder."""
    clean_prefix = link_prefix.strip("/")
    if not clean_prefix:
        return markdown

    def repl(match: re.Match[str]) -> str:
        target, heading, alias = match.groups()
        if _is_external_or_prefixed(target, clean_prefix):
            return match.group(0)
        return f"[[{clean_prefix}/{target}{heading or ''}{alias or ''}]]"

    return _WIKILINK_RE.sub(repl, markdown)


def _is_external_or_prefixed(target: str, prefix: str) -> bool:
    return "://" in target or target.startswith("/") or target.startswith(f"{prefix}/") or target.startswith("#")


async def sync_space_to_vault(
    *,
    client: GlossaClient,
    space_id: str,
    vault_path: Path,
    subdir: str = "Glossa",
    limit: int = 1000,
    include_lint_report: bool = True,
) -> SyncResult:
    """Fetch a Space over the HTTP API and write markdown files into a vault."""
    root = vault_path / subdir.strip("/") if subdir.strip("/") else vault_path
    root.mkdir(parents=True, exist_ok=True)
    link_prefix = subdir.strip("/")

    files_written = 0
    pages_written = 0

    special_pages = [
        ("schema.md", await client.get_schema(space_id)),
        ("index.md", await client.get_index(space_id)),
        ("log.md", await client.get_log(space_id)),
    ]
    if include_lint_report:
        try:
            special_pages.append(("lint_report.md", await client.get_lint_report(space_id)))
        except GlossaClientError as e:
            if e.status != 404:
                raise

    for filename, payload in special_pages:
        content = rewrite_wikilinks(str(payload.get("content") or ""), link_prefix=link_prefix)
        _write_markdown(root / filename, content)
        files_written += 1

    pages = await client.list_pages(space_id, limit=limit)
    for page in pages:
        path = str(page["path"]).removesuffix(".md")
        payload = await client.get_page(space_id, path)
        content = rewrite_wikilinks(str(payload.get("content") or ""), link_prefix=link_prefix)
        _write_markdown(root / f"{path}.md", content)
        files_written += 1
        pages_written += 1

    return SyncResult(
        space_id=space_id,
        vault_path=root,
        files_written=files_written,
        pages_written=pages_written,
    )


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror a Glossa Space into an Obsidian vault.")
    parser.add_argument("--space-id", default=os.environ.get("GLOSSA_DEFAULT_SPACE_ID"))
    parser.add_argument("--vault", default=os.environ.get("GLOSSA_OBSIDIAN_VAULT"))
    parser.add_argument("--subdir", default=os.environ.get("GLOSSA_OBSIDIAN_SUBDIR", "Glossa"))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("GLOSSA_OBSIDIAN_PAGE_LIMIT", "1000")))
    parser.add_argument("--skip-lint-report", action="store_true")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.space_id:
        raise SystemExit("--space-id is required or set GLOSSA_DEFAULT_SPACE_ID")
    if not args.vault:
        raise SystemExit("--vault is required or set GLOSSA_OBSIDIAN_VAULT")

    async with GlossaClient.from_env() as client:
        result = await sync_space_to_vault(
            client=client,
            space_id=args.space_id,
            vault_path=Path(args.vault).expanduser(),
            subdir=args.subdir,
            limit=args.limit,
            include_lint_report=not args.skip_lint_report,
        )

    sys.stdout.write(
        f"Synced {result.pages_written} pages ({result.files_written} markdown files total) to {result.vault_path}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(asyncio.run(_amain(argv)))


if __name__ == "__main__":
    main()
