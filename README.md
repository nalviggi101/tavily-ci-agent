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

My background is in solutions engineering, and at Pryon I worked as an FDE on accounts like NVIDIA (Docs Hub) and Remedy Medical — building production retrieval pipelines that actually had to work for real users.

One thing I've taken from that work: a successful agentic retrieval pipeline follows a consistent template. You check whether the input is in scope, you decouple the prompt from the retrieval step, you run the search, you compare or evaluate the results with an LLM, and then you format the response. That's the skeleton. The starter agent skipped most of it.

The other thing I believe strongly: agents should be scoped to one job. Instead of one general assistant with broad access, you build focused agents for specific workflows, teams, or risk levels. So I picked competitive intelligence specifically — not because it was the easiest fit, but because it let me enforce a real boundary and build something with a clear user in mind: a research analyst who needs a sourced briefing fast, not a chatbot that might answer anything.

The six-step pipeline here maps directly to that template: guardrail → intent extraction → query decomposition → search → synthesis → judge. Langfuse tracing was a natural addition — in production work you need to see what's happening inside the pipeline, not just whether it returned something.
