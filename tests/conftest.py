import os
import socket
import threading
import time

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
os.environ.update({
    "MOCK_LLM": "1",
    "MOCK_TAVILY": "1",
    "MOCK_DELAY": "0",
    "PUBLIC_BASE_URL": f"http://127.0.0.1:{PORT}",
    "CACHE_DIR": os.path.join(os.path.dirname(__file__), ".cache"),
})


@pytest.fixture(scope="session")
def server_url():
    import uvicorn

    from app.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server didn't start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}"
    server.should_exit = True
    thread.join(timeout=10)
