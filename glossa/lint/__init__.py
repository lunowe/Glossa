"""Lint pass over one space's wiki.

Detects orphan pages, broken wikilinks (deterministic), and contradictions or
supersessions across a page's cited sources (LLM-driven). Produces a
``lint_report.md`` page and a ``Job`` result with structured findings.
"""
