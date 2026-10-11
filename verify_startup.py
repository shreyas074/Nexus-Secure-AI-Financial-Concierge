"""Spec section 6 / rubric 'System startup & process evidence':
start all four MCP servers + the orchestrator as INDEPENDENT local processes, connect a LIVE MCP client to
each over streamable HTTP, exercise them, then kill insurance_server and prove Stress Test 4 against real
processes. Writes everything to evidence/.

    python verify_startup.py              # full run (also runs the pytest suite at the end)
    python verify_startup.py --skip-pytest

Needs the real packages:  pip install -r requirements.txt
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence"
EXPECTED = {  # server -> (#tools, #resource templates, #prompts)
    "banking_server": (3, 1, 0), "insurance_server": (3, 1, 0), "wealth_server": (3, 0, 0),
    "concierge_ops_server": (4, 2, 2), "nexus_orchestrator": (1, 0, 0),
}
SHOWCASE = ("Can I set up auto-debit for my insurance premium from my savings account, and is auto-debit "
            "actually covered under my policy's payment terms?")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append({"check": label, "passed": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    return ok


async def with_session(name, operation, timeout=240):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        async with streamablehttp_client(config.server_url(name)) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await operation(session)

    return await asyncio.wait_for(go(), timeout)


async def list_all(session):
    tools, templates, prompts = (await session.list_tools(), await session.list_resource_templates(),
                                 await session.list_prompts())
    return {"tools": sorted(t.name for t in tools.tools),
            "resources": sorted(str(r.uriTemplate) for r in templates.resourceTemplates),
            "prompts": sorted(p.name for p in prompts.prompts)}


def probe(name):
    return asyncio.run(with_session(name, list_all, timeout=10))


def call(name, tool, arguments, timeout=240):
    async def op(session):
        res = await session.call_tool(tool, arguments)
        return json.loads(res.content[0].text)
    return asyncio.run(with_session(name, op, timeout))


def start(name, env):
    out = open(EVIDENCE / f"{name}.stdout.txt", "w")
    proc_env = dict(env, SERVICE_NAME=name, NEXUS_LOG_FILE=str(ROOT / "logs" / f"{name}.jsonl"), NEXUS_LOG_STDERR="0")
    return subprocess.Popen([sys.executable, "-m", name], cwd=ROOT, env=proc_env, stdout=out, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-pytest", action="store_true")
    args = parser.parse_args()
    EVIDENCE.mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    env = dict(os.environ, NEXUS_DB=str(ROOT / "meridian.db"), NEXUS_GATEWAY="http", NEXUS_LLM="offline")

    print("Preparing database and knowledge index ...")
    subprocess.run([sys.executable, "database.py", "--seed"], cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "build_index.py"], cwd=ROOT, env=env, check=True)

    procs = {}
    try:
        print("\nStarting 5 independent processes ...")
        for name in EXPECTED:
            procs[name] = start(name, env)
        for name in EXPECTED:
            ready = False
            for _ in range(60):
                if procs[name].poll() is not None:
                    break
                try:
                    probe(name)
                    ready = True
                    break
                except Exception:
                    time.sleep(1)
            check(f"{name} started as its own process and answers a live MCP client", ready,
                  f"pid {procs[name].pid}, {config.server_url(name)}")

        print("\nRegistered primitives (queried over the wire):")
        listing = {}
        for name, (n_tools, n_res, n_prompts) in EXPECTED.items():
            try:
                listing[name] = probe(name)
                got = (len(listing[name]["tools"]), len(listing[name]["resources"]), len(listing[name]["prompts"]))
                check(f"{name}: {n_tools} tools / {n_res} resources / {n_prompts} prompts", got == (n_tools, n_res, n_prompts), str(listing[name]))
            except Exception as exc:
                check(f"{name} listing", False, repr(exc))
        backend = [n for n in EXPECTED if n != "nexus_orchestrator"]
        check("13 tools, 4 resources, 2 prompts across the four backend servers",
              sum(len(listing[n]["tools"]) for n in backend if n in listing) == 13
              and sum(len(listing[n]["resources"]) for n in backend if n in listing) == 4
              and sum(len(listing[n]["prompts"]) for n in backend if n in listing) == 2)

        print("\nLive tool calls:")
        acct = call("banking_server", "get_account_summary", {"account_id": "ACC-20077", "caller_scope": "customer_self"})
        check("banking_server returns a masked account over HTTP", acct["ok"] and acct["data"]["account_ref"] == "ACC-****0077")
        wrong = call("insurance_server", "get_policy_details", {"policy_id": "bad"})
        check("insurance_server returns a typed error for a bad id", wrong["error"]["code"] == "INVALID_ID_FORMAT")
        esc = call("concierge_ops_server", "run_escalation_check",
                   {"customer_id": "CUS-20077", "request_category": "financial_hardship", "draft_confidence": 0.99})
        check("hard escalation fires over HTTP even at confidence 0.99", esc["data"]["escalate"] and esc["data"]["hard_rule_applied"])

        print("\nShowcase Scenario through the live orchestrator process (first call loads the embedding model) ...")
        arguments = {"customer_id": "CUS-20077", "message": SHOWCASE, "account_id": "ACC-20077", "policy_id": "POL-IN-30091"}
        result = call("nexus_orchestrator", "handle_customer_request", arguments)
        check("Showcase resolved without a human via 5 processes", result.get("status") == "resolved",
              f"confidence={result.get('confidence')}")
        check("all five agent roles ran", [s["agent"] for s in result.get("steps", [])] ==
              ["classifier", "retriever", "drafter", "validator", "escalation"])
        evidence_showcase = result

        print("\nStress Test 4 against REAL processes: killing insurance_server ...")
        procs["insurance_server"].terminate()
        procs["insurance_server"].wait(timeout=15)
        down = call("nexus_orchestrator", "handle_customer_request", arguments)
        check("orchestrator returns typed SERVER_UNAVAILABLE (no crash, no guessed answer)",
              down.get("status") == "error" and down["error"]["code"] == "SERVER_UNAVAILABLE" and not down.get("response"),
              json.dumps(down.get("error")))
        still = call("banking_server", "get_account_summary", {"account_id": "ACC-20077", "caller_scope": "customer_self"})
        check("other servers keep serving while one is down", still["ok"])

        pytest_summary = "skipped"
        if not args.skip_pytest:
            print("\nRunning the pytest suite ...")
            proc = subprocess.run([sys.executable, "-m", "pytest", "-v"], cwd=ROOT, capture_output=True, text=True,
                                  env=dict(os.environ, NEXUS_INDEX=os.getenv("NEXUS_INDEX", "simple")))
            (EVIDENCE / "pytest_output.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            pytest_summary = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
            check("pytest suite executed and passed", proc.returncode == 0, pytest_summary)

        (EVIDENCE / "startup_evidence.json").write_text(json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": sys.version.split()[0], "checks": RESULTS, "pytest": pytest_summary,
            "pids": {n: p.pid for n, p in procs.items()}, "primitives": listing, "showcase": evidence_showcase,
            "stress_test_4_live": down,
        }, indent=2, default=str), encoding="utf-8")
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()

    failed = [r for r in RESULTS if not r["passed"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed. Evidence written to {EVIDENCE}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
