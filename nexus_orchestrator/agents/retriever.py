"""Agent 2 - Retriever: build ONE grounded context from live MCP data + the knowledge base.

Rules enforced here (spec 3.3, 4):
  * structured data comes only through the MCP gateway (and is sanitised there)
  * every retrieved document chunk is sanitised; injected chunks are quarantined, never used
  * if unstructured text contradicts live structured data, structured wins AND the conflict is surfaced
"""
from errors import NexusError
from guardrails import mask_identifier, redact_for_logging, sanitize_free_text
from errors import PromptInjectionDetected
from knowledge_base import parse_asserts
from logging_config import get_logger, trace
from nexus_orchestrator import llm
from nexus_orchestrator.rules import infer_scenario_tag

logger = get_logger("agent.retriever")
BANK, INS, WEALTH = "banking_server", "insurance_server", "wealth_server"


class Retriever:
    def __init__(self, gateway, index):
        self.gateway = gateway
        self.index = index

    @trace(logger, redact=redact_for_logging)
    def run(self, intake):
        ctx = {"facts": {}, "records": {}, "chunks": [], "conflicts": [], "agreements": [],
               "quarantined": [], "missing_context": [], "summary": ""}
        line = intake.product_line.value
        ran = []
        if line in ("banking", "cross_product") and (intake.account_id or line == "banking"):
            self._banking(intake, ctx)
            ran.append("banking")
        if line in ("insurance", "cross_product") and (intake.policy_id or intake.claim_id or line == "insurance"):
            self._insurance(intake, ctx)
            ran.append("insurance")
        if line in ("wealth", "cross_product") and (intake.portfolio_id or intake.product_id or line == "wealth"):
            self._wealth(intake, ctx)
            ran.append("wealth")
        self._unstructured(intake, ctx)
        self._reconcile(ctx)
        ctx["quarantined"] += self.gateway.drain_quarantine()
        ctx["sources"] = ran
        ctx["summary"] = self._summarise(ctx)
        return ctx

    # ------------------------------------------------------------- structured (live MCP)
    def _banking(self, intake, ctx):
        f = ctx["facts"]
        linked = self.gateway.call(BANK, "get_linked_accounts", customer_id=intake.customer_id,
                                   caller_scope="customer_self")
        refs = [a["account_ref"] for a in linked["accounts"]]
        f["accounts.linked_count"] = linked["count"]
        if not intake.account_id:
            ctx["missing_context"].append("account_id")
            return
        if mask_identifier(intake.account_id) not in refs:  # ownership check using masked refs only
            raise NexusError("That account is not linked to this customer", code="ACCOUNT_NOT_LINKED")
        acct = self.gateway.call(BANK, "get_account_summary", account_id=intake.account_id,
                                 caller_scope="customer_self")
        for key in ("account_ref", "account_type", "status", "balance", "currency", "auto_debit_allowed"):
            f["account." + key.replace("account_", "")] = acct[key]
        if intake.request_type.value == "balance_inquiry":
            tx = self.gateway.call(BANK, "get_transaction_history", account_id=intake.account_id, limit=5)
            ctx["records"]["transactions"] = tx["transactions"]
            f["transactions.recent_count"] = tx["count"]

    def _insurance(self, intake, ctx):
        f = ctx["facts"]
        if intake.policy_id:
            pol = self.gateway.call(INS, "get_policy_details", policy_id=intake.policy_id)
            f["policy.ref"], f["policy.type"], f["policy.status"] = pol["policy_ref"], pol["policy_type"], pol["status"]
            f["policy.premium_amount"], f["policy.premium_frequency"] = pol["premium_amount"], pol["premium_frequency"]
            f["policy.payment_methods"] = pol["payment_methods"]
            tag = infer_scenario_tag(intake.customer_stated_intent, intake.request_type.value)
            if tag:
                cov = self.gateway.call(INS, "check_coverage_clause", policy_id=intake.policy_id, scenario_tag=tag)
                f["policy.scenario_tag"], f["policy.covered"], f["policy.clause"] = tag, cov["covered"], cov["clause"]
                if tag == "auto_debit_payment":
                    f["policy.auto_debit_covered"] = cov["covered"]
        elif intake.request_type.value != "claim_status":
            ctx["missing_context"].append("policy_id")
        if intake.claim_id:
            claim = self.gateway.call(INS, "get_claim_status", claim_id=intake.claim_id)
            f["claim.ref"], f["claim.status"], f["claim.last_update"] = claim["claim_id"], claim["status"], claim["last_update"]

    def _wealth(self, intake, ctx):
        f = ctx["facts"]
        if intake.portfolio_id:
            pf = self.gateway.call(WEALTH, "get_portfolio_summary", portfolio_id=intake.portfolio_id,
                                   caller_scope="customer_self")
            f["portfolio.ref"], f["portfolio.risk_profile"] = pf["portfolio_ref"], pf["risk_profile"]
            f["portfolio.total_value"], f["portfolio.currency"] = pf["total_value"], pf["currency"]
        else:
            ctx["missing_context"].append("portfolio_id")
        if intake.product_id and f.get("portfolio.risk_profile"):
            suit = self.gateway.call(WEALTH, "check_product_suitability", product_id=intake.product_id,
                                     risk_profile=f["portfolio.risk_profile"])
            f["product.name"], f["product.suitable"] = suit["product_name"], suit["suitable"]

    # ------------------------------------------------------------- unstructured (vector index)
    def _unstructured(self, intake, ctx):
        line = intake.product_line.value
        lines = None if line == "cross_product" else {line}
        for hit in self.index.search(intake.customer_stated_intent, lines, k=5):
            try:
                text = sanitize_free_text(hit["text"], max_len=2000)  # retrieved docs are untrusted input
            except PromptInjectionDetected as exc:
                ctx["quarantined"].append({"source": hit["chunk_id"], "signature": exc.details.get("signature")})
                logger.warning("quarantined retrieved chunk",
                               extra={"fields": {"chunk_id": hit["chunk_id"], "signature": exc.details.get("signature")}})
                continue
            ctx["chunks"].append(dict(hit, text=text))

    # ------------------------------------------------------------- reconciliation
    def _reconcile(self, ctx):
        f = ctx["facts"]
        parts = [f[k] for k in ("account.auto_debit_allowed", "policy.auto_debit_covered") if f.get(k) is not None]
        structured = {"auto_debit_permitted": all(parts)} if parts else {}
        seen = set()
        for chunk in ctx["chunks"]:
            for topic, claimed in parse_asserts(chunk):
                if topic not in structured or (topic, chunk["doc_id"]) in seen:
                    continue
                seen.add((topic, chunk["doc_id"]))
                if claimed == structured[topic]:
                    ctx["agreements"].append({"topic": topic, "doc_id": chunk["doc_id"], "value": claimed})
                else:
                    why = []
                    if f.get("account.auto_debit_allowed") is False:
                        why.append(f"account {f.get('account.ref')} is {f.get('account.status')} and does not allow auto-debit")
                    if f.get("policy.auto_debit_covered") is False:
                        why.append("the policy does not list auto-debit as covered")
                    ctx["conflicts"].append({"topic": topic, "doc_id": chunk["doc_id"], "unstructured_says": claimed,
                                             "structured_says": structured[topic], "detail": "; ".join(why),
                                             "resolution": "structured_live_data_wins"})

    def _summarise(self, ctx):
        lines = [f"- {k}: {v}" for k, v in ctx["facts"].items()]
        lines += [f"- CONFLICT on {c['topic']}: document {c['doc_id']} says {c['unstructured_says']}, live data says "
                  f"{c['structured_says']} ({c['detail']}). Live data is authoritative." for c in ctx["conflicts"]]
        lines += [f"- document {c['doc_id']}: {c['text'][:160]}" for c in ctx["chunks"][:3]]
        text = "\n".join(lines) or "- (no context found)"
        if llm.llm_enabled():
            text = llm.run_agent(
                role="Context Retriever", goal="Condense retrieved facts into a faithful context summary",
                backstory="You never add facts that are not in the material you are given.",
                prompt=f"Rewrite these notes as a compact bullet summary without adding anything:\n{text}",
                expected_output="A bullet list")
        return text
