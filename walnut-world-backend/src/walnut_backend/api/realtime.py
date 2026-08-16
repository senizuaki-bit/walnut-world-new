"""Public WSS replay protocol, isolated from private runtime events."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from yaya_agent_contracts import Failure, OperationContext

from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.application.realtime.subscription import RealtimeSubscriptions

router = APIRouter()

_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")
_STREAM_ID = re.compile(r"^[A-Za-z][A-Za-z0-9:_-]{2,159}$")
_SUBSCRIPTION_ID = re.compile(r"^sub_[A-Za-z0-9_-]{8,96}$")
_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_-]{8,128}$")


@router.websocket("/v1/realtime")
async def world_realtime(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        initial = await websocket.receive_json()
    except (ValueError, WebSocketDisconnect):
        await _close_error(websocket, "INVALID_REQUEST", "first frame must be JSON", 4400)
        return
    parsed = _initial_frame(initial)
    if parsed is None:
        await _close_error(websocket, "INVALID_REQUEST", "invalid initial subscription frame", 4400)
        return
    request_id, subscription_id, stream_id, after_sequence = parsed
    result = await _subscriptions(websocket).replay(
        stream_id, after_sequence, get_operation_context(websocket)
    )
    if isinstance(result, Failure):
        await _close_for_failure(websocket, result)
        return
    high_watermark, events = result.value
    await websocket.send_json(
        {
            "frame_type": "subscribed",
            "protocol_version": "1.0.0",
            "request_id": request_id,
            "subscription_id": subscription_id,
            "stream_id": stream_id,
            "accepted_after_sequence": after_sequence,
            "high_watermark_sequence": high_watermark,
            "heartbeat_interval_ms": 30_000,
            "max_unacked_events": 10_000,
        }
    )
    sent = {event["sequence"]: event["event_id"] for event in events}
    for event in events:
        await websocket.send_json(event)
    await _serve_acks(
        websocket,
        _subscriptions(websocket),
        get_operation_context(websocket),
        subscription_id,
        stream_id,
        sent,
        after_sequence,
        high_watermark,
    )


def _subscriptions(websocket: WebSocket) -> RealtimeSubscriptions:
    return websocket.app.state.realtime_subscriptions


def _initial_frame(value: Any) -> tuple[str, str, str, int] | None:
    if not isinstance(value, dict) or value.get("frame_type") not in {"subscribe", "resume"}:
        return None
    required = {"frame_type", "protocol_version", "request_id", "stream_id", "after_sequence"}
    if value.get("frame_type") == "resume":
        required.add("subscription_id")
    if set(value) != required:
        return None
    if (
        value.get("protocol_version") != "1.0.0"
        or not isinstance(value.get("request_id"), str)
        or _REQUEST_ID.fullmatch(value["request_id"]) is None
        or not isinstance(value.get("stream_id"), str)
        or _STREAM_ID.fullmatch(value["stream_id"]) is None
        or isinstance(value.get("after_sequence"), bool)
        or not isinstance(value.get("after_sequence"), int)
        or value["after_sequence"] < 0
    ):
        return None
    if value["frame_type"] == "resume":
        subscription_id = value["subscription_id"]
        if not isinstance(subscription_id, str) or _SUBSCRIPTION_ID.fullmatch(subscription_id) is None:
            return None
    else:
        subscription_id = f"sub_{uuid4().hex}"
    return value["request_id"], subscription_id, value["stream_id"], value["after_sequence"]


async def _serve_acks(
    websocket: WebSocket,
    subscriptions: RealtimeSubscriptions,
    context: OperationContext,
    subscription_id: str,
    stream_id: str,
    sent: dict[int, str],
    accepted_after_sequence: int,
    high_watermark: int,
) -> None:
    highest_acked = accepted_after_sequence
    last_sent = high_watermark
    outstanding = max(0, high_watermark - accepted_after_sequence)
    heartbeat_nonce: str | None = None
    try:
        while True:
            try:
                frame = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            except TimeoutError:
                replay = await subscriptions.replay(stream_id, last_sent, context)
                if isinstance(replay, Failure):
                    await _close_for_failure(websocket, replay)
                    return
                high_watermark, events = replay.value
                for event in events:
                    sent[event["sequence"]] = event["event_id"]
                    await websocket.send_json(event)
                outstanding += len(events)
                last_sent = high_watermark
                if outstanding > 10_000:
                    await _close_error(websocket, "INVALID_REQUEST", "too many unacknowledged events", 4400)
                    return
                heartbeat = _heartbeat(subscription_id, stream_id, high_watermark)
                heartbeat_nonce = heartbeat["nonce"]
                await websocket.send_json(heartbeat)
                continue
            if _is_valid_heartbeat_ack(frame, subscription_id, heartbeat_nonce):
                heartbeat_nonce = None
                continue
            if not _is_valid_ack(frame, subscription_id, stream_id, sent, highest_acked):
                await _close_error(websocket, "INVALID_REQUEST", "invalid acknowledgement", 4400)
                return
            highest_acked = frame["sequence"]
            outstanding = max(0, last_sent - highest_acked)
            if highest_acked == last_sent:
                heartbeat = _heartbeat(subscription_id, stream_id, high_watermark)
                heartbeat_nonce = heartbeat["nonce"]
                await websocket.send_json(heartbeat)
    except (ValueError, WebSocketDisconnect):
        return


def _is_valid_ack(
    frame: Any,
    subscription_id: str,
    stream_id: str,
    sent: dict[int, str],
    highest_acked: int,
) -> bool:
    if not isinstance(frame, dict) or set(frame) != {
        "frame_type",
        "protocol_version",
        "subscription_id",
        "stream_id",
        "sequence",
        "event_id",
    }:
        return False
    if (
        frame.get("frame_type") != "ack"
        or frame.get("protocol_version") != "1.0.0"
        or frame.get("subscription_id") != subscription_id
        or frame.get("stream_id") != stream_id
        or isinstance(frame.get("sequence"), bool)
        or not isinstance(frame.get("sequence"), int)
        or frame["sequence"] <= highest_acked
        or sent.get(frame["sequence"]) != frame.get("event_id")
        or not isinstance(frame.get("event_id"), str)
        or _EVENT_ID.fullmatch(frame["event_id"]) is None
    ):
        return False
    return all(sequence in sent for sequence in range(highest_acked + 1, frame["sequence"] + 1))


def _is_valid_heartbeat_ack(frame: Any, subscription_id: str, nonce: str | None) -> bool:
    return (
        nonce is not None
        and isinstance(frame, dict)
        and set(frame)
        == {"frame_type", "protocol_version", "subscription_id", "nonce", "received_at"}
        and frame.get("frame_type") == "heartbeat_ack"
        and frame.get("protocol_version") == "1.0.0"
        and frame.get("subscription_id") == subscription_id
        and frame.get("nonce") == nonce
        and isinstance(frame.get("received_at"), str)
    )


def _heartbeat(subscription_id: str, stream_id: str, high_watermark: int) -> dict[str, Any]:
    return {
        "frame_type": "heartbeat",
        "protocol_version": "1.0.0",
        "subscription_id": subscription_id,
        "stream_id": stream_id,
        "nonce": f"hb_{uuid4().hex}",
        "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "high_watermark_sequence": high_watermark,
    }


async def _close_for_failure(websocket: WebSocket, result: Failure) -> None:
    close_code = {
        "NOT_FOUND": 4404,
        "EVENT_SEQUENCE_GAP": 4409,
        "INVALID_REQUEST": 4400,
    }.get(result.error.code, 4500)
    await _close_error(websocket, result.error.code, result.error.message or result.error.code, close_code)


async def _close_error(websocket: WebSocket, code: str, message: str, close_code: int) -> None:
    await websocket.send_json(
        {
            "frame_type": "error",
            "protocol_version": "1.0.0",
            "error": {
                "code": code,
                "category": "VALIDATION" if code in {"INVALID_REQUEST", "NOT_FOUND"} else "CONCURRENCY",
                "retryable": code == "EVENT_SEQUENCE_GAP",
                "user_message_key": {
                    "INVALID_REQUEST": "request.invalid",
                    "NOT_FOUND": "resource.not_found",
                    "EVENT_SEQUENCE_GAP": "event.resync_required",
                }.get(code, "system.internal_error"),
                "stage": "REALTIME",
                "message": message[:512] or code,
            },
            "close_code": close_code,
            "retry_after_ms": None,
        }
    )
    await websocket.close(code=close_code)
