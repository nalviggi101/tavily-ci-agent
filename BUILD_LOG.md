# Build Log — Competitive Intelligence Briefing Agent

**Assignment:** Tavily FDE Take-Home — Option 1 (Improve an existing application)  
**Date:** 2026-06-21  
**Author:** Nicholas Alviggi  
**AI tooling:** Claude Code (claude-sonnet-4-6) via the Claude Agent SDK  
**Development framework:** [ai-dev-playbook](https://github.com/nicholasalviggi/ai-dev-playbook) — a personal framework for structured agentic development

---

## Development approach — ai-dev-playbook

This project was built using the **ai-dev-playbook**, a framework I developed for working effectively with AI coding agents. The playbook enforces a Planner/Executor model: before any code is written, the agent and I align on a concrete task list with acceptance criteria. Only then does execution begin, one task at a time.

In practice for this project:

- **Planner phase:** Before building, I read the brief, identified the four key failure modes in the starter agent, and proposed a direction (competitive intelligence briefing, Langfuse tracing, structured output). Only after agreeing on the approach did implementation start.
- **Task-by-task execution:** Each feature (core pipeline, tracing, guardrails, intent extraction, LLM-as-judge) was treated as a discrete task — implemented, tested, and verified before moving to the next. Tasks were tracked in the session using the playbook's Beads-style structure.
- **Verify before declaring done:** Each step was run live against the real API before being marked complete. Problems discovered at runtime (Kimi-K2.6 token budget, Langfuse v4 API changes, `name=` kwarg incompatibility) were diagnosed and fixed before proceeding.
- **Agent identity discipline:** The agent was treated as a tool that executes well-defined tasks, not an autonomous collaborator. Every decision — which framework to drop, which token budget to use, which Langfuse API to call — was reasoned through explicitly rather than accepted as model output.

The playbook's principle that "understanding over authorship" is the quality bar was central here: every line of the final agent was reviewed, understood, and verified against live runs before being accepted.

---

## Overview

This document records how the solution was built — decisions made, problems hit, and how they were resolved. It is the build record required by the assignment.

---

## Phase 1 — Reading the brief and forming a strategy

**Starting point:** `starter_agent.py` — a minimal CLI that takes a question, runs one `TavilySearch()` with default parameters, and streams a freeform answer via a LangChain agent backed by Nebius/Kimi-K2.6.

**Identified weaknesses:**
- One generic search → poor coverage for multi-dimensional topics
- No input validation → any query accepted regardless of relevance
- Freeform prose output → citations buried in text, nothing traceable
- No observability → impossible to debug or measure quality over time
- `max_tokens=256` for Kimi-K2.6 — the model is a reasoning model that uses ~250 thinking tokens before visible output; any short budget returned `None`

**Strategic decision:** Rather than building a generic Q&A chatbot (a commodity), adapt the agent to a specific high-value workflow — **competitive intelligence briefing** — with scoped input, structured output, and measurable quality. This gives a clear business narrative: a research analyst gets a sourced, citable briefing in <60 seconds instead of 20 minutes of manual work.

**Architecture principle — specialization over generality:** A key design choice was to make this agent *refuse* anything outside its scope rather than attempt to handle all inputs. A general-purpose assistant with broad access is unpredictable, hard to audit, and difficult to improve because its failure surface is unbounded. A focused agent — one job, one domain, one well-defined interface — is the opposite. This is the same philosophy behind purpose-built agent architectures: instead of one omnipotent assistant, build a fleet of focused agents, each with clear scope and appropriate access. The guardrails gate (added in Phase 4) is the concrete implementation of this principle: every input is classified before any downstream cost is incurred.

---

## Phase 2 — Core pipeline (single search → multi-query decomposition)

**Decision:** Drop LangChain's `create_agent` abstraction. The starter used it as a black box; replacing it with direct OpenAI SDK calls gives full control over every prompt and LLM call, which is necessary for structured output and proper tracing.

**Changed:** Nebius is OpenAI-API-compatible, so switched from `langchain-nebius` to the `openai` SDK pointed at `https://api.studio.nebius.com/v1`. This is a one-line base URL change and no other code needs to know.

**Added:** Multi-query decomposition — the LLM generates 3 targeted sub-queries before any search runs. Each sub-query runs through `TavilySearch(search_depth="advanced")`, and results are deduplicated by URL. This takes the source count from 5 (one search) to up to 15 (three searches, deduplicated).

**Problem hit:** Kimi-K2.6 returned `None` content with `finish_reason="length"` on short token budgets. Root cause: it's a reasoning model that uses ~250 "thinking" tokens before any visible output. Fixed by raising `max_tokens` to 1024 for short calls and 8192 for synthesis.

**Problem hit:** Initial decompose prompt asked for JSON output (`["q1","q2","q3"]`). The model refused JSON-only constraints (returning `None`). Fixed by switching to a natural language list prompt ("one per line") and parsing the newlines in Python.

**Added:** Structured Markdown synthesis with numbered `[N]` inline citations and a `## Sources` section. Every factual claim is tied to a specific source.

---

## Phase 3 — Langfuse tracing

**Decision:** Use Langfuse v4's **OpenAI drop-in replacement** (`from langfuse.openai import OpenAI`) rather than manual span creation. This auto-captures model name, token counts, and generation I/O on every LLM call without any extra code.

**Added:** `@observe` decorators on each pipeline step (`decompose-queries`, `tavily-search`, `synthesize-briefing`) to create a nested span hierarchy in Langfuse. Per Langfuse best practices:
- `load_dotenv()` called before Langfuse imports — the SDK reads credentials at import time
- `lf.update_current_span(input=..., output=...)` used to set only relevant I/O, not full function signatures
- `lf.flush()` called before process exit — required for scripts to prevent buffered spans from being dropped
- Tracing is optional: falls back gracefully via `LANGFUSE_TRACING_ENABLED=false` when keys are absent

**Env var fix:** Langfuse v4 reads `LANGFUSE_BASE_URL`, not `LANGFUSE_HOST`. Updated `.env` accordingly.

**API changes discovered at runtime:**
- `langfuse_context` doesn't exist in v4 — replaced with `lf.update_current_span()` and `propagate_attributes()`
- `set_current_trace_io()` is deprecated in v4 — replaced with `update_current_span()`
- `lf.score()` doesn't exist in v4 — correct method is `lf.create_score()`

Each issue was caught by running the agent and reading the traceback, then checking `dir(lf)` against the installed package version to find the correct API.

---

## Phase 4 — Guardrails, intent extraction, and LLM-as-judge

### Guardrails

**Rationale:** A scoped agent should enforce its scope at the boundary, before expensive downstream work runs. One fast LLM call classifies input as `PASS` or `REJECT`. Rejected queries get a friendly redirect immediately; no Tavily or synthesis calls are made.

**Problem hit:** The `name=` kwarg on `client.chat.completions.create()` is only accepted by the Langfuse-wrapped OpenAI client, not the plain OpenAI client used when `--no-trace` is set. Fixed by removing `name=` from the shared `_chat()` helper — the `@observe` span names already provide the labeling in Langfuse.

### Structured intent extraction

**Rationale:** Raw string decomposition ("Anthropic funding" → generic Anthropic queries) doesn't respect what the user actually wants. Parsing the input into `entity + angle + recency` first lets the decompose prompt generate sub-queries that are specific to the angle (funding, product, legal, competitive, market, general).

**Example:** `"Anthropic funding"` → `entity=Anthropic, angle=funding, recency=latest` → all three sub-queries are about funding rounds, valuations, and investors.

**Implementation:** A separate LLM call returns a small JSON object. Wrapped in a robust `_parse_json()` helper that strips markdown code fences and falls back to sensible defaults on parse failure.

### LLM-as-judge

**Rationale:** Without a quality signal, there is no way to know whether the agent is improving or regressing as prompts and models change. A separate judge call scores the briefing on `relevance`, `citation_coverage`, and `recency` (1–5). Scores are logged to Langfuse via `lf.create_score()` and displayed in the terminal.

**Value:** In the Langfuse dashboard, traces can now be filtered by score. Over a production deployment, this surfaces quality degradation before users notice it.

---

## Final pipeline

```
User input
    │
    ▼
[guardrail-classify]  ── REJECT ──► friendly redirect message
    │ PASS
    ▼
[extract-intent]      entity / angle / recency
    │
    ▼
[decompose-queries]   3 targeted sub-queries (angle-aware)
    │
    ▼
[tavily-search × 3]   up to 15 deduplicated sources
    │
    ▼
[synthesize-briefing] structured Markdown with [N] citations
    │
    ▼
[llm-judge]           relevance / citation_coverage / recency scores
    │
    ▼
Langfuse trace (6 named spans + 3 scores)
```

---

## Key technical decisions

| Decision | Why |
|---|---|
| Drop LangChain agent | Full control over prompts, tokens, and tracing |
| OpenAI SDK → Nebius base URL | Provider-agnostic; one-line swap to any OpenAI-compatible endpoint |
| Langfuse drop-in OpenAI client | Auto-captures tokens + model; zero boilerplate per LLM call |
| `@observe` decorators | Clean span hierarchy without manual span management |
| Natural language list → parse newlines | Kimi-K2.6 refuses JSON-only constraints; line parsing is equally reliable |
| `max_tokens=1024` for short calls, `8192` for synthesis | Reasoning model needs thinking budget before visible output |
| Intent extraction before decomposition | Angle-aware queries significantly improve retrieval relevance |
| LLM-as-judge + Langfuse scores | Turns a demo into something production-observable |
