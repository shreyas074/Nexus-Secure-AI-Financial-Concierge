import os

os.environ.setdefault("SERVICE_NAME", "wealth_server")
from wealth_server.server import build_server  # noqa: E402

if __name__ == "__main__":
    build_server().run(transport="streamable-http")
