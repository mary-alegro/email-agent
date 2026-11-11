"""Shared test setup.

DATABASE_URL is pointed at a throwaway SQLite file *before* anything under `src` is
imported, so the module-level engine used by the REST app, the MCP server and the
pipeline all hit the test database. Each test starts from a freshly seeded ERP. LLM
settings are pinned too, so results don't depend on a local .env.
"""

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="sc-agent-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
# Pin settings so tests never depend on a developer's .env (environment variables win over
# .env). The keys are deliberately fake: a test that reached a real API would get a 401.
os.environ.update(
    {
        "LLM_PROVIDER": "anthropic",
        "LLM_MODEL": "",
        "LLM_EFFORT": "",
        "ANTHROPIC_API_KEY": "test-not-a-real-key",
        "OPENAI_API_KEY": "test-not-a-real-key",
        "ERP_MCP_URL": "",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from src.database.engine import engine, reset_db  # noqa: E402
from src.database.seed import seed  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db(engine)
    with Session(engine) as session:
        seed(session)
    yield


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def email_texts() -> dict[str, str]:
    """Example emails keyed by their 2-digit prefix: {'01': '...', ...}."""
    return {f.name[:2]: f.read_text() for f in sorted(FIXTURES.glob("*.txt"))}
