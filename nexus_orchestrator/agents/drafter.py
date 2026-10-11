"""Agent 3 - Drafting Agent: propose a reply using ONLY the grounded context.

The deterministic composer builds every sentence from a retrieved fact and records a `claim` (which fact or
document backs it). The Validator later re-checks each claim against the context. In Claude mode the text is
written by an LLM, but the claims and all output filters still apply.
"""
import json

from guardrails import redact_for_logging
from logging_config import get_logger, trace
from nexus_orchestrator import llm

logger = get_logger("agent.drafter")

COVERAGE_WORDS = {"auto_debit_payment": "auto-debit premium payments", "theft": "theft", "flood_damage": "flood damage"}


def _money(currency, amount):
    return f"{currency} {amount:,.0f}"


@trace(logger, redact=redact_for_logging)
def compose(intake, ctx):
    """Return (sentences, claims). Pure function: easy to explain and to unit-test."""
    f, sentences, claims = ctx["facts"], [], []

    def say(text, claim=None):
        sentences.append(text)
        if claim:
            claims.append(claim)

    def fact(key):
        return {"source": "fact", "key": key, "value": f.get(key)}

    rtype = intake.request_type.value
    if rtype == "auto_debit_setup" and ("account.auto_debit_allowed" in f or "policy.auto_debit_covered" in f):
        if "account.auto_debit_allowed" in f:
            if f["account.auto_debit_allowed"]:
                say(f"Your {f['account.type']} account {f['account.ref']} is {f['account.status']} and eligible for auto-debit.",
                    fact("account.auto_debit_allowed"))
            else:
                say(f"Your {f['account.type']} account {f['account.ref']} is currently {f['account.status']} and has "
                    f"debit restrictions, so auto-debit cannot be set up from it right now.",
                    fact("account.auto_debit_allowed"))
        if f.get("policy.auto_debit_covered") is True:
            say(f"Your policy {f['policy.ref']} payment terms (clause {f['policy.clause']}) allow the "
                f"{f['policy.premium_frequency']} premium of {_money('INR', f['policy.premium_amount'])} to be paid by auto-debit.",
                fact("policy.auto_debit_covered"))
        elif f.get("policy.auto_debit_covered") is False:
            say(f"Your policy {f['policy.ref']} payment terms do not list auto-debit as a payment option.",
                fact("policy.auto_debit_covered"))
        elif "policy.auto_debit_covered" in f:
            say(f"I could not find an auto-debit clause on policy {f['policy.ref']}.", fact("policy.auto_debit_covered"))
        for c in {c["topic"]: c for c in ctx["conflicts"]}.values():  # one sentence per conflicting topic
            say(f"Our general guidance suggests auto-debit is widely available, but your live account record takes "
                f"priority: {c['detail']}.", {"source": "conflict", "topic": c["topic"]})
        wording = next((c for c in ctx["chunks"] if c["kind"] == "policy_docs" and c["product_line"] == "insurance"), None)
        if wording:
            body = wording["text"][len(wording["section"]):].strip()  # chunk text = heading + body
            say(f"Our policy wording adds: {body.split('. ')[0].strip('. ')}.",
                {"source": "doc", "doc_id": wording["doc_id"]})
        allowed = f.get("account.auto_debit_allowed") and f.get("policy.auto_debit_covered") is not False
        say("If you would like to go ahead, please confirm in the Meridian app and we will set up the mandate."
            if allowed else "Please contact us once the restriction has been resolved and we will be glad to help.")
    elif rtype == "balance_inquiry" and "account.balance" in f:
        say(f"The current balance of your {f['account.type']} account {f['account.ref']} is "
            f"{_money(f['account.currency'], f['account.balance'])}.", fact("account.balance"))
    elif rtype == "balance_inquiry" and "accounts.linked_count" in f and f["accounts.linked_count"]:
        say(f"You have {f['accounts.linked_count']} linked account(s). Which one would you like me to look at?",
            fact("accounts.linked_count"))
    elif rtype == "claim_status" and "claim.status" in f:
        say(f"Claim {f['claim.ref']} is currently {f['claim.status'].replace('_', ' ')} (last updated {f['claim.last_update']}).",
            fact("claim.status"))
    elif rtype == "coverage_question" and "policy.covered" in f:
        word = COVERAGE_WORDS.get(f["policy.scenario_tag"], f["policy.scenario_tag"])
        if f["policy.covered"] is None:
            say(f"Your policy {f['policy.ref']} does not state a clause for {word}.", fact("policy.covered"))
        else:
            say(f"Under clause {f['policy.clause']}, {word} {'is' if f['policy.covered'] else 'is not'} covered "
                f"on your policy {f['policy.ref']}.", fact("policy.covered"))
    elif rtype == "portfolio_query" and "portfolio.total_value" in f:
        say(f"Your portfolio {f['portfolio.ref']} ({f['portfolio.risk_profile']} risk profile) is currently valued at "
            f"{_money(f['portfolio.currency'], f['portfolio.total_value'])}.", fact("portfolio.total_value"))
        if "product.suitable" in f:
            say(f"{f['product.name']} {'is' if f['product.suitable'] else 'is not'} suitable for a "
                f"{f['portfolio.risk_profile']} risk profile.", fact("product.suitable"))
    if not sentences:
        say("Thank you for getting in touch. I want to be sure I send you to the right place - could you tell me "
            "whether this is about your account, your insurance or your investments, and which one?")
    return sentences, claims


class Drafter:
    def __init__(self, gateway):
        self.gateway = gateway

    @trace(logger, redact=redact_for_logging)
    def run(self, intake, ctx, customer_name):
        sentences, claims = compose(intake, ctx)
        text = f"Hi {customer_name}. " + " ".join(sentences)
        used_llm = False
        if llm.llm_enabled():
            prompt = self.gateway.get_prompt("concierge_ops_server", "response_drafting_prompt",
                                             classified_intent=f"{intake.request_type.value}: {intake.customer_stated_intent}",
                                             retrieved_context_summary=ctx["summary"], customer_name=customer_name,
                                             escalation_flag=intake.escalation_category.value)
            text = llm.run_agent(role="Drafting Agent", goal="Write a short, accurate customer reply",
                                 backstory="You only use facts from the context you are given.",
                                 prompt=prompt + "\n\nReply with the message text only.",
                                 expected_output="The customer-facing message").strip()
            used_llm = True
        return {"text": text, "claims": claims, "used_llm": used_llm}
