"""Shared fixtures. Every test gets its own freshly seeded database in a temp folder."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NEXUS_INDEX", "simple")   # offline index for speed; test_retrieval.py also tests chroma
os.environ["NEXUS_LLM"] = "offline"              # tests are deterministic: no model calls
os.environ["NEXUS_LOG_STDERR"] = "0"             # keep pytest output readable (logs still captured by tests)
os.environ["NEXUS_GATEWAY"] = "memory"

import pytest  # noqa: E402

import database  # noqa: E402
from knowledge_base import KnowledgeIndex  # noqa: E402
from nexus_orchestrator.gateway import Gateway  # noqa: E402
from nexus_orchestrator.pipeline import NexusPipeline  # noqa: E402

@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_DB", str(tmp_path / "meridian.db"))
    database.seed()
    yield


@pytest.fixture(scope="session")
def index():
    return KnowledgeIndex()


@pytest.fixture
def gateway():
    return Gateway(mode="memory")


@pytest.fixture
def pipeline(gateway, index):
    return NexusPipeline(gateway, index)
