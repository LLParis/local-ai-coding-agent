from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class EndpointError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class EndpointReport:
    base_url: str
    model: str
    model_count: int
    models_latency_ms: int
    response_latency_ms: int | None


def normalize_loopback_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise EndpointError("endpoint must use plain HTTP on a loopback host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EndpointError("endpoint URL may not contain credentials, a query, or a fragment")
    path = parsed.path.rstrip("/")
    if path != "/v1":
        raise EndpointError("endpoint base URL must end exactly in /v1")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _request_json(url: str, *, timeout: float, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if data is None else "POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CodingIntelligence-Continuity/1",
        },
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise EndpointError(f"endpoint returned HTTP {response.status}")
            body = response.read(2 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError(f"endpoint request failed: {type(exc).__name__}") from exc
    if len(body) > 2 * 1024 * 1024:
        raise EndpointError("endpoint response exceeds 2 MiB")
    try:
        return json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EndpointError("endpoint returned invalid JSON") from exc


def _output_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct.strip()
    texts: list[str] = []
    output = response.get("output", [])
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    texts.append(part["text"])
    return "".join(texts).strip()


def check_endpoint(
    base_url: str,
    model: str,
    *,
    timeout: float = 90.0,
    models_only: bool = False,
) -> EndpointReport:
    base = normalize_loopback_base_url(base_url)
    if not model or len(model) > 200:
        raise EndpointError("model must be a non-empty identifier")
    started = time.monotonic()
    models = _request_json(f"{base}/models", timeout=timeout)
    models_latency = round((time.monotonic() - started) * 1000)
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise EndpointError("/v1/models did not return an OpenAI-compatible data list")
    identifiers = {
        item.get("id")
        for item in models["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if model not in identifiers and f"{model}:latest" not in identifiers:
        raise EndpointError(f"required model is not loaded: {model}")
    if models_only:
        return EndpointReport(base, model, len(identifiers), models_latency, None)

    started = time.monotonic()
    response = _request_json(
        f"{base}/responses",
        timeout=timeout,
        payload={
            "input": "Reply with exactly EXCALIBUR_OK and nothing else.",
            "max_output_tokens": 512,
            "model": model,
            "stream": False,
        },
    )
    response_latency = round((time.monotonic() - started) * 1000)
    if _output_text(response) != "EXCALIBUR_OK":
        raise EndpointError("/v1/responses failed the exact-output readiness probe")
    return EndpointReport(base, model, len(identifiers), models_latency, response_latency)
