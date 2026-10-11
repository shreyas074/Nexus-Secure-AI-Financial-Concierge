# Nexus - Unified Financial Services Concierge (Meridian Financial Group)

One front door for banking, insurance and wealth requests. A free-text message is validated into a typed
schema, grounded in live data (via MCP) plus policy/FAQ knowledge (vector search), drafted and fact-checked by
five agents, and escalated to a human by a **deterministic** rule when it must be.

```
customer message
   |
   v
[1 Classifier] -> typed IntakeRequest (pydantic)  --invalid--> typed error, nothing else runs
   |  identity gate (customer must exist) ------------------> CUSTOMER_NOT_FOUND
   v
[2 Retriever]  live MCP data  +  ChromaDB knowledge search  -> reconcile (live data wins, conflict surfaced)
   v                    every retrieved string is sanitised; injected chunks are quarantined
[3 Drafter]    sentences built only from retrieved facts (+ a 'claim' per sentence)
   v
[4 Validator]  re-checks every claim, output filter, conflict check -> confidence
   v
[5 Escalation] MCP tool run_escalation_check:  hard categories -> record flags -> confidence threshold
   v
notify customer + audit_log (both MCP writes)       ... all backend access via 4 MCP servers
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python database.py --seed        # builds meridian.db (WAL) with the demo fixtures
python build_index.py            # builds the ChromaDB index (first run downloads the embedding model)

python run_local_demo.py         # the whole of spec section 8, narrated, ends with a PASS/FAIL table
pytest                           # unit + integration tests (must be RUN, not just written)
python verify_startup.py         # starts 5 real processes, talks to them with a live MCP client, writes evidence/
```

Run the processes yourself (five terminals) and talk to them from Claude Desktop or any MCP client:

```bash
python -m banking_server           # :8001   http://127.0.0.1:8001/mcp
python -m insurance_server         # :8002
python -m wealth_server            # :8003
python -m concierge_ops_server     # :8004
python -m nexus_orchestrator       # :8005   tool: handle_customer_request  (is itself a client of 8001-8004)

python -m nexus_orchestrator --ask "What is my savings balance?" --customer CUS-20077 --account ACC-20077
python -m nexus_orchestrator --ask "..." --live      # same, but against the running HTTP servers
```

Offline mode (default) needs no API key and is fully deterministic.
Claude mode: `pip install -r requirements-llm.txt`, set `ANTHROPIC_API_KEY`, then `NEXUS_LLM=claude python run_local_demo.py`.
In Claude mode each role is a CrewAI Agent that may *refine* classification/drafting/validation; the safety gates
stay deterministic (the model can raise the escalation category, never lower it; it can lower confidence, never raise it).

## Project map

| Path | What it is |
|---|---|
| `logging_config.py` | JSON logging, `@trace(logger, redact=)` -> ENTER / EXIT / FAILED lines, `LOG_LEVEL` (default DEBUG) |
| `guardrails.py` | `validate_id`, `mask_identifier`, `minimize_account_fields`, `minimize_portfolio_fields`, `sanitize_free_text`, `check_output`, `redact_for_logging` |
| `database.py` | schema, seed (`--seed`), per-server **SQLite authorizer** isolation, WAL |
| `knowledge_base.py`, `build_index.py`, `knowledge/` | chunking, ChromaDB (or tiny fallback) index, policy/product/FAQ/template docs |
| `banking_server/ insurance_server/ wealth_server/ concierge_ops_server/` | the 4 MCP servers: 13 tools, 4 resources, 2 prompts (`tools.py` = logic, `server.py` = registration) |
| `nexus_orchestrator/` | `schemas.py` intake, `rules.py` lexicon, `gateway.py` MCP client, `agents/` (5 roles), `pipeline.py`, `server.py` |
| `run_local_demo.py` | spec section 8 demo, self-checking |
| `verify_startup.py` | live multi-process evidence |
| `tests/` | pytest suite |
| `ARCHITECTURE.md` | design, security model, trade-offs, debrief Q&A |

Demo customers: `CUS-20077` Arjun Mehta (showcase), `CUS-20078` Meera (restricted account -> retrieval conflict),
`CUS-20079` Rahul (hardship tests), `CUS-20080` Sunita (active safeguarding flag), `CUS-20081` Vikram (KYC pending).

## Rubric map

| Rubric item | Where to look |
|---|---|
| End-to-end integration (25%) | `NexusPipeline.handle()`; demo sections 7, `test_showcase_scenario_resolves_without_a_human` |
| Structured intake (10%) | `schemas.py`, `agents/classifier.py`, `tests/test_intake.py`, demo 1 and ST1 |
| Reconciled retrieval (15%) | `agents/retriever.py` (`_reconcile`), `tests/test_retrieval.py`, demo 2 and ST2 |
| Multi-agent + escalation (15%) | `agents/`, `run_escalation_check`, `rules.py` lexicon, demo 4-5 and ST3 |
| MCP-only access (15%) | `gateway.py` (only door), `test_orchestrator_never_touches_the_database_directly`, `database.py` authorizer, `tests/test_database.py` |
| Guardrails (10%) | `guardrails.py`, demo section 9, `tests/test_guardrails.py` |
| Startup + process evidence (5%) | `verify_startup.py` -> `evidence/` |
| Demo + debrief (5%) | `run_local_demo.py` narration, `ARCHITECTURE.md` section 7 |

## Logging

Every Tool, Resource, Prompt, agent step, gateway call, DB helper and guardrail is wrapped in `@trace`
(enforced by `tests/test_logging.py`). Exemptions, on purpose: `redact_for_logging` and the tiny masking helpers
it calls (the tracer calls them - tracing them would recurse), and constructors/registration functions.
Logs are JSON, one object per line, to stderr and (if `NEXUS_LOG_FILE` is set) a file. Arguments are redacted:
IDs keep their last four digits, e-mail keeps the domain, phone keeps two digits, names and free text become
placeholders, SQL parameters are logged as a count. Purpose: logs help debug behaviour without becoming a second
copy of customer data (data minimisation).

## Verification status - read before you submit

This package was built and executed in a sandbox **without internet access**, so the real third-party packages
could not be installed there. What that means:

* Executed and passing in the sandbox: all business logic, guardrails, logging, database isolation, intake, retrieval and reconciliation,
  the five-agent pipeline, all four stress tests, and the full demo (34/34 checks) - run against a tiny stand-in
  for `pydantic`, `mcp` and `pytest` that were used only to run the code and are **not included**.
* **Not yet verified - you must run these once with the real packages (spec section 6 requires it):**
  1. `pytest` - the 4 skipped tests are the real-`mcp` registration checks and the ChromaDB check; they run automatically once the packages are installed.
  2. `python verify_startup.py` - real HTTP processes + a live MCP client. Attach `evidence/` to your submission.
  3. `NEXUS_LLM=claude` mode (CrewAI + Claude) - written against CrewAI's public API but never executed. Pin versions and test before the live demo,
     or present offline mode and describe Claude mode as optional.
* If the real `mcp` SDK differs from what the gateway expects (for example the in-memory helper
  `mcp.shared.memory.create_connected_server_and_client_session`), the fix is confined to `Gateway._open()`.
