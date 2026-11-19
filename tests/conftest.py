import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="flextrainer-test-")
os.environ["DATA_DIR"] = _tmp
os.environ.setdefault("UPDATE_CHECK", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture(scope="session")
def auth(client):
    """Signed-in client (default login, password changed as required)."""
    r = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/account/password")
    r = client.post(
        "/account/password",
        data={"current": "admin", "new": "s3cret-pass", "confirm": "s3cret-pass"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client
