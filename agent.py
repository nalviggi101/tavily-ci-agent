# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "openai>=1.50.0",
#   "langchain-tavily>=0.2.0",
#   "langfuse>=4.0.0",
#   "python-dotenv>=1.0.0",
#   "rich>=13.0.0",
#   "typer>=0.12.0",
# ]
# ///
"""
Competitive Intelligence Briefing Agent

Design principle: this agent does exactly one thing — produce sourced,
cited competitive intelligence briefings — and does it well. It refuses
all other requests at the boundary. A focused agent with a clear scope is
more trustworthy, more debuggable, and easier to improve than a general
assistant with broad, unpredictable access.

Pipeline:
  1. Guardrails      — off-topic queries rejected before any search runs
  2. Intent extract  — entity, angle (funding/product/legal/…), recency
  3. Decompose       — 3 targeted sub-queries driven by extracted intent
  4. Tavily search   — advanced depth, deduplicated across all sub-queries
  5. Synthesis       — structured Markdown with numbered [N] citations
  6. LLM-as-judge    — relevance / citation coverage / recency scored 1–5,
                       logged to Langfuse for quality tracking over time

Usage:
  uv run agent.py "Perplexity AI"
  uv run agent.py "Anthropic funding" --days 30
  uv run agent.py "write me a poem"    # rejected by guardrails
  uv run agent.py "OpenAI" --no-trace

Required env vars (.env or shell):
  TAVILY_API_KEY      — https://app.tavily.com
  NEBIUS_API_KEY      — https://tokenfactory.nebius.com

Optional (for tracing + scoring):
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_BASE_URL   — e.g. https://us.cloud.langfuse.com
"""

from __future__ import annotations

import json
import os
import textwrap
from dataclasses import dataclass
from typing import Annotated, Any

import typer

# Load env vars BEFORE importing Langfuse — it reads credentials at import time
from dotenv import load_dotenv
load_dotenv()

from langchain_tavily import TavilySearch
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
console = Console()

NEBIUS_BASE_URL = "https://api.studio.nebius.com/v1"
DEFAULT_MODEL = "moonshotai/Kimi-K2.6"

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GUARD_PROMPT = """\
You are a classifier for a competitive intelligence research tool. Determine \
whether the user's input is a valid competitive intelligence or business research \
query (about companies, markets, products, funding, industry trends, competitors, \
or technology developments).

Respond with EXACTLY one word: PASS or REJECT.

Examples that PASS:
- "Perplexity AI"
- "OpenAI vs Anthropic"
- "AI search market trends 2025"
- "Nvidia funding and valuation"

Examples that REJECT:
- "write me a poem"
- "what is the weather today"
- "who won the Super Bowl"
- "tell me a joke"

Input: {topic}"""

INTENT_PROMPT = """\
You are a research intent parser. Extract structured intent from the user's \
competitive intelligence query.

Return a JSON object with exactly these fields:
  "entity"  — the primary company, product, or market being researched (string)
  "angle"   — the primary focus area; one of: funding, product, legal, \
competitive, market, general
  "recency" — how time-sensitive the query is; one of: latest, background, both

Return ONLY the JSON object. No explanation. Example:
{{"entity": "Perplexity AI", "angle": "funding", "recency": "latest"}}

Query: {topic}"""

DECOMPOSE_PROMPT = """\
You are a research strategist generating targeted web search queries.

Topic: {entity}
Focus: {angle}
Time sensitivity: {recency}

Generate 3 specific, complementary search queries that together give broad \
coverage of the focus area. Make queries concrete and search-engine-friendly.

List exactly 3 queries, one per line, with no numbering, bullets, or extra text."""

SYNTHESIZE_PROMPT = """\
You are a senior research analyst producing a concise competitive intelligence \
briefing. You will be given a topic and a set of search results, each tagged \
with a source index [1], [2], etc.

Your output must be a Markdown document with these sections:
  ## Summary
  2–3 sentence executive summary.

  ## Key Developments
  Bullet points of the most important recent findings. Cite sources inline \
using [N] notation.

  ## Sources
  Numbered list matching the citation indices: [N] Title — URL

Rules:
- Only use information from the provided search results.
- Every factual claim must have an inline citation.
- Be concise. Aim for ~300 words total (not counting the Sources section).
- Do not hallucinate or extrapolate beyond the sources.

Topic: {topic}

Search results:
{results}"""

JUDGE_PROMPT = """\
You are a quality evaluator for competitive intelligence briefings.

Score the briefing below on three dimensions, each from 1 to 5:

  relevance        — Does the briefing directly answer the original query?
                     5 = fully on-target, 1 = off-topic or generic
  citation_coverage — Is every factual claim backed by an inline citation?
                     5 = all claims cited, 1 = few or no citations
  recency          — Does the briefing reflect current / recent information?
                     5 = very current, 1 = outdated or no dates mentioned

Return ONLY a JSON object with integer scores. Example:
{{"relevance": 4, "citation_coverage": 5, "recency": 3}}

Original query: {topic}

Briefing:
{briefing}"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    entity: str
    angle: str   # funding | product | legal | competitive | market | general
    recency: str # latest | background | both


# ---------------------------------------------------------------------------
# Tracing helpers
# ---------------------------------------------------------------------------

def _tracing_configured() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _disable_tracing() -> None:
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"


def _make_client(tracing: bool) -> Any:
    if tracing:
        from langfuse.openai import OpenAI  # drop-in: auto-captures tokens + model
    else:
        from openai import OpenAI  # type: ignore[assignment]
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=os.environ["NEBIUS_API_KEY"])


def _chat(client: Any, *, model: str, system: str, user: str, max_tokens: int = 1024) -> str:
    """Single-turn non-streaming chat call shared by guardrail, intent, and judge steps."""
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _parse_json(raw: str, fallback: dict) -> dict:
    """Parse JSON from an LLM response, stripping markdown fences if present."""
    clean = raw.strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.split("\n")[1:]).rstrip("`").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return fallback


# ---------------------------------------------------------------------------
# Step 1 — Guardrails
# ---------------------------------------------------------------------------

def classify_intent(topic: str, *, model: str, client: Any) -> bool:
    """Return True if the topic is a valid CI query, False if it should be rejected."""
    from langfuse import observe, get_client

    lf = get_client()

    @observe(name="guardrail-classify")
    def _run() -> bool:
        lf.update_current_span(input={"topic": topic})
        raw = _chat(
            client,
            model=model,
            system="You are a strict input classifier. Follow instructions exactly.",
            user=GUARD_PROMPT.format(topic=topic),
        )
        verdict = raw.upper().startswith("PASS")
        lf.update_current_span(output={"verdict": "PASS" if verdict else "REJECT"})
        return verdict

    return _run()


# ---------------------------------------------------------------------------
# Step 2 — Structured intent extraction
# ---------------------------------------------------------------------------

def extract_intent(topic: str, *, model: str, client: Any) -> Intent:
    """Parse entity, angle, and recency from the user's query."""
    from langfuse import observe, get_client

    lf = get_client()

    @observe(name="extract-intent")
    def _run() -> Intent:
        lf.update_current_span(input={"topic": topic})
        raw = _chat(
            client,
            model=model,
            system="You are a precise intent parser. Return only the JSON object requested.",
            user=INTENT_PROMPT.format(topic=topic),
        )
        data = _parse_json(raw, {"entity": topic, "angle": "general", "recency": "latest"})
        intent = Intent(
            entity=data.get("entity") or topic,
            angle=data.get("angle") or "general",
            recency=data.get("recency") or "latest",
        )
        lf.update_current_span(output={"entity": intent.entity, "angle": intent.angle, "recency": intent.recency})
        return intent

    return _run()


# ---------------------------------------------------------------------------
# Step 3 — Query decomposition (intent-driven)
# ---------------------------------------------------------------------------

def decompose_queries(intent: Intent, *, model: str, client: Any) -> list[str]:
    """Generate 3 targeted sub-queries from structured intent."""
    from langfuse import observe, get_client

    lf = get_client()

    @observe(name="decompose-queries")
    def _run() -> list[str]:
        lf.update_current_span(input={"entity": intent.entity, "angle": intent.angle, "recency": intent.recency})
        raw = _chat(
            client,
            model=model,
            system="You are a precise research strategist. Output only what is requested.",
            user=DECOMPOSE_PROMPT.format(
                entity=intent.entity,
                angle=intent.angle,
                recency=intent.recency,
            ),
            max_tokens=1024,
        )
        lines = [
            ln.strip().lstrip("-•123456789.) ")
            for ln in raw.splitlines()
            if ln.strip()
        ]
        queries = [ln for ln in lines if ln][:3] or [intent.entity]
        lf.update_current_span(output={"queries": queries})
        return queries

    return _run()


# ---------------------------------------------------------------------------
# Step 4 — Tavily search
# ---------------------------------------------------------------------------

def run_searches(queries: list[str]) -> list[dict[str, Any]]:
    """Run each sub-query through TavilySearch, deduplicating by URL."""
    from langfuse import observe, get_client

    lf = get_client()
    search = TavilySearch(max_results=5, search_depth="advanced", include_raw_content=False)
    seen_urls: set[str] = set()
    all_results: list[dict[str, Any]] = []

    @observe(name="tavily-search")
    def _search_one(query: str) -> list[dict[str, Any]]:
        lf.update_current_span(input={"query": query})
        try:
            raw = search.invoke({"query": query})
        except Exception as exc:
            console.print(f"[dim red]Search failed for '{query}': {exc}[/dim red]")
            lf.update_current_span(metadata={"error": str(exc)})
            return []
        results: list[dict[str, Any]] = (
            raw if isinstance(raw, list) else raw.get("results", [])
        )
        new = [r for r in results if r.get("url") not in seen_urls]
        lf.update_current_span(output={"new_results": len(new)})
        return new

    for query in queries:
        new = _search_one(query)
        for r in new:
            seen_urls.add(r.get("url", ""))
        all_results.extend(new)

    return all_results


# ---------------------------------------------------------------------------
# Step 5 — Synthesis
# ---------------------------------------------------------------------------

def synthesize_briefing(topic: str, results: list[dict[str, Any]], *, model: str, client: Any) -> str:
    """Stream a structured Markdown briefing from the gathered search results."""
    from langfuse import observe, get_client

    lf = get_client()

    @observe(name="synthesize-briefing")
    def _run() -> str:
        lf.update_current_span(input={"topic": topic, "source_count": len(results)})
        context_block = _format_results(results)
        prompt = SYNTHESIZE_PROMPT.format(topic=topic, results=context_block)
        stream = client.chat.completions.create(
            model=model,
            max_tokens=8192,
            stream=True,
            messages=[
                {
                    "role": "system",
                    "content": "You are a senior research analyst. Produce concise, well-cited Markdown briefings using only the provided sources.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        briefing = "".join(chunk.choices[0].delta.content or "" for chunk in stream)
        lf.update_current_span(output={"chars": len(briefing)})
        return briefing

    return _run()


# ---------------------------------------------------------------------------
# Step 6 — LLM-as-judge
# ---------------------------------------------------------------------------

def evaluate_briefing(topic: str, briefing: str, *, model: str, client: Any, trace_id: str | None) -> dict[str, int]:
    """Score the briefing on relevance, citation coverage, and recency.
    Scores are logged to Langfuse so quality can be tracked and filtered over time.
    """
    from langfuse import observe, get_client

    lf = get_client()

    @observe(name="llm-judge")
    def _run() -> dict[str, int]:
        lf.update_current_span(input={"topic": topic, "briefing_chars": len(briefing)})
        raw = _chat(
            client,
            model=model,
            system="You are a strict quality evaluator. Return only the JSON object requested.",
            user=JUDGE_PROMPT.format(topic=topic, briefing=briefing[:3000]),
            max_tokens=1024,
        )
        scores = _parse_json(raw, {"relevance": 3, "citation_coverage": 3, "recency": 3})
        # Clamp to 1–5
        result = {k: max(1, min(5, int(scores.get(k, 3)))) for k in ("relevance", "citation_coverage", "recency")}
        lf.update_current_span(output=result)

        # Log each dimension as a Langfuse score on the parent trace
        if trace_id:
            for score_name, value in result.items():
                lf.create_score(
                    trace_id=trace_id,
                    name=score_name,
                    value=value,
                    data_type="NUMERIC",
                    comment="LLM-as-judge (1–5)",
                )
        return result

    return _run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_results(results: list[dict[str, Any]], max_results: int = 8) -> str:
    lines: list[str] = []
    for i, r in enumerate(results[:max_results], start=1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = textwrap.shorten(r.get("content", "").strip(), width=300, placeholder="...")
        lines.append(f"[{i}] {title}\n    URL: {url}\n    {content}\n")
    return "\n".join(lines)


def _score_bar(score: int) -> str:
    filled = "█" * score
    empty = "░" * (5 - score)
    return f"{filled}{empty} {score}/5"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@app.command()
def main(
    topic: Annotated[list[str], typer.Argument(help="Company name or research topic")],
    days: Annotated[int, typer.Option(help="Recency window for news search (days)")] = 14,
    model: Annotated[str, typer.Option(help="Nebius model name")] = DEFAULT_MODEL,
    no_trace: Annotated[bool, typer.Option("--no-trace", help="Disable Langfuse tracing")] = False,
) -> None:
    """Produce a sourced competitive intelligence briefing on any topic."""

    for var, url in [
        ("TAVILY_API_KEY", "https://app.tavily.com"),
        ("NEBIUS_API_KEY", "https://tokenfactory.nebius.com"),
    ]:
        if not os.getenv(var):
            console.print(f"[bold red]Missing {var}[/bold red] — get one at {url}")
            raise typer.Exit(code=1)

    tracing = not no_trace and _tracing_configured()
    if not tracing:
        _disable_tracing()
        if not no_trace:
            console.print("[dim yellow]Langfuse keys not set — tracing disabled.[/dim yellow]")

    from langfuse import observe, get_client, propagate_attributes

    lf = get_client()
    topic_text = " ".join(topic)
    client = _make_client(tracing=tracing)

    @observe(name="briefing")
    def run_briefing() -> None:
        with propagate_attributes(
            trace_name=f"briefing: {topic_text}",
            tags=["competitive-intelligence"],
            metadata={"days": days, "model": model},
        ):
            lf.update_current_span(input=topic_text)

            console.print(Panel.fit(topic_text, title="Briefing request", border_style="cyan"))

            # ── Step 1: Guardrails ──────────────────────────────────────────
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Checking query…")
                is_valid = classify_intent(topic_text, model=model, client=client)

            if not is_valid:
                console.print(
                    Panel(
                        "[yellow]This agent is scoped to competitive intelligence research —\n"
                        "company analysis, market trends, funding, products, and industry developments.\n\n"
                        "Please ask a question in one of those areas.[/yellow]",
                        title="Out of scope",
                        border_style="red",
                    )
                )
                lf.update_current_span(output="rejected: off-topic", metadata={"guardrail": "REJECT"})
                return

            # ── Step 2: Intent extraction ───────────────────────────────────
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Understanding intent…")
                intent = extract_intent(topic_text, model=model, client=client)

            console.print(
                f"\n[dim]Intent — entity:[/dim] [bold]{intent.entity}[/bold]  "
                f"[dim]angle:[/dim] [bold]{intent.angle}[/bold]  "
                f"[dim]recency:[/dim] [bold]{intent.recency}[/bold]"
            )

            # ── Step 3: Query decomposition ─────────────────────────────────
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Planning searches…")
                queries = decompose_queries(intent, model=model, client=client)

            console.print("\n[bold]Sub-queries:[/bold]")
            for i, q in enumerate(queries, 1):
                console.print(f"  {i}. [yellow]{q}[/yellow]")

            # ── Step 4: Tavily search ───────────────────────────────────────
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Searching with Tavily…")
                results = run_searches(queries)

            if not results:
                console.print("[bold red]No results returned. Check your TAVILY_API_KEY.[/bold red]")
                raise typer.Exit(code=1)

            console.print(f"[dim]Retrieved {len(results)} unique sources[/dim]\n")

            # ── Step 5: Synthesis ───────────────────────────────────────────
            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Writing briefing…")
                briefing = synthesize_briefing(topic_text, results, model=model, client=client)

            console.rule("[bold blue]Briefing")
            console.print(Markdown(briefing))

            # ── Step 6: LLM-as-judge ────────────────────────────────────────
            # Get the active trace id so scores are attached to the right trace
            current_trace_id: str | None = None
            try:
                current_trace_id = lf.get_current_trace_id()
            except Exception:
                pass

            with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console, transient=True) as p:
                p.add_task("Evaluating quality…")
                scores = evaluate_briefing(topic_text, briefing, model=model, client=client, trace_id=current_trace_id)

            console.rule("[bold]Quality scores[/bold]")
            labels = {"relevance": "Relevance", "citation_coverage": "Citation coverage", "recency": "Recency"}
            for key, label in labels.items():
                console.print(f"  {label:20s} {_score_bar(scores[key])}")
            console.print()

            lf.update_current_span(output=briefing[:500], metadata={"scores": scores})

    run_briefing()
    lf.flush()


if __name__ == "__main__":
    app()
