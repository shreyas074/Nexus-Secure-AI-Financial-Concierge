"""python -m nexus_orchestrator              -> serve the orchestrator as an MCP server on :8005
   python -m nexus_orchestrator --ask "..." --customer CUS-20077 --account ACC-20077 --policy POL-IN-30091
                                             -> run one request and print the full trace (add --live for HTTP servers)"""
import argparse
import json
import os

os.environ.setdefault("SERVICE_NAME", "nexus_orchestrator")


def main():
    parser = argparse.ArgumentParser(prog="nexus_orchestrator")
    parser.add_argument("--ask", help="a customer message; omit to start the MCP server")
    parser.add_argument("--customer", default="CUS-20077")
    parser.add_argument("--account", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--portfolio", default="")
    parser.add_argument("--live", action="store_true", help="talk to the HTTP servers on ports 8001-8004")
    args = parser.parse_args()

    if args.ask:
        from nexus_orchestrator.gateway import Gateway
        from nexus_orchestrator.pipeline import NexusPipeline
        if not args.live and not os.path.exists(os.getenv("NEXUS_DB", "meridian.db")):
            raise SystemExit("No database yet - run:  python database.py --seed")  # the orchestrator never touches the DB itself
        pipeline = NexusPipeline(Gateway(mode="http" if args.live else "memory"))
        context = {"account_id": args.account, "policy_id": args.policy, "portfolio_id": args.portfolio}
        print(json.dumps(pipeline.handle(args.ask, args.customer, context), indent=2, default=str))
    else:
        from nexus_orchestrator.server import build_server
        build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
