"""Strict live-event bridge for the pinned DeepSeek Harness child."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

ALLOWED_TOOLS = frozenset(("read", "search", "edit", "test"))
MAX_LINE_BYTES = 1_048_576
MAX_STREAM_BYTES = 4_194_304


class EventProtocolError(ValueError):
    """The child violated the reviewed live-event contract."""


def strict_json_loads(text: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EventProtocolError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise EventProtocolError(f"non-finite JSON number: {value}")

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=constant)
    except EventProtocolError:
        raise
    except json.JSONDecodeError as error:
        raise EventProtocolError(f"invalid JSON: {error.msg}") from error


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EventProtocolError(f"{name} must be an integer >= {minimum}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise EventProtocolError(f"{name} must be a non-empty string")
    return value


@dataclass
class _Call:
    name: str
    effect_id: str | None
    denied: str | None
    settled: bool = False


@dataclass
class EventAudit:
    max_turns: int
    max_tool_calls: int
    turns: set[tuple[int, int]] = field(default_factory=set)
    calls: dict[str, _Call] = field(default_factory=dict)
    effect_ids: set[str] = field(default_factory=set)
    edit_calls: int = 0
    test_calls: int = 0
    edit_succeeded: bool = False
    test_succeeded: bool = False
    agent_end: bool = False
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})

    def accept(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise EventProtocolError("event must be a JSON object")
        kind = _text(event.get("type"), "event.type")
        if self.agent_end:
            raise EventProtocolError(f"event followed agent_end: {kind}")
        if kind == "turn_start":
            self._turn(event)
        elif kind == "tool_execution_start":
            self._tool_start(event)
        elif kind == "tool_execution_end":
            self._tool_end(event)
        elif kind == "model_request_error":
            self._model_error(event)
        elif kind == "agent_end":
            self._agent_end(event)
        elif kind == "agent_error":
            self._agent_error(event)
        elif kind == "automatic_retry_start":
            raise EventProtocolError("automatic model retry is forbidden")
        elif kind == "model_budget_exceeded":
            raise EventProtocolError("model-call budget was exceeded")
        else:
            raise EventProtocolError(f"unknown event type: {kind}")

    def _turn(self, event: dict[str, Any]) -> None:
        turn = _integer(event.get("turn"), "turn", minimum=1)
        step = _integer(event.get("step"), "step", minimum=1)
        attempt = _integer(event.get("attempt"), "attempt", minimum=1)
        if attempt != 1:
            raise EventProtocolError("a model step had more than one request attempt")
        key = (turn, step)
        if key in self.turns:
            raise EventProtocolError("duplicate model request identity")
        self.turns.add(key)
        if len(self.turns) > self.max_turns:
            raise EventProtocolError("model-call budget exceeded")

    def _tool_start(self, event: dict[str, Any]) -> None:
        call_id = _text(event.get("toolCallId"), "toolCallId")
        name = _text(event.get("toolName"), "toolName")
        if name not in ALLOWED_TOOLS:
            raise EventProtocolError(f"tool is outside allowlist: {name}")
        if call_id in self.calls:
            raise EventProtocolError("duplicate tool call id")
        if len(self.calls) >= self.max_tool_calls:
            raise EventProtocolError("tool-call budget exceeded")
        if not isinstance(event.get("args"), dict):
            raise EventProtocolError("tool args must be an object")
        intent = event.get("intent")
        if not isinstance(intent, dict) or intent.get("recorded") is not True:
            raise EventProtocolError("tool start lacks recorded pre-dispatch intent")
        side_effect = name in ("edit", "test")
        if intent.get("side_effect") is not side_effect:
            raise EventProtocolError("side-effect classification is incorrect")
        effect_id = event.get("effectId")
        if side_effect:
            effect_id = _text(effect_id, "effectId")
            if effect_id in self.effect_ids:
                if event.get("denied") != "duplicate side-effect identity":
                    raise EventProtocolError("duplicate side-effect identity was not denied")
            else:
                self.effect_ids.add(effect_id)
        elif effect_id is not None:
            raise EventProtocolError("read-only tool unexpectedly has an effect identity")
        denied = event.get("denied")
        if denied is not None and not isinstance(denied, str):
            raise EventProtocolError("denied must be null or a string")
        if name == "edit":
            self.edit_calls += 1
        if name == "test":
            self.test_calls += 1
            if not self.edit_succeeded and denied is None:
                raise EventProtocolError("test dispatch preceded a successful edit")
        self.calls[call_id] = _Call(name=name, effect_id=effect_id, denied=denied)

    def _tool_end(self, event: dict[str, Any]) -> None:
        call_id = _text(event.get("toolCallId"), "toolCallId")
        name = _text(event.get("toolName"), "toolName")
        call = self.calls.get(call_id)
        if call is None:
            raise EventProtocolError("tool result lacks a matching start")
        if call.settled:
            raise EventProtocolError("tool result repeated")
        if call.name != name:
            raise EventProtocolError("tool result name does not match its start")
        if not isinstance(event.get("isError"), bool):
            raise EventProtocolError("tool result isError must be boolean")
        if not isinstance(event.get("result"), dict):
            raise EventProtocolError("tool result payload must be an object")
        call.settled = True
        succeeded = call.denied is None and event["isError"] is False
        if name == "edit" and succeeded:
            self.edit_succeeded = True
        if name == "test" and succeeded:
            self.test_succeeded = True

    @staticmethod
    def _model_error(event: dict[str, Any]) -> None:
        _integer(event.get("turn"), "turn", minimum=1)
        _integer(event.get("step"), "step", minimum=1)
        _text(event.get("provider"), "provider")
        error = event.get("error")
        if not isinstance(error, dict):
            raise EventProtocolError("model_request_error.error must be an object")
        _text(error.get("code"), "error.code")
        _text(error.get("message"), "error.message")

    def _agent_end(self, event: dict[str, Any]) -> None:
        status = event.get("status")
        if status not in ("completed", "failed"):
            raise EventProtocolError("agent_end status is invalid")
        model_calls = _integer(event.get("modelCalls"), "modelCalls")
        tool_calls = _integer(event.get("toolCalls"), "toolCalls")
        if model_calls != len(self.turns) or tool_calls != len(self.calls):
            raise EventProtocolError("terminal call counts do not match the live stream")
        if any(not call.settled for call in self.calls.values()):
            raise EventProtocolError("agent ended with an unsettled tool call")
        usage = event.get("usage")
        if not isinstance(usage, dict):
            raise EventProtocolError("agent_end usage must be an object")
        for key in ("input", "output", "total"):
            _integer(usage.get(key), f"usage.{key}")
        if usage["total"] != usage["input"] + usage["output"]:
            raise EventProtocolError("usage.total must equal usage.input + usage.output")
        self.usage = {key: usage[key] for key in ("input", "output", "total")}
        if status == "completed" and not (self.edit_succeeded and self.test_succeeded):
            raise EventProtocolError("completed agent lacks successful edit->test evidence")
        self.agent_end = True

    @staticmethod
    def _agent_error(event: dict[str, Any]) -> None:
        error = event.get("error")
        if not isinstance(error, dict):
            raise EventProtocolError("agent_error.error must be an object")
        _text(error.get("name"), "error.name")
        _text(error.get("message"), "error.message")

    def finish(self, return_code: int) -> None:
        if not self.agent_end:
            raise EventProtocolError(
                f"DeepSeek child exited with code {return_code} without agent_end"
            )


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _protocol_failure(error: Exception) -> None:
    _emit(
        {
            "type": "message_end",
            "message": {
                "stopReason": "error",
                "errorMessage": f"DeepSeek live-event protocol violation: {error}",
            },
        }
    )


def bridge(command: list[str], *, max_turns: int, max_tool_calls: int) -> int:
    audit = EventAudit(max_turns=max_turns, max_tool_calls=max_tool_calls)
    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        env=dict(os.environ),
        bufsize=0,
    )
    assert child.stdout is not None
    total = 0
    try:
        for raw in iter(child.stdout.readline, b""):
            total += len(raw)
            if len(raw) > MAX_LINE_BYTES:
                raise EventProtocolError("one event exceeded 1 MiB")
            if total > MAX_STREAM_BYTES:
                raise EventProtocolError("event stream exceeded 4 MiB")
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise EventProtocolError("event stream was not UTF-8") from error
            event = strict_json_loads(text)
            audit.accept(event)
            _emit(event)
        return_code = child.wait(timeout=5)
        audit.finish(return_code)
        return return_code
    except (EventProtocolError, subprocess.SubprocessError) as error:
        _protocol_failure(error)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        return 2
    finally:
        child.stdout.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and relay DeepSeek live events")
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--max-tool-calls", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a child command is required after --")
    if not 1 <= args.max_turns <= 32 or not 1 <= args.max_tool_calls <= 64:
        parser.error("budgets are outside qualification bounds")
    return bridge(command, max_turns=args.max_turns, max_tool_calls=args.max_tool_calls)


if __name__ == "__main__":
    raise SystemExit(main())
