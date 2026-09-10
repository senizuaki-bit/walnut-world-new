"""Run unchanged main FastAPI voice route with test-only context/provider I/O.

Uses no real credentials, microphone, database, Sandbox, or paid Provider.
Only the injected external dependencies are fakes; the public route and codec
are the backend's actual implementations. Bind to loopback for Godot testing.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "walnut-world-backend"
sys.path[:0] = [str(BACKEND), str(BACKEND / "src"), str(ROOT / "agent/python")]

from tests.unit.test_dingdang_voice import ContextReader, ProviderSocket
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from yaya_agent_runtime.adapters.doubao_realtime import DoubaoRealtimeAdapter, DoubaoRealtimeConfig
import uvicorn

app = create_app(Settings.for_test(
    contract_path=DEFAULT_CONTRACT_PATH,
    contract_release_path=BACKEND / "contract-release.json",
))
original_lifespan = app.router.lifespan_context

@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        application.state.dingdang_voice_context = ContextReader()
        def factory():
            provider = ProviderSocket()
            return DoubaoRealtimeAdapter(DoubaoRealtimeConfig(api_key="offline-test-key"), provider.connect)
        application.state.dingdang_voice_factory = factory
        yield

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8790, access_log=False)
