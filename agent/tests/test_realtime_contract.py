from __future__ import annotations

import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "python"))

from yaya_agent_contracts import (  # noqa: E402
    ContractError,
    ErrorCategory,
    RealtimeAckFrame,
    RealtimeBootstrap,
    RealtimeCheckpoint,
    RealtimeCloseCode,
    RealtimeErrorFrame,
    RealtimeHeartbeatAckFrame,
    RealtimeHeartbeatFrame,
    RealtimeResumeFrame,
    RealtimeSubscribedFrame,
    RealtimeSubscribeFrame,
)


class RealtimeContractTests(unittest.TestCase):
    stream_id = "world:world_demo_001"
    subscription_id = "sub_realtime_00000001"

    def error(self, code: str = "EVENT_SEQUENCE_GAP") -> ContractError:
        if code == "EVENT_SEQUENCE_GAP":
            return ContractError(
                code=code,
                category=ErrorCategory.CONCURRENCY,
                retryable=True,
                user_message_key="event.resync_required",
                stage="REALTIME_STREAM",
            )
        return ContractError(
            code=code,
            category=ErrorCategory.AUTHENTICATION,
            retryable=False,
            user_message_key="auth.login_required",
            stage="REALTIME_HANDSHAKE",
        )

    def test_bootstrap_is_tls_only_and_versioned(self) -> None:
        bootstrap = RealtimeBootstrap(
            world_id="world_demo_001",
            stream_id=self.stream_id,
            stream_url="wss://api.yaya.example/v1/realtime",
            last_event_sequence=731,
        )
        self.assertEqual("1.0.0", bootstrap.stream_protocol_version)
        for invalid in [
            "ws://api.yaya.example/v1/realtime",
            "https://api.yaya.example/v1/realtime",
            "wss://user:secret@api.yaya.example/v1/realtime",
            "wss://api.yaya.example/v1/realtime?token=secret",
            "wss://api.yaya.example/v1/realtime#fragment",
            "wss://api.yaya.example/v1/%zz",
            "wss://api.yaya.example/v1/[",
            "wss://api.yaya.example/v1/🌱",
        ]:
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                RealtimeBootstrap("world_demo_001", self.stream_id, invalid, 731)
        with self.assertRaises(ValueError):
            RealtimeBootstrap("world_demo_001", self.stream_id, bootstrap.stream_url, -1)
        with self.assertRaises(ValueError):
            RealtimeBootstrap(
                "world_demo_001",
                "world:different_world",
                bootstrap.stream_url,
                731,
            )
        with self.assertRaises(ValueError):
            RealtimeBootstrap(
                "world_demo_001",
                self.stream_id,
                bootstrap.stream_url,
                731,
                "2.0.0",  # type: ignore[arg-type]
            )

    def test_client_frames_reject_bad_checkpoints_and_protocols(self) -> None:
        subscribe = RealtimeSubscribeFrame(
            request_id="req_realtime_00000001",
            stream_id=self.stream_id,
            after_sequence=731,
        )
        self.assertEqual("subscribe", subscribe.frame_type)
        self.assertEqual(
            731,
            RealtimeResumeFrame(
                request_id="req_realtime_00000002",
                subscription_id=self.subscription_id,
                stream_id=self.stream_id,
                after_sequence=731,
            ).after_sequence,
        )
        self.assertEqual(
            "ack",
            RealtimeAckFrame(
                subscription_id=self.subscription_id,
                stream_id=self.stream_id,
                sequence=732,
                event_id="evt_world_00000001",
            ).frame_type,
        )
        self.assertEqual(
            "heartbeat_ack",
            RealtimeHeartbeatAckFrame(
                subscription_id=self.subscription_id,
                nonce="hb_realtime_00000001",
                received_at=datetime.now(UTC),
            ).frame_type,
        )
        with self.assertRaises(ValueError):
            RealtimeSubscribeFrame("req_realtime_00000001", self.stream_id, -1)
        with self.assertRaises(ValueError):
            RealtimeAckFrame(
                self.subscription_id,
                self.stream_id,
                0,
                "evt_world_00000001",
            )
        with self.assertRaises(ValueError):
            RealtimeHeartbeatAckFrame(
                self.subscription_id,
                "hb_realtime_00000001",
                datetime.now(),
            )
        with self.assertRaises(FrozenInstanceError):
            subscribe.after_sequence = 999  # type: ignore[misc]

    def test_server_frames_enforce_watermarks_and_error_close_mapping(self) -> None:
        subscribed = RealtimeSubscribedFrame(
            request_id="req_realtime_00000001",
            subscription_id=self.subscription_id,
            stream_id=self.stream_id,
            accepted_after_sequence=731,
            high_watermark_sequence=733,
            heartbeat_interval_ms=30_000,
            max_unacked_events=256,
        )
        self.assertEqual("subscribed", subscribed.frame_type)
        heartbeat = RealtimeHeartbeatFrame(
            subscription_id=self.subscription_id,
            stream_id=self.stream_id,
            nonce="hb_realtime_00000001",
            server_time=datetime.now(UTC),
            high_watermark_sequence=733,
        )
        self.assertEqual(733, heartbeat.high_watermark_sequence)
        with self.assertRaises(ValueError):
            RealtimeSubscribedFrame(
                "req_realtime_00000001",
                self.subscription_id,
                self.stream_id,
                734,
                733,
                30_000,
                256,
            )

        valid = RealtimeErrorFrame(
            request_id="req_realtime_00000002",
            subscription_id=self.subscription_id,
            stream_id=self.stream_id,
            fatal=True,
            close_code=4409,
            retry_after_ms=0,
            error=self.error(),
        )
        self.assertEqual("error", valid.frame_type)
        for fatal, close_code in [(True, None), (False, 4409)]:
            with self.subTest(fatal=fatal), self.assertRaises(ValueError):
                RealtimeErrorFrame(
                    request_id=None,
                    subscription_id=None,
                    stream_id=None,
                    fatal=fatal,
                    close_code=close_code,  # type: ignore[arg-type]
                    retry_after_ms=None,
                    error=self.error(),
                )
        with self.assertRaises(ValueError):
            RealtimeErrorFrame(
                request_id=None,
                subscription_id=None,
                stream_id=None,
                fatal=True,
                close_code=4401,
                retry_after_ms=None,
                error=self.error(),
            )
        with self.assertRaises(ValueError):
            RealtimeErrorFrame(
                request_id=None,
                subscription_id=None,
                stream_id=None,
                fatal=True,
                close_code=4401,
                retry_after_ms=1000,
                error=self.error("AUTHENTICATION_REQUIRED"),
            )

    def test_checkpoint_is_contiguous_and_close_codes_match_asyncapi(self) -> None:
        self.assertEqual(
            0,
            RealtimeCheckpoint(self.stream_id, 0, None).last_applied_sequence,
        )
        self.assertEqual(
            "evt_world_00000001",
            RealtimeCheckpoint(
                self.stream_id,
                732,
                "evt_world_00000001",
            ).last_event_id,
        )
        with self.assertRaises(ValueError):
            RealtimeCheckpoint(self.stream_id, 1, None)
        with self.assertRaises(ValueError):
            RealtimeCheckpoint(self.stream_id, 0, "evt_world_00000001")

        asyncapi = json.loads(
            (AGENT_ROOT / "contracts/asyncapi/runtime-events.asyncapi.json").read_text(
                encoding="utf-8"
            )
        )
        wire_codes = set(asyncapi["components"]["schemas"]["RealtimeCloseCode"]["enum"])
        python_codes = set(get_args(RealtimeCloseCode.__value__))
        self.assertEqual(wire_codes, python_codes)

    def test_python_frame_fields_exactly_match_asyncapi(self) -> None:
        asyncapi = json.loads(
            (AGENT_ROOT / "contracts/asyncapi/runtime-events.asyncapi.json").read_text(
                encoding="utf-8"
            )
        )
        pairs = {
            "SubscribeFrame": RealtimeSubscribeFrame,
            "ResumeFrame": RealtimeResumeFrame,
            "AckFrame": RealtimeAckFrame,
            "HeartbeatAckFrame": RealtimeHeartbeatAckFrame,
            "SubscribedFrame": RealtimeSubscribedFrame,
            "HeartbeatFrame": RealtimeHeartbeatFrame,
            "RealtimeErrorFrame": RealtimeErrorFrame,
        }
        schemas = asyncapi["components"]["schemas"]
        for schema_name, python_class in pairs.items():
            with self.subTest(schema=schema_name):
                self.assertEqual(
                    set(schemas[schema_name]["required"]),
                    {item.name for item in fields(python_class)},
                )


if __name__ == "__main__":
    unittest.main()
