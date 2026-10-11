import json
import logging
import os
import subprocess
import sys

import pytest

from logging_config import JsonFormatter, get_logger, trace

logger = get_logger("test")


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))  # every line must be valid JSON


def capture():
    handler = Collect()
    logging.getLogger("nexus").addHandler(handler)
    return handler


def test_enter_exit_share_call_id_and_have_timing():
    handler = capture()

    @trace(logger)
    def add(a, b=2):
        return a + b

    assert add(1) == 3
    enter, exit_ = handler.lines[-2:]
    assert enter["event"] == "ENTER" and exit_["event"] == "EXIT"
    assert enter["call_id"] == exit_["call_id"]
    assert enter["args"] == {"a": 1, "b": 2}                       # bound args, defaults applied
    assert enter["level"] == "DEBUG" and exit_["level"] == "DEBUG"
    assert exit_["duration_ms"] >= 0 and exit_["result_preview"] == "3"
    assert enter["function"].endswith("add")


def test_failure_logs_error_with_traceback_and_reraises():
    handler = capture()

    @trace(logger)
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom()
    failed = handler.lines[-1]
    assert failed["event"] == "FAILED" and failed["level"] == "ERROR"
    assert failed["exception_type"] == "ValueError" and "Traceback" in failed["traceback"]
    assert failed["call_id"] == handler.lines[-2]["call_id"] and "duration_ms" in failed


def test_redact_function_is_applied_to_arguments():
    handler = capture()

    @trace(logger, redact=lambda d: {k: "***" for k in d})
    def secret(token):
        return "ok"

    secret("abc")
    assert handler.lines[-2]["args"] == {"token": "***"}


def test_preview_is_truncated():
    handler = capture()

    @trace(logger)
    def big():
        return "x" * 5000

    big()
    assert len(handler.lines[-1]["result_preview"]) < 300 and "truncated" in handler.lines[-1]["result_preview"]


def test_async_functions_are_traced():
    import asyncio
    handler = capture()

    @trace(logger)
    async def double(x):
        return x * 2

    assert asyncio.run(double(4)) == 8
    assert [l["event"] for l in handler.lines[-2:]] == ["ENTER", "EXIT"]


def test_log_level_defaults_to_debug():
    env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
    code = "import logging, logging_config; logging_config.get_logger('x'); print(logging.getLogger('nexus').level)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout.strip()
    assert out == str(logging.DEBUG)


# ---------------------------------------------------------------- coverage: "every Tool, Resource, Prompt and agent step"
def is_traced(fn):
    fn = getattr(fn, "__func__", fn)
    while fn is not None:
        if getattr(getattr(fn, "__code__", None), "co_filename", "").endswith("logging_config.py"):
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


def test_every_tool_resource_prompt_and_agent_step_is_traced():
    from banking_server import tools as bank
    from concierge_ops_server import tools as ops
    from insurance_server import tools as ins
    from nexus_orchestrator.agents import Classifier, Drafter, EscalationAgent, Retriever, Validator
    from nexus_orchestrator.gateway import Gateway
    from nexus_orchestrator.pipeline import NexusPipeline
    from wealth_server import tools as wealth

    required = [
        bank.get_account_summary, bank.get_transaction_history, bank.get_linked_accounts, bank.account_summary_resource,
        ins.get_policy_details, ins.check_coverage_clause, ins.get_claim_status, ins.policy_summary_resource,
        wealth.get_portfolio_summary, wealth.get_investment_product_details, wealth.check_product_suitability,
        ops.get_customer_kyc_status, ops.run_escalation_check, ops.send_customer_notification, ops.write_audit_log,
        ops.customer_profile_resource, ops.knowledge_excerpt_resource,
        ops.request_classification_prompt, ops.response_drafting_prompt,
        Classifier.run, Retriever.run, Drafter.run, Validator.run, EscalationAgent.run, NexusPipeline.handle,
        Gateway.call, Gateway.read_resource, Gateway.get_prompt,
    ]
    untraced = [f.__qualname__ for f in required if not is_traced(f)]
    assert untraced == []


def test_guardrail_and_database_public_functions_are_traced():
    import database
    import guardrails
    for fn in (guardrails.validate_id, guardrails.sanitize_free_text, guardrails.check_output,
               guardrails.minimize_account_fields, guardrails.minimize_portfolio_fields,
               database.fetch_all, database.fetch_one, database.execute, database.seed):
        assert is_traced(fn), fn.__qualname__
