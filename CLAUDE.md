
# CLAUDE.md — Multi-Agent Spec for Weekly Async Course Content Generator (with Internet Tools)

## 1) Purpose

Build a **Python + LangGraph** workflow that generates the **weekly content package** for a Master’s-level **Data Science** async course. The system reads the **course syllabus**, **weekly learning objectives**, a **document template**, and **authoring guidelines**, then produces section-by-section Markdown, validates it with expert agents, and assembles the final **`./weekly_content/Week#.md`**.
This version explicitly adds **internet tools** for freshness.

---

## 2) High-Level Architecture

### Agents (LLM roles)

1. **ProgramDirector** (orchestrator & gatekeeper)
   Controls the flow with LangGraph; requests one section at a time from ContentExpert; routes drafts to EducationExpert and AlphaStudent; writes approved sections to `./temporal_output`; assembles the final `Week#.md`.

2. **ContentExpert** (author)
   Writes each section using `course_syllabus.docx` and `guidelines.md`.
   Has internet search & retrieval tools to gather **fresh, authoritative** examples, definitions, citations, and updated statistics.
   Can read previously approved material in `./temporal_output` to preserve continuity.
   Needs access to the Syllabus to understand the WLOs and use the correct bibliography. 

3. **EducationExpert** (pedagogy & template compliance)
   Uses `template.docx` (structure) + `guidelines.md` (style/assessment rules).
   Ensures alignment to CLOs/WLOs and strict template conformance; issues actionable revision notes if needed.

4. **AlphaStudent** (usability & verification)
   Reads the draft like a student; flags unclear parts; checks links resolve; verifies bibliography consistency.

### Tools

* **ContentExpert**

  * `web.search(query, top_k, recency)` → search the web for current, authoritative materials.
  * `web.fetch(url)` → retrieve page snippets/metadata for citation context.
  * Policy: prefer sources ≤ **2 years old** when the topic requires currency; include URLs and publication/last-updated dates when available; prioritize peer-reviewed, official docs, reputable publishers.
* Policy: it can only recommend readings to the students based on the weekly bibliography provided in the Syllabus.
* Needs access the syllabus as well
* **AlphaStudent**

  * `links.check(urls)` → HEAD/GET with redirect follow, timeout, 200 OK required (403 acceptable for paywalled scholarly sources on expected domains).
* **ProgramDirector**

  * File I/O only (assemble, save, log).
* **EducationExpert**

  * Local template/guidelines only; no internet needed.

> Note: **LangGraph provides orchestration, not internet access**. Implement tools using a provider such as **Tavily API (recommended for simplicity)**, **SerpAPI**, **Bing Web Search API**, or **Google Programmable Search Engine (CSE)**. See **Section 5** for secrets and **Section 14** for a minimal wrapper.

---

## 3) Inputs & File Layout

```
./input/
  course_syllabus.docx     # Source for ContentExpert (CLOs, WLOs, syllabus topics)
  template.docx            # Structure & required subsections (EducationExpert ground truth)
  guidelines.md            # Style, length, tone, citation rules

./temporal_output/         # ProgramDirector writes each approved section here
  01-introduction.md
  02-key-concepts.md
  ...

./weekly_content/
  Week#.md                 # ProgramDirector compiles final weekly file

./config/
  sections.json            # Optional: canonical ordered list of sections
  course_config.yaml       # Optional: course/global knobs (citation style, word counts)

.secrets                   # API keys / model endpoints (not committed)
.env                       # Optional local development env
```

Runtime inputs:

* Week number (`WEEK_NUMBER` env or CLI).
* Section order from `template.docx` or `config/sections.json` (the latter wins if present).
* WLOs from syllabus or `course_config.yaml`.

---

## 4) Outputs

* Per-section Markdown in `./temporal_output/{ordinal}-{slug}.md` (approved drafts).
* Final weekly Markdown in `./weekly_content/Week#.md` (ordered, ToC, anchors, merged references).

---

## 5) Secrets & Model Configuration

`.secrets` (INI-style; load into env before run):

```
# LLMs (per agent, override as needed)
MODEL_PROGRAM_DIRECTOR=gpt-4o-mini
MODEL_CONTENT_EXPERT=gpt-4o
MODEL_EDUCATION_EXPERT=gpt-4o-mini
MODEL_ALPHA_STUDENT=gpt-4o-mini
OPENAI_API_KEY=...

# Optional Anthropic/Azure/etc if you swap models
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_API_KEY=...

# Web search provider (pick ONE primary; Tavily recommended for simplicity)
TAVILY_API_KEY=...

# Networking
HTTP_TIMEOUT_SECONDS=15
MAX_REDIRECTS=3
```

Guidelines:

* Never hardcode secrets; load via env.
* Allow agent-specific model selection through env vars above.
* Choose exactly **one** search backend at runtime; expose it via a unified `web.search` tool interface.

---

## 6) Section Taxonomy & Ordering

ProgramDirector derives the ordered sections from `template.docx` or `config/sections.json`.
Example ordering:

* Introduction
* Weekly Learning Objectives
* Required Reading
* Lecture Notes
* Learning Activities
* Assessment & Rubric
* Further Reading & Links
* Summary & Next Steps

---

## 7) LangGraph Workflow (State Machine)

Shared State (pydantic):

* SectionSpec, SectionDraft, ReviewNotes, RunState (unchanged in shape; include `links` in SectionDraft).

Nodes:

* `program_director_plan`
* `program_director_request_section`
* `content_expert_write`
* `education_expert_review`
* `alpha_student_review`
* `program_director_merge_or_revise`
* `program_director_finalize_week`

Control Flow:

1. Plan → RequestSection → ContentExpert → EducationReview → AlphaReview → MergeOrRevise.
2. On approve, write section file and advance.
3. After last approval, finalize week file.

**Internet usage hook**: Inside `content_expert_write`, the ContentExpert may call `web.search` and `web.fetch` **before** or **during** drafting when freshness is needed.

Retry/Timeouts:

* Wrap I/O and link checks with tenacity (exponential backoff, max 3 attempts).
* Fail fast if required input files are missing.

---

## 8) Prompts (Agent-Specific)

### 8.1 ProgramDirector — Section Request (System Prompt)

You are the **ProgramDirector** for a Master’s-level async Data Science course. Orchestrate authoring **one section at a time**. Provide the **section description**, **Weekly Learning Objectives**, constraints from the **template**, relevant context from previously approved sections, and key **style rules**. Require exact Markdown structure, explicit citations, and adherence to guidelines. Output **only** the Markdown for this section, ready to publish.

**Message to ContentExpert includes**:

* SectionSpec (id, title, description, constraints).
* WLOs excerpt.
* Concise context from previously approved sections (≤ 400 tokens).
* Relevant rules from `guidelines.md` (tone, structure, citation style).
* **Freshness requirement**: “When recent data/examples are relevant, use the internet tool to include sources ≤ 2 years old, adding URLs and dates.”

### 8.2 ContentExpert — System Prompt (with Tools & Freshness)

You are the **ContentExpert**, a senior data-science educator and writer. Draft the section in **clear, student-friendly** Markdown that exactly follows the **template** and **guidelines**.
**Tools available**: `web.search`, `web.fetch`.
**When to use tools**: If the topic benefits from **current context** (methods, libraries, benchmarks, case studies, standards, market trends), **search the web** and cite sources **≤ 2 years old**. If only historical sources exist, state this explicitly.

Rules:

* Follow template headings exactly; map content to **WLOs** explicitly.
* Cite all sources per the provided citation style; include URLs and publication/last-updated dates when available.
* Keep paragraphs short; explain jargon; include examples where helpful.
* Do **not** assume prior weeks unless referenced by ProgramDirector.
* Prefer peer-reviewed, official docs, and reputable publishers.

Output: Markdown only.

### 8.3 EducationExpert — System Prompt

You are the **EducationExpert**. Validate the draft against:

* **template.docx** (headings, subsections, rubric location/format),
* **guidelines.md** (tone, accessibility, assessment rules),
* **CLO/WLO alignment** (explicit tagging).
  Return `ReviewNotes` JSON with `approved` (bool), and **specific**, **actionable** `required_fixes` if non-compliant. Reject drafts missing WLO mapping, rubric alignment, or template structure.

### 8.4 AlphaStudent — System Prompt (with Link QA)

You are the **AlphaStudent**. Read the draft as if the teacher is in front of you. Check:

* Clarity and flow; flag jargon without explanation; suggest simpler phrasing.
* **Links**: verify HTTP 200 (allow 301/302→200), identify any dead links.
* **Bibliography**: ensure in-text ↔ references match; consistent style.
  Return `ReviewNotes` JSON; include a list of failing URLs if any.

---

## 9) Content & Compliance Rules

* **Headings** must mirror `template.docx` exactly (H2/H3 levels as specified).
* **WLO mapping**: each major subsection explicitly states which WLO(s) it supports.
* **Citations**: follow the style in `guidelines.md`; include URLs and dates when applicable.
* **Accessibility**: descriptive link text, alt-text where images are allowed, avoid color-only cues.
* **Tone**: clear, supportive, concise (≤ 3 lines per paragraph).
* **Assessment**: rubric criteria tie directly to WLOs.
* **Links**: must resolve to 200; 403 acceptable for known paywalled scholarly domains.
* **Freshness (NEW)**: When examples, data, standards, API changes, or “latest methods” are relevant, **ContentExpert must use the internet tool** and cite sources **≤ 2 years old**. If newer authoritative sources do not exist, the draft must say so explicitly.

---

## 10) File I/O & Assembly Rules

Per-section files in `./temporal_output/{ordinal}-{slug}.md` with optional YAML front-matter:

```
---
section_id: 02-learning-objectives
week: 7
status: approved
---
```

Final file `./weekly_content/Week#.md` must include:

* Title `# Week {#}: {Week Title}`
* Auto-generated ToC (anchor list)
* Sections concatenated in **template order**
* De-duplicated **References** section

---

## 11) Link & Bibliography Verification

* `links.check`: HEAD then GET fallback; timeout `HTTP_TIMEOUT_SECONDS`; follow up to `MAX_REDIRECTS`.
* Treat 301/302→200 as OK.
* Consider 403 OK for paywalled scholarly sources on expected domains.
* Bibliography checks:

  * Every in-text citation appears in References.
  * Every Reference is cited at least once (or labeled “Further Reading”).
  * Normalize style per `guidelines.md`.

---

## 12) Success Criteria (Definition of Done)

1. All sections present and approved by EducationExpert and AlphaStudent.
2. Template conformance and explicit WLO mapping.
3. Assessment & rubric align to WLOs.
4. No dead links; bibliography consistent.
5. `./weekly_content/Week#.md` exists; ToC and anchors validate.
6. Idempotent re-runs with unchanged inputs.
7. Run logs record states, revision counts, and link checks.
8. Citations complete and properly formatted.
9. **Fresh Data (NEW)**: Topics requiring current context include **≤ 2-year-old** sources gathered via `web.search`, with dates/URLs present—or a clear note when only historical sources are authoritative.

---

## 13) Non-Goals

* PDF/DOCX export (future).
* LMS integration.
* Multimedia generation beyond links.

---

## 14) Minimal Code Skeleton (with Web Tool)

```python
# app/tools/web.py
import os, requests
from typing import List, Dict

TAVILY_KEY = os.getenv("TAVILY_API_KEY")
BING_KEY = os.getenv("BING_SEARCH_API_KEY")
SERP_KEY = os.getenv("SERPAPI_API_KEY")
G_CSE_KEY = os.getenv("GOOGLE_CSE_KEY")
G_CSE_ID  = os.getenv("GOOGLE_CSE_ID")

def search(query: str, top_k: int = 5, recency_days: int | None = 730) -> List[Dict]:
    """
    Unified web.search interface.
    Prefer Tavily if TAVILY_API_KEY present, else fallback to Bing/SerpAPI/Google CSE.
    Return a list of dicts: { 'title':..., 'url':..., 'snippet':..., 'published':... }
    """
    if TAVILY_KEY:
        # Tavily search
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_KEY, "query": query, "max_results": top_k, "include_answer": False}
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content"), "published": r.get("published_time")}
            for r in data.get("results", [])
        ]
    # Implement other providers similarly (Bing, SerpAPI, Google CSE) if keys exist
    raise RuntimeError("No search provider configured. Set TAVILY_API_KEY or another provider key.")

def fetch(url: str, timeout: int = 15) -> Dict:
    r = requests.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    # Optional: strip HTML to text, pull <title>, <meta name='date' ...>
    return {"url": url, "status": r.status_code, "text": r.text}
```

```python
# app/tools/links.py
import requests

def check(urls, timeout=15, max_redirects=3):
    out = []
    for u in urls:
        try:
            r = requests.head(u, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400 or r.is_permanent_redirect:
                r = requests.get(u, timeout=timeout, allow_redirects=True)
            ok = 200 <= r.status_code < 300
            out.append({"url": u, "ok": ok, "status": r.status_code})
        except Exception as e:
            out.append({"url": u, "ok": False, "error": str(e)})
    return out
```

```python
# app/main.py (snippets only)
from langgraph.graph import StateGraph, END
from tools import web, links
from tenacity import retry, stop_after_attempt, wait_exponential
# ... pydantic schemas and other nodes omitted for brevity

def content_expert_write(state: RunState):
    spec = state.sections[state.current_index]
    # Heuristic: if spec.description or constraints mention "latest", "current", "industry", etc., do a search
    if needs_freshness(spec):
        results = web.search(f"{spec.title} data science latest 2024 2025", top_k=5)
        fetched  = [web.fetch(r["url"]) for r in results[:3]]
        # Package top snippets/urls into the ContentExpert tool context
        state.current_draft = ask_content_expert(state, extra_context={"web_results": results})
    else:
        state.current_draft = ask_content_expert(state)
    return state

def alpha_student_review(state: RunState):
    url_list = extract_urls(state.current_draft.content_md)
    link_report = links.check(url_list, timeout=int(os.getenv("HTTP_TIMEOUT_SECONDS", "15")))
    state.alpha_review = ask_alpha_student(state.current_draft, link_report)
    return state
```

---

## 15) Testing Strategy

Unit:

* Template parsing → section order.
* Link checker status handling (200, 301/302→200, 404).
* Reference normalization & de-duplication.
* Freshness detector (`needs_freshness`) logic.

Integration:

* Golden file week with and without web lookups.
* Simulated revision loops (EducationExpert/AlphaStudent rejections).
* Search provider fallback if primary key absent.

Smoke:

* `WEEK_NUMBER=1`; creates all section files and final weekly file.

---

## 16) Operational Notes

* Idempotency: overwrite section files only on re-approval; optionally keep `.bak`.
* Determinism: keep prompts stable; set `SEED` where supported; allow slightly higher creativity for ContentExpert only.
* Logging: `./run_logs/week{#}.jsonl` with node transitions, approvals, link checks, and which web sources were used.
* Missing inputs: halt with clear error; do not emit partial weekly output.

---

## 17) CLI Usage

```
export WEEK_NUMBER=7
# Choose a web provider (example: Tavily)
export TAVILY_API_KEY=sk-...
python -m app.main
```

Optional flags:

* `--sections ./config/sections.json`
* `--model-map ./config/models.yaml`
* `--dry-run`

---

## 18) Acceptance Checklist

* `.secrets` present; env loaded; at least one **web provider key** configured.
* `./input/*.docx` and `guidelines.md` readable.
* All sections approved by EducationExpert and AlphaStudent.
* Freshness enforced where relevant (≤ 2 years).
* `./temporal_output/*.md` exist; `./weekly_content/Week#.md` generated.
* Logs include web sources and link check summary.

---

### FAQ: Do we need a Google API to access the internet?

* **No, not strictly.** LangGraph has no built-in browser; you must plug in a tool. You can choose:

  * **Tavily API** (simple, focused on LLM retrieval quality)
  * **Bing Web Search API**
  * **SerpAPI** (wraps multiple engines)
  * **Google Programmable Search Engine (CSE)** (requires `GOOGLE_CSE_KEY` + `GOOGLE_CSE_ID`)
* Any of these work. We’ve specified a **unified `web.search` interface** so you can swap providers without changing agent logic.



