"""Normalize Qwen Code's official headless stream into production telemetry."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from typing import Any


class QwenCodeEventError(ValueError):
    """The official stream was malformed or internally inconsistent."""


def strict_json_loads(text: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise QwenCodeEventError(f"duplicate JSON key: {key!r}")
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise QwenCodeEventError(f"non-finite JSON value: {value}")

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=constant)
    except QwenCodeEventError:
        raise
    except json.JSONDecodeError as error:
        raise QwenCodeEventError(f"invalid headless JSON: {error.msg}") from error


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QwenCodeEventError(f"{name} must be a non-negative integer")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise QwenCodeEventError(f"{name} must be a non-empty string")
    return value


def _content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content", [])
    if not isinstance(content, list):
        raise QwenCodeEventError("message.content must be an array")
    if not all(isinstance(item, dict) for item in content):
        raise QwenCodeEventError("message.content entries must be objects")
    return content


def _result_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _model_requests(stats: dict[str, Any]) -> tuple[int, dict[str, int]]:
    raw_models = stats.get("models", {})
    if not isinstance(raw_models, dict):
        raise QwenCodeEventError("result.stats.models must be an object")
    by_model: dict[str, int] = {}
    for raw_name, raw_value in raw_models.items():
        name = _text(raw_name, "model name")
        if not isinstance(raw_value, dict):
            raise QwenCodeEventError(f"result.stats.models[{name!r}] must be an object")
        api = raw_value.get("api")
        if isinstance(api, dict) and "totalRequests" in api:
            requests = _integer(api.get("totalRequests"), f"models.{name}.api.totalRequests")
        elif "requests" in raw_value:
            requests = _integer(raw_value.get("requests"), f"models.{name}.requests")
        else:
            raise QwenCodeEventError(f"result.stats.models[{name!r}] has no request count")
        by_model[name] = requests
    return sum(by_model.values()), by_model


def _tool_counts(stats: dict[str, Any]) -> tuple[int, dict[str, int], int, int]:
    tools = stats.get("tools")
    if not isinstance(tools, dict):
        raise QwenCodeEventError("result.stats.tools must be an object")
    total = _integer(tools.get("totalCalls"), "result.stats.tools.totalCalls")
    success = _integer(tools.get("totalSuccess", 0), "result.stats.tools.totalSuccess")
    failed = _integer(tools.get("totalFail", 0), "result.stats.tools.totalFail")
    raw_by_name = tools.get("byName", {})
    if not isinstance(raw_by_name, dict):
        raise QwenCodeEventError("result.stats.tools.byName must be an object")
    by_name: dict[str, int] = {}
    for raw_name, raw_value in raw_by_name.items():
        name = _text(raw_name, "tool name")
        if not isinstance(raw_value, dict):
            raise QwenCodeEventError(f"result.stats.tools.byName[{name!r}] must be an object")
        by_name[name] = _integer(raw_value.get("count"), f"tools.{name}.count")
    if sum(by_name.values()) != total:
        raise QwenCodeEventError("Qwen Code tool totals disagree with its per-tool counts")
    if success + failed != total:
        raise QwenCodeEventError("Qwen Code tool success/failure totals disagree")
    return total, by_name, success, failed


@dataclass
class QwenCodeEventAccumulator:
    """Audit completed headless messages while preserving the official event stream."""

    session_id: str | None = None
    raw_event_count: int = 0
    assistant_messages: int = 0
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    assistant_text: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None

    def accept(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            raise QwenCodeEventError("headless event must be an object")
        self.raw_event_count += 1
        kind = _text(value.get("type"), "event.type")
        normalized: list[dict[str, Any]] = []
        if kind == "system":
            raw_session_id = value.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                if self.session_id is not None and self.session_id != raw_session_id:
                    raise QwenCodeEventError("headless stream changed session ID")
                self.session_id = raw_session_id
            normalized.append(
                {
                    "type": "session_start"
                    if value.get("subtype") in {"session_start", "init"}
                    else "harness_system",
                    "session_id": self.session_id,
                    "subtype": value.get("subtype"),
                }
            )
        elif kind == "assistant":
            normalized.extend(self._assistant(value))
        elif kind == "user":
            normalized.extend(self._user(value))
        elif kind == "result":
            if self.result is not None:
                raise QwenCodeEventError("headless stream emitted multiple result events")
            self.result = value
            if self.session_id is None:
                raw_session_id = value.get("session_id")
                if isinstance(raw_session_id, str) and raw_session_id:
                    self.session_id = raw_session_id
            normalized.append(
                {
                    "type": "harness_result",
                    "subtype": value.get("subtype"),
                    "is_error": value.get("is_error"),
                    "duration_ms": value.get("duration_ms"),
                    "num_turns": value.get("num_turns"),
                }
            )
        elif kind in ("stream_event", "control_response"):
            pass
        elif kind == "control_request":
            raise QwenCodeEventError("Qwen Code requested interactive permission in YOLO mode")
        else:
            normalized.append({"type": "harness_event", "event_type": kind})
        return normalized

    def _assistant(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise QwenCodeEventError("assistant event has an invalid message")
        self.assistant_messages += 1
        output: list[dict[str, Any]] = [
            {"type": "model_response", "index": self.assistant_messages}
        ]
        for block in _content(message):
            block_type = block.get("type")
            if block_type == "tool_use":
                call_id = _text(block.get("id"), "tool_use.id")
                name = _text(block.get("name"), "tool_use.name")
                arguments = block.get("input", {})
                if not isinstance(arguments, dict):
                    raise QwenCodeEventError("tool_use.input must be an object")
                if call_id in self.calls:
                    raise QwenCodeEventError(f"duplicate tool use ID: {call_id}")
                self.calls[call_id] = {"name": name, "input": arguments}
                output.append(
                    {
                        "type": "tool_call",
                        "call_id": call_id,
                        "tool": name,
                        "input": arguments,
                    }
                )
            elif block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    self.assistant_text.append(text)
        return output

    def _user(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            raise QwenCodeEventError("user event has an invalid message")
        output: list[dict[str, Any]] = []
        for block in _content(message):
            if block.get("type") != "tool_result":
                continue
            call_id = _text(block.get("tool_use_id"), "tool_result.tool_use_id")
            if call_id not in self.calls:
                raise QwenCodeEventError(f"tool result has no matching call: {call_id}")
            if call_id in self.tool_results:
                raise QwenCodeEventError(f"duplicate tool result: {call_id}")
            result = {
                "call_id": call_id,
                "tool": self.calls[call_id]["name"],
                "is_error": bool(block.get("is_error", False)),
                "result_sha256": _result_sha256(block.get("content")),
            }
            self.tool_results[call_id] = result
            output.append({"type": "tool_result", **result})
        return output

    def finish(self) -> dict[str, Any]:
        if self.result is None:
            raise QwenCodeEventError("headless stream omitted its terminal result")
        if self.session_id is None:
            self.session_id = "derived:" + _result_sha256(self.result).removeprefix("sha256:")[:32]
        unresolved = sorted(set(self.calls) - set(self.tool_results))
        if unresolved:
            raise QwenCodeEventError(f"headless stream left unresolved tool calls: {unresolved}")
        stats = self.result.get("stats")
        if not isinstance(stats, dict):
            raise QwenCodeEventError("headless result omitted exact session stats")
        model_total, model_by_name = _model_requests(stats)
        tool_total, tool_by_name, tool_success, tool_failed = _tool_counts(stats)
        parsed_by_name: dict[str, int] = {}
        for call in self.calls.values():
            name = call["name"]
            parsed_by_name[name] = parsed_by_name.get(name, 0) + 1
        if tool_total != len(self.calls) or tool_by_name != parsed_by_name:
            raise QwenCodeEventError(
                "Qwen Code final tool stats disagree with its official message stream"
            )
        final_text = self.result.get("result")
        if not isinstance(final_text, str):
            final_text = "\n".join(self.assistant_text).strip()
        return {
            "session_id": self.session_id,
            "is_error": bool(self.result.get("is_error", False)),
            "subtype": self.result.get("subtype"),
            "final_text": final_text,
            "duration_ms": self.result.get("duration_ms"),
            "api_duration_ms": self.result.get("duration_api_ms"),
            "turns": self.result.get("num_turns"),
            "usage": self.result.get("usage", {}),
            "stats": stats,
            "model_calls": {"total": model_total, "by_model": model_by_name},
            "tool_calls": {
                "total": tool_total,
                "success": tool_success,
                "failed": tool_failed,
                "by_name": tool_by_name,
                "records": [
                    {
                        "call_id": call_id,
                        "tool": self.calls[call_id]["name"],
                        "input": self.calls[call_id]["input"],
                        "is_error": self.tool_results[call_id]["is_error"],
                        "result_sha256": self.tool_results[call_id]["result_sha256"],
                    }
                    for call_id in self.calls
                ],
            },
            "permission_denials": self.result.get("permission_denials", []),
            "raw_event_count": self.raw_event_count,
        }
