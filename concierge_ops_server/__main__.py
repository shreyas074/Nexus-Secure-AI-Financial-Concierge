import os

os.environ.setdefault("SERVICE_NAME", "concierge_ops_server")
from concierge_ops_server.server import build_server  # noqa: E402

if __name__ == "__main__":
    build_server().run(transport="streamable-http")
