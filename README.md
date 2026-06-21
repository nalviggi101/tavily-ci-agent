# Competitive Intelligence Briefing Agent

> **Design principle:** This agent does exactly one thing and refuses everything else.
>
> A general-purpose assistant with broad access is hard to trust, hard to debug, and hard to improve. A focused agent with a clear scope — company research, market trends, funding, products, industry developments — is predictable, auditable, and safe to deploy to real users. Every input is classified at the boundary; off-topic requests are rejected before a single search or LLM synthesis call runs.

A CLI that turns any company name or research topic into a structured, cited intelligence briefing in under 60 seconds.

```
uv run agent.py "Perplexity AI"
uv run agent.py "Anthropic funding"
uv run agent.py "AI search market Q2 2025" --days 30
uv run agent.py "write me a poem"   # rejected by guardrails
uv run agent.py "OpenAI" --no-trace # skip Langfuse
```

## What it does

The agent runs six sequential steps, each traced as a named span in Langfuse:

### 1. Guardrails (`guardrail-classify`)
A fast LLM call classifies the input as `PASS` or `REJECT` before any search runs. Off-topic queries (poems, weather, sports) get a friendly redirect immediately. This keeps the agent purposeful and prevents wasted API calls on irrelevant input.

### 2. Structured intent extraction (`extract-intent`)
Rather than treating the input as a raw string, the agent parses it into three structured fields:
- **entity** — the primary company, product, or market (e.g. `Anthropic`)
- **angle** — what the user actually wants: `funding`, `product`, `legal`, `competitive`, `market`, or `general`
- **recency** — `latest`, `background`, or `both`

This means `"Anthropic funding"` and `"Anthropic lawsuits"` produce entirely different sub-queries, even though both mention Anthropic.

### 3. Intent-driven query decomposition (`decompose-queries`)
Three targeted sub-queries are generated from the structured intent — not from the raw input string. A query with `angle=funding` gets queries about rounds, valuations, and investors. A query with `angle=legal` gets queries about lawsuits and regulatory filings.

### 4. Tavily search (`tavily-search` × 3)
Each sub-query runs through TavilySearch with `search_depth="advanced"`. Results are deduplicated by URL across all three queries, yielding up to 15 unique sources.

### 5. Synthesis (`synthesize-briefing`)
Results are assembled into a numbered context block. The LLM produces a Markdown briefing with an executive summary, bulleted key developments with inline `[N]` citations, and a sourced references list. Every factual claim is traceable to a specific source.

### 6. LLM-as-judge (`llm-judge`)
After synthesis, a separate LLM call scores the briefing on three dimensions (1–5):
- **relevance** — does it answer what the user asked?
- **citation_coverage** — is every claim backed by a citation?
- **recency** — does it reflect current information?

Scores are logged to Langfuse via `create_score()` and displayed in the terminal. Over time, this makes quality regressions visible in the Langfuse dashboard — filterable by score, model, or topic.

---

## Setup

```bash
cp .env.example .env
# Fill in TAVILY_API_KEY, NEBIUS_API_KEY, and optionally LANGFUSE_* keys
uv run agent.py "OpenAI"
```

**Required:**
| Variable | Where to get it |
|---|---|
| `TAVILY_API_KEY` | https://app.tavily.com |
| `NEBIUS_API_KEY` | https://tokenfactory.nebius.com |

**Optional (tracing + quality scores):**
| Variable | Notes |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | https://cloud.langfuse.com |
| `LANGFUSE_SECRET_KEY` | |
| `LANGFUSE_BASE_URL` | e.g. `https://us.cloud.langfuse.com` |

## Live trace example

A real trace from the validation runs is visible in the Langfuse dashboard:

**[briefing: Anthropic funding — trace `7471326e`](https://us.cloud.langfuse.com/project/cmqnywych01n1ad0cddwnr72u/traces/7471326e0b3bab7f0573a426e7ad37ee)**

Shows all 6 spans (`guardrail-classify` → `extract-intent` → `decompose-queries` → `tavily-search` ×3 → `synthesize-briefing` → `llm-judge`) with the 3 quality scores attached to the parent trace.

---

## CLI options

| Flag | Default | Description |
|---|---|---|
| `--days N` | 14 | Recency window passed to Tavily |
| `--model NAME` | `moonshotai/Kimi-K2.6` | Nebius model |
| `--no-trace` | off | Disable Langfuse tracing |

---

## Technical statement

### Design philosophy: specialized agents over general assistants

The single most important architectural decision in this project is what the agent *refuses* to do.

A general-purpose assistant with broad access is a liability: unpredictable scope, hard to audit, and impossible to improve systematically because you don't know what it might be asked next. A specialized agent — one that does exactly one job and enforces that boundary at every input — is the opposite: predictable, auditable, and improvable because its failure modes are well-defined.

This is the same principle behind purpose-built agent architectures: instead of one omnipotent assistant, you build a fleet of focused agents, each with a clear scope, appropriate access, and a well-defined interface. This agent is scoped to competitive intelligence. It will not write code, answer trivia, or summarize emails — and it enforces that explicitly, before any downstream cost is incurred.

### What I changed and why

The starter agent is a thin single-turn wrapper: one generic search, freeform prose output, no input validation, no observability. The improvements address four distinct failure modes that would matter to a real customer.

**1. No input validation → wasted API calls and confused users.**
The guardrails gate adds a single fast LLM call that rejects off-topic queries before any Tavily search runs. This is a standard production pattern: scope enforcement at the boundary, not after expensive downstream work has already been done.

**2. Generic query decomposition → mismatched retrieval.**
The original approach generates sub-queries from a raw string. "Anthropic funding" and "Anthropic lawsuits" would produce similar-looking queries because both mention Anthropic. Structured intent extraction first parses the query into `entity + angle + recency`, then uses those fields to generate targeted sub-queries. The decomposition prompt changes based on what the user actually wants, not just what they typed.

**3. Unstructured output → hard to trust or reuse.**
Freeform prose with URLs buried at the end is the common failure mode for research agents. Numbered inline citations tied to a structured sources section make every factual claim auditable — a requirement in any professional research context.

**4. No quality signal → impossible to improve.**
Without a quality loop, you can't tell whether the agent is getting better or worse as prompts and models change. The LLM-as-judge scores the briefing on relevance, citation coverage, and recency after each run. Langfuse stores these as scores on the trace, making quality trends visible and filterable in the dashboard over time.

### Design decisions

- **Single file, `uv` inline deps.** Zero setup friction — no virtualenv, no install step, no requirements.txt drift.
- **Langfuse v4 OpenAI drop-in** (`from langfuse.openai import OpenAI`). Tokens, model names, and generation I/O are captured automatically. `@observe` decorators add the step-level span hierarchy on top.
- **Graceful tracing.** Langfuse is optional — the agent runs identically without keys, falling back cleanly via `LANGFUSE_TRACING_ENABLED=false`.
- **Token budget awareness.** Kimi-K2.6 is a reasoning model that uses ~250 thinking tokens before any visible output. Short calls (guardrail, intent, decompose) use a 1024-token budget; synthesis uses 8192.

### Business value

A competitive intelligence analyst spending 20 minutes compiling a briefing gets the same artifact in under 60 seconds, with sources. Guardrails keep the tool on-task. Intent extraction makes retrieval significantly more targeted for specific research angles (funding vs. legal vs. product). The judge loop gives a team deploying this at scale the ability to track quality over time and catch regressions before users notice them.
