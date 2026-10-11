# Nexus - Architecture

## 1. The one-sentence design

**Anything that must always be true is plain, deterministic code. A language model may help, but it can never be
the thing that makes a safety decision.** That is why the demo is reproducible, the tests are stable, and every
"can the model bypass this?" question has the same answer: no, the gate is not the model.

## 2. Components and processes

```
                       +---------------------------------------------+
 customer message ---> |  nexus_orchestrator  (process, :8005)       |
 (MCP client / CLI)    |  Classifier -> Retriever -> Drafter ->      |
                       |  Validator -> Escalation   (pipeline.py)    |
                       |        |                 |                  |
                       |   gateway.py (MCP client)  knowledge_base   |
                       +--------|-----------------|------------------+
              streamable HTTP   |                 |  ChromaDB (local folder)
        +-----------+-----------+----------+      |  policy_docs/ product_docs/ faqs/ templates/
        v           v           v          v
  banking_server insurance_server wealth_server concierge_ops_server      4 independent processes
     :8001          :8002          :8003          :8004                    (13 tools, 4 resources, 2 prompts)
        \           |             |            /
         +----------+-------------+-----------+
                    meridian.db  (SQLite, WAL)  - every server opens it through database.connect(<server>),
                    which installs a per-server SQLite authorizer (see section 5)
```

* Each backend server is `python -m <server>`: `tools.py` holds the logic as plain functions, `server.py` registers them with `FastMCP`.
  Keeping logic separate from registration is what lets the tests exercise every tool directly *and* through the real MCP protocol.
* The orchestrator is both an MCP **client** (of the four servers) and an MCP **server** (one tool, `handle_customer_request`), so it too is a standalone process that answers a live client.
* The gateway has two transports and one code path (always a real `mcp.ClientSession`): `memory` (servers run in-process over in-memory streams; used by the demo and tests, so no processes are needed) and `http` (the five real processes).

## 3. One request, step by step (the Showcase Scenario)

Arjun (`CUS-20077`): *"Can I set up auto-debit for my insurance premium from my savings account, and is auto-debit actually covered under my policy's payment terms?"*

| # | Step | What happens | Where |
|---|---|---|---|
| 1 | **Classifier** | Sanitise text; rules pick `product_line=cross_product` (it mentions both banking and insurance words), `request_type=auto_debit_setup`, `urgency=routine`, `escalation_category=none`; validate into `IntakeRequest`. Invalid -> typed `IntakeValidationError`, nothing else runs. | `agents/classifier.py`, `rules.py`, `schemas.py` |
| 1b | Identity gate | `get_customer_kyc_status` (ops server). Unknown customer -> `CUSTOMER_NOT_FOUND`; this is the only call made. Then `customer://CUS-20077/profile` for the name. | `pipeline.py` |
| 2 | **Retriever** | Live: `get_linked_accounts` (ownership check on masked refs), `get_account_summary`, `get_policy_details`, `check_coverage_clause("auto_debit_payment")`. Unstructured: vector search of the knowledge base filtered by metadata. Each chunk is sanitised; the poisoned FAQ chunk is **quarantined**. Reconcile: the FAQ and the payment-terms doc both assert `auto_debit_permitted=true`; live data says account allows it and clause 4.2 covers it -> agreement, no conflict. | `agents/retriever.py` |
| 3 | **Drafter** | Builds each sentence from a retrieved fact and records a *claim* (which fact/document backs it). | `agents/drafter.py` |
| 4 | **Validator** | Re-checks every claim against the context, runs the output filter, checks that any conflict was acknowledged, computes confidence (1.0 here). | `agents/validator.py` |
| 5 | **Escalation** | MCP `run_escalation_check(customer, "none", 1.0)` -> `escalate=false`. | `agents/escalation.py`, ops server |
| 6 | Act | `send_customer_notification` (sanitise, KYC check, log) then `write_audit_log`. Status: `resolved`, no human involved. | `pipeline.py` |

## 4. The three load-bearing mechanisms

### 4.1 Hard escalation cannot be bypassed by rephrasing (rubric: "provably unbypassable")
`run_escalation_check` runs these in order and returns at the first match:

1. `request_category` in {financial_hardship, safeguarding_concern, compliance_override_request} -> **escalate**, regardless of `draft_confidence`.
2. An **active flag** of a hard type on the customer's record (`escalation_flags` table) -> **escalate**.
3. `draft_confidence < 0.75` -> escalate (confidence-based).
4. Otherwise resolve.

Why rephrasing does not help: `escalation_category` is extracted at **intake**, before any drafting, by a regex lexicon that
covers many phrasings ("lost my job", "laid off", "redundancy", "money's been tight", "struggling to", "pause my premium", "skip the KYC", ...).
Rule 2 means that even if the lexicon misses, a flag already on the record still escalates. The pipeline reads the **tool's** answer; in Claude mode the Escalation
agent may add an advisory note but its text is never consulted for the decision. In Claude mode the classifier model can only **raise** the category (`merge_escalation`).
Proofs: `tests/test_tools.py` (3 categories x 4 confidence values), `test_pipeline.py::test_hard_escalation_beats_a_perfect_validator` (Validator forced to 0.99), the 4 rephrasing variants, and the flag-only case.

### 4.2 Structured live data is authoritative, conflicts are surfaced (rubric: reconciliation)
Knowledge-base documents carry an `asserts:` line (e.g. `auto_debit_permitted=true`). The Retriever derives the same topic from live MCP facts
(`account.auto_debit_allowed AND policy.auto_debit_covered`). If they differ it records a conflict with `resolution: structured_live_data_wins`; the Drafter
must state the live position; the Validator receives `conflicts_surfaced` and penalises a draft that ignores it (-0.30). For Meera (`CUS-20078`) the FAQ says auto-debit is
available but her account is `restricted`, so the reply says it cannot be set up and why. Nothing is cached: `test_live_data_really_is_live` changes the database between two requests.

### 4.3 Confidence is a formula, not a vibe
`confidence = supported_claims / total_claims  - 0.30 (unacknowledged conflict)  - 0.50 (output-filter violation)`, floored at 0.
A draft with no verifiable claims scores 0, which is why a vague message escalates by confidence. The LLM, if enabled, can only lower the score.

## 5. Security model

| Requirement | Mechanism | Evidence |
|---|---|---|
| Scoped field visibility | `minimize_account_fields/portfolio_fields(row, caller_scope)`; `customer_self` vs `agent_internal`; invalid scope -> typed error | `test_guardrails`, `test_tools` |
| Which server reaches which data | **`sqlite3.set_authorizer`** per server in `database.connect()`: reads/writes allowed only on that server's tables; `ATTACH`, DDL, pragmas denied. Structural: a buggy query in `insurance_server` cannot read `accounts`. Works for one shared file or four. | `test_database.py` (13 forbidden table pairs, write protection, ATTACH) |
| Masking | `mask_identifier` -> `ACC-****0077`; every tool/resource returns masked refs; `no_unmasked()` asserted on **every** tool result | `test_tools.py` |
| ID format validation | `validate_id` for CUS/ACC/POL/PORT/PROD/CLM on every argument; typed `INVALID_ID_FORMAT` | `test_guardrails`, `test_tools` |
| Hard escalation gate | section 4.1 | |
| Audit on writes | `audit_log` table; `write_audit_log`; `send_customer_notification` audits itself (sent **and** blocked outcomes); pipeline audits each outcome | `test_every_write_is_audited` |
| Sanitisation / injection | `sanitize_free_text`: scan, strip HTML (incl. comments/script), re-scan (catches tag-split signatures), length limit. Applied to customer text, notification bodies, **every retrieved chunk** and **every string returned by a tool** (gateway) | demo 6, `test_retrieval`, `test_guardrails` |
| Output filtering | `check_output`: unmasked IDs, numbers not present in the grounded context, promise/guarantee wording -> draft never sent, escalates | `test_output_filter_blocks_leaky_drafts` |
| PII-safe logs | `redact_for_logging` inside `@trace`; logs verified free of IDs/names/emails in the demo | demo 9 |
| MCP-only backend access | the orchestrator package contains no `database`/`sqlite3` import (a test greps for it) | `test_orchestrator_never_touches_the_database_directly` |

## 6. Design decisions and honest limitations (good debrief material)

1. **Why plain Python between the agents instead of one big CrewAI crew?** Gates (sanitise, MCP calls, escalation) must run *between* roles. In Claude mode each role is its own CrewAI `Agent`+`Task` in a tiny `Crew`; Python orchestrates. Chaining all five inside one crew would hide the gates from us.
2. **Why the deterministic offline mode?** Reproducible demo, tests that do not flake, no API cost, and it proves the safety story does not depend on a model. Claude mode adds richer language only.
3. **Account/policy IDs come from the app session (`context`), not from MCP.** The brief makes masking a hard requirement, so MCP can never hand back a usable full ID. The channel (like a banking app) already knows which account the customer has open. Ownership is verified for accounts (`get_linked_accounts` returns masked refs; the Retriever checks the requested one is among them). *Limitation:* policy and portfolio ownership are not checked because the specified tools give no masked "list by customer" equivalent; production tools should verify `customer_id` themselves.
4. **Regex injection defence is a signature filter.** It reliably stops the demo attacks and obvious variants, but a determined attacker can paraphrase around signatures. Defence in depth is the point: untrusted text is also fenced in prompts, the Drafter only uses structured facts, and the output filter plus the deterministic gates sit behind it.
5. **The lexicon can miss novel phrasings.** Mitigations: flags on the record (rule 2), LLM can raise the category, and unclear requests score low confidence and escalate anyway. Failing "open" to a human is the intended direction.
6. **Search quality.** Chroma + MiniLM is real semantic search. The `simple` fallback is lexical TF-IDF (used by tests / when Chroma is absent) - it is an explicit, warned fallback, not the default.
7. **Request correlation.** `request_id` is carried in a contextvar through orchestrator-side logs. Server processes are separate, so their log lines have `request_id: "-"` (they can be matched by time and `call_id`). Propagating it over MCP metadata would be the next step.
8. **No server-side session state.** Every gateway call opens a fresh MCP session: slower than a pooled connection, but simple and easy to reason about; fine for this scale.
9. **Notifications are simulated**: "sent" means logged to `interaction_log` and audited, nothing leaves the machine.

## 7. Debrief cheat-sheet

* *"Show me the hard rule cannot be skipped."* `run_escalation_check` in `concierge_ops_server/tools.py` (first lines of the function) and `test_hard_escalation_beats_a_perfect_validator`.
* *"What if the model says confidence is high?"* The pipeline never lets confidence influence rules 1-2; and the model can only lower confidence.
* *"How do you know insurance cannot see accounts?"* Run `database.fetch_all("insurance_server", "SELECT * FROM accounts")` - SQLite refuses (`test_database.py`).
* *"What if the knowledge base is poisoned?"* `FAQ_AUTO_DEBIT_FEES` is. Demo section 6 / `test_injected_chunk_is_quarantined_not_used`.
* *"What happens if a server dies?"* `ServerUnavailableError` -> `status: error`, no response text (`test_st4_any_server_offline...`; live version in `verify_startup.py`).
* *"Why are some numbers rejected in drafts?"* The output filter requires every number to appear in the grounded context - invented figures cannot reach a customer.
* *"What would you do next?"* Ownership checks for policies/portfolios, `request_id` over MCP metadata, a learned classifier alongside the lexicon, pooled MCP sessions.
