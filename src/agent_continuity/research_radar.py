from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PAPER_SCHEMA = "coding-intelligence.research-radar.paper/v1"
PAPER_VERSION_SCHEMA = "coding-intelligence.research-radar.paper-version/v1"
SOURCE_OBSERVATION_SCHEMA = "coding-intelligence.research-radar.source-observation/v1"
LIFECYCLE_SCHEMA = "coding-intelligence.research-radar.lifecycle-event/v1"
ARTIFACT_OBSERVATION_SCHEMA = "coding-intelligence.research-radar.artifact-observation/v1"
TRIAGE_SCHEMA = "coding-intelligence.research-radar.triage/v1"
SYNC_CONFIG_SCHEMA = "coding-intelligence.research-radar.sync-config/v1"
SYNC_STATE_SCHEMA = "coding-intelligence.research-radar.sync-state/v1"
SYNC_RUN_SCHEMA = "coding-intelligence.research-radar.sync-run/v1"
CACHE_ENTRY_SCHEMA = "coding-intelligence.research-radar.http-cache-entry/v1"
PRIMARY_RECEIPT_SCHEMA = "coding-intelligence.research-radar.primary-receipt/v1"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_API_MANUAL = "https://info.arxiv.org/help/api/user-manual.html"
DEFAULT_MAX_IDS = 20
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_PAGE_INTERVAL_SECONDS = 3.0
MIN_ARTIFACT_INTERVAL_SECONDS = 0.25
DEFAULT_SYNC_MAX_REQUESTS = 16
DEFAULT_SYNC_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_SYNC_MAX_ITEMS = 40
DEFAULT_SYNC_MAX_ARTIFACTS = 8
DEFAULT_SYNC_MAX_WALL_SECONDS = 180.0
DEFAULT_DISCOVERY_LOOKBACK_HOURS = 48
DEFAULT_VERSION_RECHECK_DAYS = 7
MAX_DISCOVERY_LOOKBACK_HOURS = 168
MAX_CATALOG_RECORDS = 10_000
MAX_PENDING_ARTIFACTS = 1_000

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

_MODERN_ID = r"\d{4}\.\d{4,5}"
_LEGACY_ID = r"[a-z][a-z0-9.-]*/\d{7}"
_ARXIV_ID_RE = re.compile(
    rf"^(?P<base>(?:{_MODERN_ID}|{_LEGACY_ID}))(?P<version>v[1-9]\d*)?$",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_XML_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(
    r"^\s*<\?xml\s+[^?]*encoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.025
_LIVE_RECEIPT_TOKEN = object()
_PLAN_AUTHORITY_TOKEN = object()
_LEGACY_UNBOUND_CONFIG_SHA256 = "sha256:" + "0" * 64
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9.-]{1,31}$", re.IGNORECASE)
_DISCOVERY_TERM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+/\-]{0,63}$")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ALLOWED_NETWORK_HOSTS = frozenset({"export.arxiv.org", "api.github.com", "huggingface.co"})

LIFECYCLE_STATUSES = frozenset(
    {
        "new",
        "metadata_verified",
        "screened",
        "evidence_extracted",
        "decision_required",
        "experiment_candidate",
        "watch",
        "deferred",
        "rejected",
        "reproduced",
        "failed",
        "accepted",
        "inconclusive",
        "evaluator_invalid",
        "runtime_blocked",
        "adopted",
        "superseded",
    }
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"metadata_verified"}),
    "metadata_verified": frozenset({"screened"}),
    "screened": frozenset({"evidence_extracted", "watch", "deferred", "rejected"}),
    "evidence_extracted": frozenset({"decision_required"}),
    "decision_required": frozenset({"experiment_candidate", "watch", "deferred", "rejected"}),
    "experiment_candidate": frozenset(
        {
            "accepted",
            "rejected",
            "inconclusive",
            "evaluator_invalid",
            "runtime_blocked",
            "reproduced",
            "failed",
        }
    ),
    "accepted": frozenset({"reproduced", "adopted", "superseded"}),
    "inconclusive": frozenset({"experiment_candidate", "watch", "deferred"}),
    "evaluator_invalid": frozenset({"experiment_candidate", "deferred"}),
    "runtime_blocked": frozenset({"experiment_candidate", "watch", "deferred"}),
    "reproduced": frozenset({"adopted", "failed", "superseded"}),
    "failed": frozenset({"experiment_candidate", "deferred", "rejected"}),
    "watch": frozenset({"screened", "evidence_extracted", "deferred", "superseded"}),
    "deferred": frozenset({"screened", "evidence_extracted", "superseded"}),
    "rejected": frozenset({"screened", "superseded"}),
    "adopted": frozenset({"superseded"}),
    "superseded": frozenset(),
}

_REVISIT_REQUIRED = frozenset(
    {"watch", "deferred", "rejected", "inconclusive", "evaluator_invalid", "runtime_blocked"}
)
_EVIDENCE_REQUIRED = frozenset(
    {
        "accepted",
        "rejected",
        "reproduced",
        "failed",
        "adopted",
        "evaluator_invalid",
        "runtime_blocked",
        "superseded",
    }
)
_GOVERNED_EXPERIMENT_RESULTS = frozenset({"accepted", "reproduced", "failed", "adopted"})
_WRITE_PRIORITY = {
    "paper": 0,
    "paper_version": 1,
    "source_observation": 2,
    "primary_receipt": 3,
    "lifecycle": 4,
}
_LIFECYCLE_KEYS = {
    "schema",
    "paper_version_key",
    "sequence",
    "previous_record_sha256",
    "from_status",
    "to_status",
    "occurred_at",
    "owner",
    "reason",
    "evidence",
    "revisit_trigger",
    "verdict_class",
}


class ResearchRadarError(RuntimeError):
    pass


class RadarValidationError(ResearchRadarError):
    pass


class RadarConflictError(ResearchRadarError):
    pass


class RadarNetworkError(ResearchRadarError):
    def __init__(self, message: str, *, classification: str):
        super().__init__(message)
        self.classification = classification


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _safe_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class ArxivIdentity:
    base_id: str
    version: int | None

    @property
    def versioned_id(self) -> str:
        if self.version is None:
            return self.base_id
        return f"{self.base_id}v{self.version}"

    @property
    def paper_key(self) -> str:
        return f"arxiv:{self.base_id}"

    @property
    def version_key(self) -> str:
        if self.version is None:
            raise RadarValidationError("an immutable paper-version key requires vN")
        return f"{self.paper_key}:v{self.version}"


@dataclass(frozen=True)
class _FetchReceipt:
    provider: str
    request_locator: str
    final_locator: str
    response_sha256: str
    expected_identifiers: tuple[str, ...]
    source: str
    _token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class SourceBatch:
    source_locator: str
    content: bytes
    expected_identifiers: tuple[str, ...] = ()
    provider: str = "offline_atom_fixture"
    primary_metadata_verified: bool = False
    allow_empty: bool = False
    _receipt: _FetchReceipt | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PlannedWrite:
    kind: str
    relative_path: str
    content: bytes
    state: str

    @property
    def sha256(self) -> str:
        return _sha256(self.content)

    def report(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.relative_path,
            "sha256": self.sha256,
            "state": self.state,
        }


@dataclass(frozen=True)
class TriageProfile:
    profile_id: str
    active_questions: tuple[tuple[str, tuple[str, ...]], ...]
    observed_failures: tuple[tuple[str, tuple[str, ...]], ...]
    mutable_layers: tuple[tuple[str, tuple[str, ...]], ...]
    supported_categories: tuple[str, ...]


@dataclass(frozen=True)
class RadarSyncConfig:
    categories: tuple[str, ...]
    discovery_terms: tuple[str, ...]
    triage: TriageProfile
    max_requests: int = DEFAULT_SYNC_MAX_REQUESTS
    max_response_bytes: int = DEFAULT_SYNC_MAX_RESPONSE_BYTES
    max_items: int = DEFAULT_SYNC_MAX_ITEMS
    max_discovery_items: int = 20
    max_frontier_items: int = 12
    max_backlog_items: int = 8
    max_recheck_items: int = 20
    max_artifacts: int = DEFAULT_SYNC_MAX_ARTIFACTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_wall_seconds: float = DEFAULT_SYNC_MAX_WALL_SECONDS
    discovery_lookback_hours: int = DEFAULT_DISCOVERY_LOOKBACK_HOURS
    version_recheck_days: int = DEFAULT_VERSION_RECHECK_DAYS
    verify_artifacts: bool = True

    def __post_init__(self) -> None:
        _validate_sync_config_instance(self)

    @property
    def sha256(self) -> str:
        return _sha256(_canonical_bytes(sync_config_as_dict(self)))


@dataclass(frozen=True)
class ArtifactCandidate:
    provider: str
    artifact_kind: str
    namespace: str
    name: str
    observed_url: str

    @property
    def normalized_id(self) -> str:
        return f"{self.provider}:{self.artifact_kind}:{self.namespace}/{self.name}"


@dataclass(frozen=True)
class CachedFetch:
    locator: str
    content: bytes
    response_sha256: str
    source: str
    final_locator: str
    content_type: str | None


@dataclass(frozen=True)
class _ObservationAuthority:
    observation_path: str
    observation_sha256: str
    receipt: _FetchReceipt
    version_entries: tuple[tuple[str, str], ...]


class _IngestionPlan(Sequence[PlannedWrite]):
    __slots__ = ("_authority", "_token", "_writes")

    def __init__(
        self,
        writes: Sequence[PlannedWrite],
        authority: Sequence[_ObservationAuthority],
        *,
        token: object,
    ):
        if token is not _PLAN_AUTHORITY_TOKEN:
            raise RadarValidationError("invalid ingestion-plan authority constructor")
        self._writes = tuple(writes)
        self._authority = tuple(authority)
        self._token = token

    def __getitem__(self, index: int | slice) -> PlannedWrite | tuple[PlannedWrite, ...]:
        return self._writes[index]

    def __len__(self) -> int:
        return len(self._writes)

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("ingestion plans are immutable")
        object.__setattr__(self, name, value)


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RadarValidationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_bytes(content: bytes, *, label: str) -> object:
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except RadarValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RadarValidationError(f"{label} is not canonical UTF-8 JSON") from exc


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _digest_id(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean_text(
    value: object | None, *, required: bool = False, label: str = "value"
) -> str | None:
    if value is None:
        if required:
            raise RadarValidationError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise RadarValidationError(f"{label} must be text")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise RadarValidationError(f"{label} is required")
    return cleaned or None


def parse_arxiv_id(value: str, *, require_version: bool = False) -> ArxivIdentity:
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() not in {"http", "https"}:
            raise RadarValidationError(f"unsupported arXiv locator scheme: {value}")
        if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise RadarValidationError(f"not an arXiv locator: {value}")
        candidate = parsed.path
    candidate = candidate.strip("/")
    for prefix in ("abs/", "pdf/"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    if candidate.lower().endswith(".pdf"):
        candidate = candidate[:-4]
    match = _ARXIV_ID_RE.fullmatch(candidate)
    if match is None:
        raise RadarValidationError(f"invalid arXiv identifier: {value}")
    base = match.group("base").lower()
    version_text = match.group("version")
    version = int(version_text[1:]) if version_text else None
    if require_version and version is None:
        raise RadarValidationError(f"arXiv identifier is missing an immutable version: {value}")
    return ArxivIdentity(base, version)


def _canonical_expected_ids(values: Sequence[str]) -> tuple[str, ...]:
    identities = [parse_arxiv_id(item) for item in values]
    if not identities or len({item.base_id for item in identities}) != len(identities):
        raise RadarValidationError("exact-ID receipt identifiers must be nonempty and unique")
    return tuple(sorted(item.versioned_id for item in identities))


def _validate_exact_arxiv_locator(locator: str, expected: Sequence[str]) -> None:
    parsed = urllib.parse.urlsplit(locator)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "export.arxiv.org":
        raise RadarValidationError("exact-ID receipt request is not the primary arXiv endpoint")
    if parsed.path != "/api/query" or parsed.fragment:
        raise RadarValidationError("exact-ID receipt request path is invalid")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"id_list", "start", "max_results"}:
        raise RadarValidationError("exact-ID receipt query keys are invalid")
    if len(query["id_list"]) != 1 or len(query["start"]) != 1 or len(query["max_results"]) != 1:
        raise RadarValidationError("exact-ID receipt query values are ambiguous")
    requested = _canonical_expected_ids(query["id_list"][0].split(","))
    canonical_expected = _canonical_expected_ids(expected)
    if requested != canonical_expected:
        raise RadarValidationError("exact-ID receipt query does not match expected identifiers")
    if query["start"][0] != "0" or query["max_results"][0] != str(len(canonical_expected)):
        raise RadarValidationError("exact-ID receipt query bounds are invalid")


def _mint_live_receipt(
    *,
    provider: str,
    request_locator: str,
    final_locator: str,
    response: bytes,
    expected_identifiers: Sequence[str],
    source: str,
) -> _FetchReceipt:
    if provider != "arxiv_atom_primary":
        raise RadarValidationError("only primary arXiv exact-ID fetches receive authority")
    canonical_expected = _canonical_expected_ids(expected_identifiers)
    _validate_exact_arxiv_locator(request_locator, canonical_expected)
    final = urllib.parse.urlsplit(final_locator)
    if (
        final.scheme != "https"
        or (final.hostname or "").lower() != "export.arxiv.org"
        or final.path != "/api/query"
    ):
        raise RadarValidationError("exact-ID receipt final endpoint is not primary arXiv")
    if final_locator != request_locator:
        raise RadarValidationError("exact-ID receipt final locator differs from its request")
    if source not in {"network", "daily_cache"}:
        raise RadarValidationError("exact-ID receipt source is invalid")
    return _FetchReceipt(
        provider=provider,
        request_locator=request_locator,
        final_locator=final_locator,
        response_sha256=_sha256(response),
        expected_identifiers=canonical_expected,
        source=source,
        _token=_LIVE_RECEIPT_TOKEN,
    )


def _validated_batch_receipt(batch: SourceBatch) -> _FetchReceipt | None:
    receipt = batch._receipt
    if receipt is None:
        return None
    if receipt._token is not _LIVE_RECEIPT_TOKEN:
        return None
    if batch.provider != "arxiv_atom_primary" or receipt.provider != batch.provider:
        return None
    try:
        canonical_expected = _canonical_expected_ids(batch.expected_identifiers)
        _validate_exact_arxiv_locator(batch.source_locator, canonical_expected)
    except RadarValidationError:
        return None
    if (
        receipt.request_locator != batch.source_locator
        or receipt.final_locator != batch.source_locator
        or receipt.expected_identifiers != canonical_expected
        or receipt.response_sha256 != _sha256(batch.content)
        or batch.allow_empty
    ):
        return None
    final = urllib.parse.urlsplit(receipt.final_locator)
    if (
        final.scheme != "https"
        or (final.hostname or "").lower() != "export.arxiv.org"
        or final.path != "/api/query"
        or receipt.source not in {"network", "daily_cache"}
    ):
        return None
    return receipt


def _iso8601(value: object | None, label: str) -> str:
    cleaned = _clean_text(value, required=True, label=label)
    assert cleaned is not None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RadarValidationError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RadarValidationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _child_text(parent: ET.Element, qualified_name: str) -> str | None:
    child = parent.find(qualified_name)
    return child.text if child is not None else None


def _feed_count(root: ET.Element, name: str, *, default: int) -> int:
    raw = _clean_text(_child_text(root, f"{OPENSEARCH}{name}"))
    if raw is None:
        return default
    if re.fullmatch(r"\d+", raw) is None:
        raise RadarValidationError(f"arXiv feed {name} must be a nonnegative integer")
    value = int(raw)
    if value > 10_000_000:
        raise RadarValidationError(f"arXiv feed {name} exceeds the sanity bound")
    return value


def _entry_record(entry: ET.Element) -> tuple[dict[str, object], dict[str, object]]:
    entry_locator = (
        _clean_text(_child_text(entry, f"{ATOM}id"), required=True, label="entry.id") or ""
    )
    entry_identity = parse_arxiv_id(entry_locator)
    links: list[dict[str, str]] = []
    link_versions: set[int] = set()
    for link in entry.findall(f"{ATOM}link"):
        href = _clean_text(link.attrib.get("href"))
        if href is None:
            continue
        links.append(
            {
                "href": href,
                "rel": _clean_text(link.attrib.get("rel")) or "",
                "type": _clean_text(link.attrib.get("type")) or "",
                "title": _clean_text(link.attrib.get("title")) or "",
            }
        )
        try:
            linked_identity = parse_arxiv_id(href)
        except RadarValidationError:
            continue
        if linked_identity.base_id == entry_identity.base_id and linked_identity.version:
            link_versions.add(linked_identity.version)
    links.sort(key=lambda item: (item["rel"], item["type"], item["title"], item["href"]))
    versions = link_versions | (
        {entry_identity.version} if entry_identity.version is not None else set()
    )
    if len(versions) != 1:
        raise RadarValidationError(
            f"arXiv entry {entry_identity.base_id} must resolve to exactly one version"
        )
    identity = ArxivIdentity(entry_identity.base_id, versions.pop())
    title = _clean_text(_child_text(entry, f"{ATOM}title"), required=True, label="entry.title")
    abstract = _clean_text(
        _child_text(entry, f"{ATOM}summary"), required=True, label="entry.summary"
    )
    published = _iso8601(_child_text(entry, f"{ATOM}published"), "entry.published")
    updated = _iso8601(_child_text(entry, f"{ATOM}updated"), "entry.updated")

    authors: list[dict[str, object]] = []
    for author in entry.findall(f"{ATOM}author"):
        name = _clean_text(_child_text(author, f"{ATOM}name"), required=True, label="author.name")
        affiliations = sorted(
            {
                affiliation
                for node in author.findall(f"{ARXIV}affiliation")
                if (affiliation := _clean_text(node.text)) is not None
            }
        )
        authors.append({"name": name, "affiliations": affiliations})
    if not authors:
        raise RadarValidationError(f"{identity.version_key} has no authors")

    categories = sorted(
        {
            term
            for node in entry.findall(f"{ATOM}category")
            if (term := _clean_text(node.attrib.get("term"))) is not None
        }
    )
    primary_node = entry.find(f"{ARXIV}primary_category")
    primary_category = (
        _clean_text(primary_node.attrib.get("term")) if primary_node is not None else None
    )
    if primary_category is None:
        raise RadarValidationError(f"{identity.version_key} has no primary category")
    if primary_category not in categories:
        categories.append(primary_category)
        categories.sort()

    doi = _clean_text(_child_text(entry, f"{ARXIV}doi"))
    entry_metadata = {
        "identity": {
            "arxiv_base_id": identity.base_id,
            "arxiv_version": identity.version,
            "paper_key": identity.paper_key,
            "version_key": identity.version_key,
        },
        "title": title,
        "authors": authors,
        "submitted_at": published,
        "revised_at": updated,
        "categories": categories,
        "primary_category": primary_category,
        "abstract": abstract,
        "license_uri": _clean_text(_child_text(entry, f"{ARXIV}license")),
        "doi": {"observed": doi, "normalized": doi.lower() if doi else None},
        "journal_reference": _clean_text(_child_text(entry, f"{ARXIV}journal_ref")),
        "comment": _clean_text(_child_text(entry, f"{ARXIV}comment")),
        "links": links,
    }
    atom_entry_sha256 = _sha256(_canonical_bytes(entry_metadata))
    version = identity.version
    assert version is not None
    version_record: dict[str, object] = {
        "schema": PAPER_VERSION_SCHEMA,
        **entry_metadata,
        "lineage": {
            "previous_version_key": (
                f"{identity.paper_key}:v{version - 1}" if version > 1 else None
            ),
            "supersedes_metadata": version > 1,
        },
        "status_observations": {
            "withdrawal": "unknown",
            "replacement": "unknown",
            "cross_listed": len(categories) > 1,
        },
        "artifact_integrity": {
            "atom_entry": {"status": "normalized", "sha256": atom_entry_sha256},
            "pdf": {"status": "not_fetched", "sha256": None},
            "source": {"status": "not_fetched", "sha256": None},
        },
        "trust": {
            "classification": "untrusted_external_metadata",
            "code_execution": "forbidden_during_intake",
            "instructions_are_data": True,
        },
    }
    paper_record = {
        "schema": PAPER_SCHEMA,
        "paper_key": identity.paper_key,
        "arxiv_base_id": identity.base_id,
        "canonical_abs_uri": f"https://arxiv.org/abs/{identity.base_id}",
        "identity_policy": "versionless_arxiv_id",
    }
    return paper_record, version_record


def parse_arxiv_atom(batch: SourceBatch, *, max_entries: int) -> dict[str, object]:
    content = batch.content
    live_receipt = _validated_batch_receipt(batch)
    if not content:
        raise RadarValidationError("arXiv response is empty")
    if len(content) > DEFAULT_MAX_RESPONSE_BYTES:
        raise RadarValidationError("arXiv response exceeds the configured byte limit")
    try:
        xml_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RadarValidationError("arXiv Atom must be UTF-8") from exc
    if "\x00" in xml_text:
        raise RadarValidationError("arXiv Atom must be UTF-8 text without NUL bytes")
    encoding = _XML_ENCODING_RE.search(xml_text)
    if encoding is not None and encoding.group(1).lower().replace("_", "-") not in {
        "utf-8",
        "utf8",
    }:
        raise RadarValidationError("arXiv Atom declares a non-UTF-8 encoding")
    if _UNSAFE_XML_RE.search(xml_text):
        raise RadarValidationError("arXiv response contains a forbidden XML declaration")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RadarValidationError(f"invalid arXiv Atom response: {exc}") from exc
    if root.tag != f"{ATOM}feed":
        raise RadarValidationError("arXiv response root must be an Atom feed")
    entries = root.findall(f"{ATOM}entry")
    if not entries and not batch.allow_empty:
        raise RadarValidationError("arXiv response contains no paper entries")
    if len(entries) > max_entries:
        raise RadarValidationError("arXiv response exceeds the configured entry limit")
    if batch.provider == "arxiv_atom_discovery" and any(
        _child_text(root, f"{OPENSEARCH}{name}") is None
        for name in ("startIndex", "itemsPerPage", "totalResults")
    ):
        raise RadarValidationError(
            "arXiv discovery feed is missing required OpenSearch pagination metadata"
        )
    page_start = _feed_count(root, "startIndex", default=0)
    page_items = _feed_count(root, "itemsPerPage", default=len(entries))
    total_results = _feed_count(root, "totalResults", default=page_start + len(entries))
    if page_items < len(entries) or (entries and total_results < page_start + len(entries)):
        raise RadarValidationError("arXiv feed pagination metadata is inconsistent")

    paper_records: dict[str, dict[str, object]] = {}
    version_records: dict[str, dict[str, object]] = {}
    observed_entries: list[dict[str, str]] = []
    for entry in entries:
        paper, version = _entry_record(entry)
        paper_key = str(paper["paper_key"])
        version_key = str(version["identity"]["version_key"])  # type: ignore[index]
        for key, value, records in (
            (paper_key, paper, paper_records),
            (version_key, version, version_records),
        ):
            previous = records.get(key)
            if previous is not None:
                if key == version_key:
                    raise RadarValidationError(f"duplicate arXiv version entry: {key}")
                if previous != value:
                    raise RadarConflictError(f"conflicting duplicate metadata for {key}")
            records[key] = value
        observed_entries.append(
            {
                "version_key": version_key,
                "atom_entry_sha256": str(
                    version["artifact_integrity"]["atom_entry"]["sha256"]  # type: ignore[index]
                ),
            }
        )
    if batch.expected_identifiers:
        expected = [parse_arxiv_id(value) for value in batch.expected_identifiers]
        actual = [
            ArxivIdentity(
                str(record["identity"]["arxiv_base_id"]),
                int(record["identity"]["arxiv_version"]),
            )
            for record in version_records.values()
        ]
        for requested in expected:
            matches = [
                item
                for item in actual
                if item.base_id == requested.base_id
                and (requested.version is None or requested.version == item.version)
            ]
            if len(matches) != 1:
                raise RadarValidationError(
                    f"arXiv response did not resolve exactly one entry for {requested.versioned_id}"
                )
        for returned in actual:
            matches = [
                item
                for item in expected
                if item.base_id == returned.base_id
                and (item.version is None or item.version == returned.version)
            ]
            if len(matches) != 1:
                raise RadarValidationError(
                    f"arXiv response contained unexpected entry {returned.versioned_id}"
                )
    observed_entry_objects = sorted(
        observed_entries,
        key=lambda item: (item["version_key"], item["atom_entry_sha256"]),
    )
    response_sha256 = _sha256(content)
    observation_identity = {
        "provider": batch.provider,
        "source_locator": batch.source_locator,
        "response_sha256": response_sha256,
    }
    observation_id = "arxiv-" + _digest_id(observation_identity)
    observation = {
        "schema": SOURCE_OBSERVATION_SCHEMA,
        "observation_id": observation_id,
        **observation_identity,
        "feed_updated_at": (
            _iso8601(_child_text(root, f"{ATOM}updated"), "feed.updated")
            if _child_text(root, f"{ATOM}updated") is not None
            else None
        ),
        "entries": observed_entry_objects,
        "pagination": {
            "start_index": page_start,
            "items_per_page": page_items,
            "total_results": total_results,
        },
        "trust": {
            "classification": "untrusted_external_metadata",
            "execution_permitted": False,
        },
        "primary_metadata_verified": live_receipt is not None,
    }
    return {
        "papers": [paper_records[key] for key in sorted(paper_records)],
        "versions": [version_records[key] for key in sorted(version_records)],
        "observation": observation,
        "pagination": observation["pagination"],
    }


def fetch_arxiv_batches(
    identifiers: Sequence[str],
    *,
    max_ids: int = DEFAULT_MAX_IDS,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] = _safe_urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[SourceBatch]:
    if not identifiers:
        raise RadarValidationError("at least one arXiv identifier is required")
    if not 1 <= len(identifiers) <= max_ids:
        raise RadarValidationError(f"arXiv identifier count must be between 1 and {max_ids}")
    if not 1 <= page_size <= max_ids:
        raise RadarValidationError("page_size is outside the configured bounds")
    parsed_identifiers = [parse_arxiv_id(value) for value in identifiers]
    if len({item.base_id for item in parsed_identifiers}) != len(parsed_identifiers):
        raise RadarValidationError("duplicate arXiv base identifiers are not allowed")
    canonical = sorted(item.versioned_id for item in parsed_identifiers)

    batches: list[SourceBatch] = []
    response_bytes = 0
    for offset in range(0, len(canonical), page_size):
        if batches:
            sleeper(MIN_PAGE_INTERVAL_SECONDS)
        page = canonical[offset : offset + page_size]
        query = urllib.parse.urlencode(
            {"id_list": ",".join(page), "start": 0, "max_results": len(page)}
        )
        locator = f"{ARXIV_API_URL}?{query}"
        request = urllib.request.Request(
            locator,
            headers={
                "Accept": "application/atom+xml",
                "User-Agent": "CodingIntelligenceResearchRadar/1.0 (local metadata intake)",
            },
        )
        try:
            response = opener(request, timeout=timeout_seconds)
            with response:  # type: ignore[attr-defined]
                final_locator = (
                    response.geturl()  # type: ignore[attr-defined]
                    if hasattr(response, "geturl")
                    else locator
                )
                final = urllib.parse.urlsplit(str(final_locator))
                if (
                    final.scheme != "https"
                    or (final.hostname or "").lower() != "export.arxiv.org"
                    or final.path != "/api/query"
                ):
                    raise RadarNetworkError(
                        "arXiv exact-ID response endpoint is not allowlisted",
                        classification="unverified_redirect_target",
                    )
                remaining = max_response_bytes - response_bytes
                content = response.read(remaining + 1)  # type: ignore[attr-defined]
        except (OSError, TimeoutError) as exc:
            raise ResearchRadarError(f"arXiv metadata fetch failed: {exc}") from exc
        response_bytes += len(content)
        if response_bytes > max_response_bytes:
            raise RadarValidationError("arXiv responses exceed the aggregate byte limit")
        receipt = _mint_live_receipt(
            provider="arxiv_atom_primary",
            request_locator=locator,
            final_locator=str(final_locator),
            response=content,
            expected_identifiers=page,
            source="network",
        )
        batches.append(
            SourceBatch(
                locator,
                content,
                tuple(page),
                provider="arxiv_atom_primary",
                primary_metadata_verified=True,
                _receipt=receipt,
            )
        )
    return batches


def read_atom_file(
    path: Path, *, max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
) -> SourceBatch:
    resolved = path.expanduser().resolve()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ResearchRadarError(f"cannot inspect Atom file {resolved}: {exc}") from exc
    if size > max_response_bytes:
        raise RadarValidationError("Atom file exceeds the configured byte limit")
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        raise ResearchRadarError(f"cannot read Atom file {resolved}: {exc}") from exc
    if len(content) > max_response_bytes:
        raise RadarValidationError("Atom file exceeds the configured byte limit")
    return SourceBatch(resolved.as_uri(), content)


def _safe_base_path(base_id: str) -> str:
    return urllib.parse.quote(base_id, safe=".")


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _lifecycle_write_lock(root: Path, identity: ArxivIdentity) -> Iterator[None]:
    directory = _lifecycle_directory(root, identity)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".transition.lock"
    process_lock = _process_lock(lock_path)
    if not process_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise RadarConflictError(f"timed out acquiring lifecycle lock: {identity.version_key}")
    stream = None
    locked = False
    try:
        stream = lock_path.open("a+b", buffering=0)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ResearchRadarError(f"cannot acquire lifecycle lock: {exc}") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RadarConflictError(
                        f"timed out acquiring lifecycle lock: {identity.version_key}"
                    ) from exc
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        try:
            if stream is not None and locked:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if stream is not None:
                stream.close()
            process_lock.release()


def _relative_paths(parsed: dict[str, object]) -> Iterable[tuple[str, str, dict[str, object]]]:
    for paper in parsed["papers"]:  # type: ignore[union-attr]
        base_id = str(paper["arxiv_base_id"])
        yield "paper", f"papers/{_safe_base_path(base_id)}/identity.json", paper
    for version in parsed["versions"]:  # type: ignore[union-attr]
        identity = version["identity"]
        base_id = str(identity["arxiv_base_id"])
        version_number = int(identity["arxiv_version"])
        yield (
            "paper_version",
            f"papers/{_safe_base_path(base_id)}/versions/v{version_number}.json",
            version,
        )
    observation = parsed["observation"]
    observation_id = str(observation["observation_id"])
    yield "source_observation", f"observations/arxiv/{observation_id}.json", observation


def _initial_lifecycle(version: dict[str, object]) -> dict[str, object]:
    identity = version["identity"]
    return {
        "schema": LIFECYCLE_SCHEMA,
        "paper_version_key": identity["version_key"],
        "sequence": 1,
        "previous_record_sha256": None,
        "from_status": None,
        "to_status": "new",
        "occurred_at": version["revised_at"],
        "owner": "research-radar-intake-v1",
        "reason": "Untrusted Atom metadata normalized into an immutable intake record.",
        "evidence": [version["artifact_integrity"]["atom_entry"]["sha256"]],
        "revisit_trigger": None,
        "verdict_class": None,
    }


def _metadata_verified_lifecycle(
    version: dict[str, object], previous_sha256: str
) -> dict[str, object]:
    identity = version["identity"]
    return {
        "schema": LIFECYCLE_SCHEMA,
        "paper_version_key": identity["version_key"],
        "sequence": 2,
        "previous_record_sha256": previous_sha256,
        "from_status": "new",
        "to_status": "metadata_verified",
        "occurred_at": version["revised_at"],
        "owner": "research-radar-arxiv-live-v1",
        "reason": "Exact-ID response matched primary arXiv metadata and version identity.",
        "evidence": [version["artifact_integrity"]["atom_entry"]["sha256"]],
        "revisit_trigger": None,
        "verdict_class": None,
    }


def plan_ingestion(
    root: Path,
    batches: Sequence[SourceBatch],
    *,
    max_entries_total: int = DEFAULT_MAX_IDS,
    max_response_bytes_total: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> _IngestionPlan:
    if not batches:
        raise RadarValidationError("at least one source batch is required")
    candidates: dict[str, tuple[str, bytes]] = {}
    version_candidates: dict[str, dict[str, object]] = {}
    primary_verified_versions: set[str] = set()
    authority_candidates: dict[str, _ObservationAuthority] = {}
    total_entries = 0
    total_bytes = sum(len(batch.content) for batch in batches)
    if total_bytes > max_response_bytes_total:
        raise RadarValidationError("Atom inputs exceed the aggregate byte limit")
    if len(batches) > max_entries_total:
        raise RadarValidationError("Atom input batch count exceeds the aggregate limit")
    for batch in batches:
        remaining = max_entries_total - total_entries
        if remaining <= 0:
            raise RadarValidationError("Atom entries exceed the aggregate limit")
        parsed = parse_arxiv_atom(batch, max_entries=remaining)
        total_entries += len(parsed["versions"])
        if total_entries > max_entries_total:
            raise RadarValidationError("Atom entries exceed the aggregate limit")
        for kind, relative_path, value in _relative_paths(parsed):
            content = _canonical_bytes(value)
            previous = candidates.get(relative_path)
            if previous is not None and previous[1] != content:
                raise RadarConflictError(f"conflicting candidate records for {relative_path}")
            candidates[relative_path] = (kind, content)
        for version in parsed["versions"]:  # type: ignore[union-attr]
            version_key = str(version["identity"]["version_key"])
            previous = version_candidates.get(version_key)
            if previous is not None and previous != version:
                raise RadarConflictError(f"conflicting version records for {version_key}")
            version_candidates[version_key] = version
            if _validated_batch_receipt(batch) is not None:
                primary_verified_versions.add(version_key)
        receipt = _validated_batch_receipt(batch)
        if receipt is not None:
            observation = parsed["observation"]
            observation_path = f"observations/arxiv/{str(observation['observation_id'])}.json"
            observation_content = _canonical_bytes(observation)
            version_entries = tuple(
                sorted(
                    (
                        str(entry["version_key"]),
                        str(entry["atom_entry_sha256"]),
                    )
                    for entry in observation["entries"]
                )
            )
            authority = _ObservationAuthority(
                observation_path=observation_path,
                observation_sha256=_sha256(observation_content),
                receipt=receipt,
                version_entries=version_entries,
            )
            previous_authority = authority_candidates.get(observation_path)
            if previous_authority is not None and previous_authority != authority:
                raise RadarConflictError(f"conflicting live authority for {observation_path}")
            authority_candidates[observation_path] = authority

    root = root.expanduser().resolve()
    planned: list[PlannedWrite] = []
    for relative_path in sorted(candidates):
        kind, content = candidates[relative_path]
        target = root / Path(relative_path)
        if target.exists():
            state = "unchanged" if target.read_bytes() == content else "conflict"
        else:
            state = "create"
        planned.append(PlannedWrite(kind, relative_path, content, state))

    conflicts = [item.relative_path for item in planned if item.state == "conflict"]
    if conflicts:
        raise RadarConflictError("immutable record conflict: " + ", ".join(sorted(conflicts)))

    for observation_path in sorted(authority_candidates):
        authority = authority_candidates[observation_path]
        observation_candidate = candidates.get(observation_path)
        if observation_candidate is None:
            raise RadarValidationError("live authority lacks a planned observation")
        observation_value = _json_bytes(
            observation_candidate[1], label="planned source observation"
        )
        if not isinstance(observation_value, dict):
            raise RadarValidationError("planned source observation must be an object")
        for version_key, entry_hash in authority.version_entries:
            receipt_record = _primary_receipt_record(
                authority,
                version_key=version_key,
                entry_hash=entry_hash,
                observation=observation_value,
            )
            receipt_content = _canonical_bytes(receipt_record)
            receipt_relative = _primary_receipt_path(receipt_record)
            receipt_target = root / Path(receipt_relative)
            receipt_state = (
                "unchanged"
                if receipt_target.exists() and receipt_target.read_bytes() == receipt_content
                else "create"
            )
            planned.append(
                PlannedWrite(
                    "primary_receipt",
                    receipt_relative,
                    receipt_content,
                    receipt_state,
                )
            )

    for version_key in sorted(version_candidates):
        version = version_candidates[version_key]
        event = _initial_lifecycle(version)
        content = _canonical_bytes(event)
        base_id = str(version["identity"]["arxiv_base_id"])
        version_number = int(version["identity"]["arxiv_version"])
        relative_path = (
            f"lifecycle/{_safe_base_path(base_id)}/v{version_number}/"
            f"0001-{hashlib.sha256(content).hexdigest()}.json"
        )
        target = root / Path(relative_path)
        existing_initial = sorted(target.parent.glob("0001-*.json"))
        if existing_initial and target not in existing_initial:
            raise RadarConflictError(f"immutable initial lifecycle conflict for {version_key}")
        state = "unchanged" if target.exists() and target.read_bytes() == content else "create"
        planned.append(PlannedWrite("lifecycle", relative_path, content, state))

        if version_key not in primary_verified_versions:
            continue
        identity = ArxivIdentity(base_id, version_number)
        records = (
            load_lifecycle(root, identity, allow_unresolved_primary=True) if target.exists() else []
        )
        if records and records[-1][1]["to_status"] != "new":
            continue
        previous_sha256 = records[-1][2] if records else _sha256(content)
        verified_event = _metadata_verified_lifecycle(version, previous_sha256)
        verified_content = _canonical_bytes(verified_event)
        verified_relative = (
            f"lifecycle/{_safe_base_path(base_id)}/v{version_number}/"
            f"0002-{hashlib.sha256(verified_content).hexdigest()}.json"
        )
        verified_target = root / Path(verified_relative)
        existing_verified = sorted(verified_target.parent.glob("0002-*.json"))
        if existing_verified and verified_target not in existing_verified:
            raise RadarConflictError(f"immutable metadata verification conflict for {version_key}")
        verified_state = (
            "unchanged"
            if verified_target.exists() and verified_target.read_bytes() == verified_content
            else "create"
        )
        planned.append(
            PlannedWrite("lifecycle", verified_relative, verified_content, verified_state)
        )

    return _IngestionPlan(
        sorted(planned, key=lambda item: (_WRITE_PRIORITY[item.kind], item.relative_path)),
        [authority_candidates[key] for key in sorted(authority_candidates)],
        token=_PLAN_AUTHORITY_TOKEN,
    )


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _planned_target(root: Path, relative_path: str) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
        or len(relative_path) > 1024
    ):
        raise RadarValidationError("planned write path is not canonical POSIX-relative text")
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative_path
        or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
    ):
        raise RadarValidationError("planned write path escapes the Radar root")
    expanded_root = root.expanduser().absolute()
    if expanded_root.exists() and _is_reparse_point(expanded_root):
        raise RadarValidationError("Research Radar root cannot be a symlink or junction")
    resolved_root = expanded_root.resolve()
    target = resolved_root.joinpath(*pure.parts)
    current = resolved_root
    for part in pure.parts:
        current = current / part
        if os.path.lexists(current) and _is_reparse_point(current):
            raise RadarValidationError(
                f"planned write path crosses a symlink or junction: {relative_path}"
            )
    try:
        target.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise RadarValidationError("planned write path resolves outside the Radar root") from exc
    return target


def _canonical_planned_json(item: PlannedWrite) -> dict[str, object]:
    value = _json_bytes(item.content, label=f"planned {item.kind}")
    if not isinstance(value, dict) or _canonical_bytes(value) != item.content:
        raise RadarValidationError(f"planned {item.kind} content is not canonical JSON")
    return value


def _validate_source_observation(value: dict[str, object]) -> None:
    required = {
        "schema",
        "observation_id",
        "provider",
        "source_locator",
        "response_sha256",
        "feed_updated_at",
        "entries",
        "pagination",
        "trust",
        "primary_metadata_verified",
    }
    if set(value) != required or value.get("schema") != SOURCE_OBSERVATION_SCHEMA:
        raise RadarValidationError("source observation schema or keys are invalid")
    provider = value.get("provider")
    if provider not in {
        "offline_atom_fixture",
        "arxiv_atom_primary",
        "arxiv_atom_discovery",
    }:
        raise RadarValidationError("source observation provider is invalid")
    locator = value.get("source_locator")
    response_sha256 = value.get("response_sha256")
    if not isinstance(locator, str) or not isinstance(response_sha256, str):
        raise RadarValidationError("source observation identity fields are invalid")
    if _SHA256_RE.fullmatch(response_sha256) is None:
        raise RadarValidationError("source observation response hash is invalid")
    expected_id = "arxiv-" + _digest_id(
        {
            "provider": provider,
            "source_locator": locator,
            "response_sha256": response_sha256,
        }
    )
    if value.get("observation_id") != expected_id:
        raise RadarValidationError("source observation content identity is invalid")
    primary = value.get("primary_metadata_verified")
    if not isinstance(primary, bool) or (primary and provider != "arxiv_atom_primary"):
        raise RadarValidationError("source observation primary-verification flag is invalid")
    feed_updated = value.get("feed_updated_at")
    if feed_updated is not None and _iso8601(feed_updated, "feed_updated_at") != feed_updated:
        raise RadarValidationError("source observation feed timestamp is not canonical")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise RadarValidationError("source observation entries are invalid")
    canonical_entries: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "version_key",
            "atom_entry_sha256",
        }:
            raise RadarValidationError("source observation entry keys are invalid")
        identity = _identity_from_version_key(entry.get("version_key"))
        digest = entry.get("atom_entry_sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise RadarValidationError("source observation entry hash is invalid")
        canonical_entries.append((identity.version_key, digest))
    if canonical_entries != sorted(set(canonical_entries)):
        raise RadarValidationError("source observation entries are not unique and sorted")
    pagination = value.get("pagination")
    if not isinstance(pagination, dict) or set(pagination) != {
        "start_index",
        "items_per_page",
        "total_results",
    }:
        raise RadarValidationError("source observation pagination is invalid")
    counts = [pagination[key] for key in ("start_index", "items_per_page", "total_results")]
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
        raise RadarValidationError("source observation pagination counts are invalid")
    trust = value.get("trust")
    if trust != {
        "classification": "untrusted_external_metadata",
        "execution_permitted": False,
    }:
        raise RadarValidationError("source observation trust contract is invalid")


def _validate_paper_version(value: dict[str, object]) -> ArxivIdentity:
    expected_keys = {
        "schema",
        "identity",
        "title",
        "authors",
        "submitted_at",
        "revised_at",
        "categories",
        "primary_category",
        "abstract",
        "license_uri",
        "doi",
        "journal_reference",
        "comment",
        "links",
        "lineage",
        "status_observations",
        "artifact_integrity",
        "trust",
    }
    if set(value) != expected_keys or value.get("schema") != PAPER_VERSION_SCHEMA:
        raise RadarValidationError("paper-version schema or keys are invalid")
    identity_value = value.get("identity")
    if not isinstance(identity_value, dict) or set(identity_value) != {
        "arxiv_base_id",
        "arxiv_version",
        "paper_key",
        "version_key",
    }:
        raise RadarValidationError("paper-version identity is invalid")
    identity = _identity_from_version_key(identity_value.get("version_key"))
    if identity_value != {
        "arxiv_base_id": identity.base_id,
        "arxiv_version": identity.version,
        "paper_key": identity.paper_key,
        "version_key": identity.version_key,
    }:
        raise RadarValidationError("paper-version identity fields disagree")
    entry_keys = expected_keys - {
        "schema",
        "lineage",
        "status_observations",
        "artifact_integrity",
        "trust",
    }
    entry_metadata = {key: value[key] for key in entry_keys}
    integrity = value.get("artifact_integrity")
    if not isinstance(integrity, dict):
        raise RadarValidationError("paper-version artifact integrity is invalid")
    atom_entry = integrity.get("atom_entry")
    if not isinstance(atom_entry, dict) or atom_entry != {
        "status": "normalized",
        "sha256": _sha256(_canonical_bytes(entry_metadata)),
    }:
        raise RadarValidationError("paper-version normalized entry hash is invalid")
    return identity


def _validate_planned_write(root: Path, item: PlannedWrite) -> tuple[Path, dict[str, object]]:
    if not isinstance(item, PlannedWrite) or item.state not in {"create", "unchanged"}:
        raise RadarValidationError("planned write kind or state is invalid")
    target = _planned_target(root, item.relative_path)
    value = _canonical_planned_json(item)
    if item.kind == "paper":
        if (
            set(value)
            != {
                "schema",
                "paper_key",
                "arxiv_base_id",
                "canonical_abs_uri",
                "identity_policy",
            }
            or value.get("schema") != PAPER_SCHEMA
        ):
            raise RadarValidationError("paper identity schema or keys are invalid")
        identity = parse_arxiv_id(str(value.get("arxiv_base_id")))
        if identity.version is not None or value != {
            "schema": PAPER_SCHEMA,
            "paper_key": identity.paper_key,
            "arxiv_base_id": identity.base_id,
            "canonical_abs_uri": f"https://arxiv.org/abs/{identity.base_id}",
            "identity_policy": "versionless_arxiv_id",
        }:
            raise RadarValidationError("paper identity content is invalid")
        expected = f"papers/{_safe_base_path(identity.base_id)}/identity.json"
    elif item.kind == "paper_version":
        identity = _validate_paper_version(value)
        expected = f"papers/{_safe_base_path(identity.base_id)}/versions/v{identity.version}.json"
    elif item.kind == "source_observation":
        _validate_source_observation(value)
        expected = f"observations/arxiv/{value['observation_id']}.json"
    elif item.kind == "primary_receipt":
        required = {
            "schema",
            "paper_version_key",
            "atom_entry_sha256",
            "observation_id",
            "observation_path",
            "observation_sha256",
            "provider",
            "request_locator",
            "final_locator",
            "response_sha256",
            "expected_identifiers",
            "source",
        }
        if set(value) != required or value.get("schema") != PRIMARY_RECEIPT_SCHEMA:
            raise RadarValidationError("primary receipt schema or keys are invalid")
        identity = _identity_from_version_key(value.get("paper_version_key"))
        for key in ("atom_entry_sha256", "observation_sha256", "response_sha256"):
            digest_value = value.get(key)
            if not isinstance(digest_value, str) or _SHA256_RE.fullmatch(digest_value) is None:
                raise RadarValidationError("primary receipt hash is invalid")
        expected_ids = value.get("expected_identifiers")
        if not isinstance(expected_ids, list):
            raise RadarValidationError("primary receipt expected IDs are invalid")
        canonical_ids = _canonical_expected_ids([str(identifier) for identifier in expected_ids])
        if list(canonical_ids) != expected_ids:
            raise RadarValidationError("primary receipt expected IDs are not canonical")
        if (
            value.get("provider") != "arxiv_atom_primary"
            or value.get("source") != "validated_network_or_daily_cache"
            or value.get("final_locator") != value.get("request_locator")
        ):
            raise RadarValidationError("primary receipt provenance is invalid")
        _validate_exact_arxiv_locator(str(value.get("request_locator")), canonical_ids)
        observation_id = value.get("observation_id")
        if (
            not isinstance(observation_id, str)
            or re.fullmatch(r"arxiv-[0-9a-f]{64}", observation_id) is None
        ):
            raise RadarValidationError("primary receipt observation ID is invalid")
        if value.get("observation_path") != f"observations/arxiv/{observation_id}.json":
            raise RadarValidationError("primary receipt observation path is invalid")
        expected = _primary_receipt_path(value)
    elif item.kind == "lifecycle":
        identity = _identity_from_version_key(value.get("paper_version_key"))
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise RadarValidationError("lifecycle plan sequence is invalid")
        digest = hashlib.sha256(item.content).hexdigest()
        expected = (
            f"lifecycle/{_safe_base_path(identity.base_id)}/v{identity.version}/"
            f"{sequence:04d}-{digest}.json"
        )
    elif item.kind == "artifact":
        if value.get("schema") != ARTIFACT_OBSERVATION_SCHEMA:
            raise RadarValidationError("artifact observation schema is invalid")
        identity = _identity_from_version_key(value.get("paper_version_key"))
        provider = value.get("provider")
        artifact_id = value.get("artifact_id")
        if provider not in {"github", "huggingface"} or not isinstance(artifact_id, str):
            raise RadarValidationError("artifact observation identity is invalid")
        identity_hash = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()[:20]
        digest = hashlib.sha256(item.content).hexdigest()
        expected = (
            f"artifacts/{provider}/{_safe_base_path(identity.base_id)}/v{identity.version}/"
            f"{identity_hash}-{digest}.json"
        )
    elif item.kind == "triage":
        if value.get("schema") != TRIAGE_SCHEMA:
            raise RadarValidationError("triage schema is invalid")
        identity = _identity_from_version_key(value.get("paper_version_key"))
        digest = hashlib.sha256(item.content).hexdigest()
        expected = f"triage/{_safe_base_path(identity.base_id)}/v{identity.version}/{digest}.json"
    elif item.kind == "sync_run":
        if value.get("schema") != SYNC_RUN_SCHEMA or not isinstance(value.get("run_id"), str):
            raise RadarValidationError("sync-run schema or identity is invalid")
        run_id = str(value["run_id"])
        if re.fullmatch(r"radar-[0-9a-f]{64}", run_id) is None:
            raise RadarValidationError("sync-run ID is invalid")
        digest = hashlib.sha256(item.content).hexdigest()
        expected = f"sync/runs/{run_id}-{digest}.json"
    else:
        raise RadarValidationError(f"unsupported planned write kind: {item.kind}")
    if item.relative_path != expected:
        raise RadarValidationError(f"planned {item.kind} path does not match its content identity")
    if target.exists():
        if not target.is_file():
            raise RadarConflictError("planned write target is not an immutable file")
        if target.read_bytes() != item.content:
            raise RadarConflictError(f"immutable record conflict: {target}")
    return target, value


def _write_immutable(target: Path, content: bytes) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != content:
            raise RadarConflictError(f"immutable record conflict: {target}")
        return "unchanged"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".radar-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise RadarConflictError(f"immutable record conflict: {target}")
            return "unchanged"
        return "created"
    finally:
        temporary.unlink(missing_ok=True)


def _plan_primary_authority(
    plan: Sequence[PlannedWrite],
    values_by_path: Mapping[str, dict[str, object]],
) -> dict[str, str]:
    if not isinstance(plan, _IngestionPlan) or plan._token is not _PLAN_AUTHORITY_TOKEN:
        return {}
    authorized: dict[str, str] = {}
    writes_by_path = {item.relative_path: item for item in plan}
    for authority in plan._authority:
        receipt = authority.receipt
        if receipt._token is not _LIVE_RECEIPT_TOKEN:
            raise RadarValidationError("ingestion plan contains an invalid live receipt")
        item = writes_by_path.get(authority.observation_path)
        observation = values_by_path.get(authority.observation_path)
        if item is None or observation is None or item.kind != "source_observation":
            raise RadarValidationError("ingestion plan live authority lacks its observation")
        if item.sha256 != authority.observation_sha256:
            raise RadarValidationError("ingestion plan observation authority hash mismatch")
        _validate_source_observation(observation)
        observed_entries = tuple(
            sorted(
                (
                    str(entry["version_key"]),
                    str(entry["atom_entry_sha256"]),
                )
                for entry in observation["entries"]  # type: ignore[union-attr]
            )
        )
        if (
            observation.get("provider") != "arxiv_atom_primary"
            or observation.get("primary_metadata_verified") is not True
            or observation.get("source_locator") != receipt.request_locator
            or observation.get("response_sha256") != receipt.response_sha256
            or receipt.final_locator != receipt.request_locator
            or observed_entries != authority.version_entries
        ):
            raise RadarValidationError("ingestion plan live authority is not observation-bound")
        _validate_exact_arxiv_locator(receipt.request_locator, receipt.expected_identifiers)
        expected = [parse_arxiv_id(item) for item in receipt.expected_identifiers]
        returned = [
            _identity_from_version_key(version_key) for version_key, _entry_hash in observed_entries
        ]
        for requested in expected:
            matches = [
                item
                for item in returned
                if item.base_id == requested.base_id
                and (requested.version is None or item.version == requested.version)
            ]
            if len(matches) != 1:
                raise RadarValidationError(
                    "ingestion plan receipt does not uniquely bind every requested ID"
                )
        for returned_identity in returned:
            if not any(
                item.base_id == returned_identity.base_id
                and (item.version is None or item.version == returned_identity.version)
                for item in expected
            ):
                raise RadarValidationError("ingestion plan authority contains an unexpected ID")
        for version_key, entry_hash in observed_entries:
            expected_receipt = _primary_receipt_record(
                authority,
                version_key=version_key,
                entry_hash=entry_hash,
                observation=observation,
            )
            receipt_path = _primary_receipt_path(expected_receipt)
            receipt_item = writes_by_path.get(receipt_path)
            receipt_value = values_by_path.get(receipt_path)
            if (
                receipt_item is None
                or receipt_item.kind != "primary_receipt"
                or receipt_value != expected_receipt
            ):
                raise RadarValidationError(
                    "ingestion plan live authority lacks its canonical persisted receipt"
                )
            previous = authorized.get(version_key)
            if previous is not None and previous != entry_hash:
                raise RadarConflictError(f"conflicting primary authority for {version_key}")
            authorized[version_key] = entry_hash
    return authorized


def _primary_receipt_record(
    authority: _ObservationAuthority,
    *,
    version_key: str,
    entry_hash: str,
    observation: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": PRIMARY_RECEIPT_SCHEMA,
        "paper_version_key": version_key,
        "atom_entry_sha256": entry_hash,
        "observation_id": observation["observation_id"],
        "observation_path": authority.observation_path,
        "observation_sha256": authority.observation_sha256,
        "provider": authority.receipt.provider,
        "request_locator": authority.receipt.request_locator,
        "final_locator": authority.receipt.final_locator,
        "response_sha256": authority.receipt.response_sha256,
        "expected_identifiers": list(authority.receipt.expected_identifiers),
        "source": "validated_network_or_daily_cache",
    }


def _primary_receipt_path(record: Mapping[str, object]) -> str:
    identity = _identity_from_version_key(record.get("paper_version_key"))
    observation_id = str(record.get("observation_id"))
    return (
        f"authority/arxiv/{_safe_base_path(identity.base_id)}/v{identity.version}/"
        f"{observation_id}.json"
    )


def _preflight_lifecycle_plan(
    root: Path,
    items: Sequence[PlannedWrite],
    values_by_path: Mapping[str, dict[str, object]],
    authorized_entries: Mapping[str, str],
) -> None:
    grouped: dict[str, list[PlannedWrite]] = {}
    identities: dict[str, ArxivIdentity] = {}
    for item in items:
        if item.kind != "lifecycle":
            continue
        value = values_by_path[item.relative_path]
        identity = _identity_from_version_key(value.get("paper_version_key"))
        identities[identity.version_key] = identity
        grouped.setdefault(identity.version_key, []).append(item)
    for version_key in sorted(grouped):
        identity = identities[version_key]
        migration_authority = authorized_entries.get(version_key)
        existing = load_lifecycle(
            root,
            identity,
            allow_unresolved_primary=migration_authority is not None,
        )
        if migration_authority is not None and len(existing) >= 2:
            version_relative = (
                f"papers/{_safe_base_path(identity.base_id)}/versions/v{identity.version}.json"
            )
            version_value = values_by_path.get(version_relative)
            if version_value is None:
                loaded = _json_bytes(
                    _planned_target(root, version_relative).read_bytes(),
                    label="immutable paper version",
                )
                version_value = loaded if isinstance(loaded, dict) else None
            expected_migration = (
                _metadata_verified_lifecycle(version_value, existing[0][2])
                if version_value is not None
                else None
            )
            if existing[1][1] != expected_migration or existing[1][1].get("evidence") != [
                migration_authority
            ]:
                raise RadarValidationError(
                    "legacy primary lifecycle does not match the live migration receipt"
                )
        simulated = list(existing)
        existing_paths = {path.resolve(): path.read_bytes() for path, _value, _sha in existing}
        for item in sorted(
            grouped[version_key],
            key=lambda candidate: int(values_by_path[candidate.relative_path]["sequence"]),
        ):
            target = _planned_target(root, item.relative_path)
            existing_content = existing_paths.get(target.resolve())
            if existing_content is not None:
                if existing_content != item.content:
                    raise RadarConflictError("planned lifecycle conflicts with an existing event")
                continue
            value = values_by_path[item.relative_path]
            sequence = len(simulated) + 1
            previous = simulated[-1] if simulated else None
            evidence = value.get("evidence")
            authority_hash = authorized_entries.get(identity.version_key)
            version_relative = (
                f"papers/{_safe_base_path(identity.base_id)}/versions/v{identity.version}.json"
            )
            version_value = values_by_path.get(version_relative)
            if version_value is None:
                version_path = _planned_target(root, version_relative)
                if version_path.is_file():
                    loaded = _json_bytes(version_path.read_bytes(), label="immutable paper version")
                    version_value = loaded if isinstance(loaded, dict) else None
            expected_primary = (
                _metadata_verified_lifecycle(
                    version_value,
                    previous[2] if previous else "",
                )
                if version_value is not None and previous is not None
                else None
            )
            allow_primary = bool(
                value.get("from_status") == "new"
                and value.get("to_status") == "metadata_verified"
                and isinstance(evidence, list)
                and authority_hash is not None
                and evidence == [authority_hash]
                and expected_primary == value
            )
            status, occurred_at = _validate_lifecycle_value(
                value,
                identity,
                expected_sequence=sequence,
                previous_sha256=previous[2] if previous else None,
                previous_status=str(previous[1]["to_status"]) if previous else None,
                previous_occurred_at=(
                    datetime.fromisoformat(str(previous[1]["occurred_at"]).replace("Z", "+00:00"))
                    if previous
                    else None
                ),
                allow_primary_verification=allow_primary,
                label=item.relative_path,
            )
            _ = status, occurred_at
            simulated.append((target, value, _sha256(item.content)))


def apply_plan(root: Path, plan: Sequence[PlannedWrite]) -> dict[str, int]:
    items = tuple(plan)
    if not items:
        return {"created": 0, "unchanged": 0}
    if len({item.relative_path for item in items}) != len(items):
        raise RadarValidationError("ingestion plan contains duplicate target paths")
    values_by_path: dict[str, dict[str, object]] = {}
    for item in items:
        if item.kind not in {
            "paper",
            "paper_version",
            "source_observation",
            "primary_receipt",
            "lifecycle",
        }:
            raise RadarValidationError("ingestion plan contains an unsupported write kind")
        _target, value = _validate_planned_write(root, item)
        values_by_path[item.relative_path] = value
    authorized_entries = _plan_primary_authority(plan, values_by_path)
    bound_observations = (
        {authority.observation_path: authority.observation_sha256 for authority in plan._authority}
        if isinstance(plan, _IngestionPlan) and plan._token is _PLAN_AUTHORITY_TOKEN
        else {}
    )
    for item in items:
        if item.kind != "source_observation":
            continue
        observation = values_by_path[item.relative_path]
        if (
            observation.get("primary_metadata_verified") is True
            and bound_observations.get(item.relative_path) != item.sha256
        ):
            raise RadarValidationError("primary source observation lacks bound live-plan authority")
    for item in items:
        if item.kind != "primary_receipt":
            continue
        receipt_value = values_by_path[item.relative_path]
        version_key = str(receipt_value["paper_version_key"])
        if authorized_entries.get(version_key) != receipt_value.get("atom_entry_sha256"):
            raise RadarValidationError("primary receipt write lacks bound live-plan authority")
    _preflight_lifecycle_plan(root, items, values_by_path, authorized_entries)
    created = 0
    unchanged = 0
    for item in sorted(items, key=lambda value: (_WRITE_PRIORITY[value.kind], value.relative_path)):
        target, _value = _validate_planned_write(root, item)
        if item.kind == "lifecycle":
            state = _apply_lifecycle_write(
                root,
                item,
                authorized_primary_entries=authorized_entries,
            )
        else:
            state = _write_immutable(target, item.content)
        created += state == "created"
        unchanged += state == "unchanged"
    return {"created": created, "unchanged": unchanged}


def _identity_from_version_key(value: object) -> ArxivIdentity:
    if not isinstance(value, str) or not value.startswith("arxiv:"):
        raise RadarValidationError("invalid lifecycle paper version key")
    identifier = value.split("arxiv:", 1)[1].replace(":v", "v")
    return parse_arxiv_id(identifier, require_version=True)


def _validate_lifecycle_value(
    value: object,
    identity: ArxivIdentity,
    *,
    expected_sequence: int,
    previous_sha256: str | None,
    previous_status: str | None,
    previous_occurred_at: datetime | None,
    allow_primary_verification: bool,
    label: str,
) -> tuple[str, datetime]:
    if not isinstance(value, dict) or set(value) != _LIFECYCLE_KEYS:
        raise RadarValidationError(f"lifecycle record keys are not canonical: {label}")
    if value.get("schema") != LIFECYCLE_SCHEMA:
        raise RadarValidationError(f"unsupported lifecycle record schema: {label}")
    if value.get("sequence") != expected_sequence:
        raise RadarValidationError(f"lifecycle sequence mismatch: {label}")
    if value.get("previous_record_sha256") != previous_sha256:
        raise RadarValidationError(f"lifecycle hash chain mismatch: {label}")
    if value.get("paper_version_key") != identity.version_key:
        raise RadarValidationError(f"lifecycle paper identity mismatch: {label}")
    from_status = value.get("from_status")
    to_status = value.get("to_status")
    if to_status not in LIFECYCLE_STATUSES:
        raise RadarValidationError(f"unknown lifecycle status: {label}")
    if expected_sequence == 1:
        if from_status is not None or to_status != "new":
            raise RadarValidationError(f"initial lifecycle must establish new: {label}")
    else:
        if from_status not in LIFECYCLE_STATUSES:
            raise RadarValidationError(f"unknown lifecycle status: {label}")
        if to_status not in ALLOWED_TRANSITIONS.get(str(from_status), frozenset()):
            raise RadarValidationError(f"invalid lifecycle transition in {label}")
        if (
            from_status == "new"
            and to_status == "metadata_verified"
            and not allow_primary_verification
        ):
            raise RadarValidationError(
                f"primary metadata verification lacks live exact-ID authority: {label}"
            )
        if to_status in _GOVERNED_EXPERIMENT_RESULTS or (
            from_status == "experiment_candidate" and to_status == "rejected"
        ):
            raise RadarValidationError(
                f"governed experiment result is unsupported by Radar v1: {label}"
            )
        if from_status != previous_status:
            raise RadarValidationError(f"lifecycle status chain mismatch: {label}")
    canonical_occurred_at = _iso8601(value.get("occurred_at"), "lifecycle occurred_at")
    if value.get("occurred_at") != canonical_occurred_at:
        raise RadarValidationError(f"lifecycle occurred_at is not canonical: {label}")
    occurred_at = datetime.fromisoformat(canonical_occurred_at.replace("Z", "+00:00"))
    if previous_occurred_at is not None and occurred_at < previous_occurred_at:
        raise RadarValidationError(f"lifecycle time moved backwards: {label}")
    owner = _clean_text(value.get("owner"), required=True, label="lifecycle owner")
    reason = _clean_text(value.get("reason"), required=True, label="lifecycle reason")
    if value.get("owner") != owner or value.get("reason") != reason:
        raise RadarValidationError(f"lifecycle text is not canonical: {label}")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) or not _SHA256_RE.fullmatch(item) for item in evidence
    ):
        raise RadarValidationError(f"lifecycle evidence must be sha256 digests: {label}")
    if evidence != sorted(set(evidence)):
        raise RadarValidationError(f"lifecycle evidence is not canonical: {label}")
    revisit = value.get("revisit_trigger")
    clean_revisit = _clean_text(revisit) if revisit is not None else None
    if revisit != clean_revisit:
        raise RadarValidationError(f"invalid lifecycle revisit trigger: {label}")
    if to_status in _REVISIT_REQUIRED and revisit is None:
        raise RadarValidationError(f"missing lifecycle revisit trigger: {label}")
    if to_status in _EVIDENCE_REQUIRED and not evidence:
        raise RadarValidationError(f"missing lifecycle evidence: {label}")
    expected_verdict = (
        to_status
        if to_status
        in {
            "accepted",
            "rejected",
            "inconclusive",
            "evaluator_invalid",
            "runtime_blocked",
        }
        else None
    )
    if value.get("verdict_class") != expected_verdict:
        raise RadarValidationError(f"lifecycle verdict classification mismatch: {label}")
    return str(to_status), occurred_at


def _apply_lifecycle_write(
    root: Path,
    planned: PlannedWrite,
    *,
    expected_from: str | None = None,
    authorized_primary_entries: Mapping[str, str] | None = None,
) -> str:
    if planned.kind != "lifecycle" or planned.state not in {"create", "unchanged"}:
        raise RadarValidationError("plan is not an immutable lifecycle write")
    try:
        value = json.loads(planned.content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RadarValidationError("invalid planned lifecycle JSON") from exc
    identity = _identity_from_version_key(value.get("paper_version_key"))
    _validate_planned_write(root, planned)
    with _lifecycle_write_lock(root, identity):
        records = load_lifecycle(root, identity)
        current = str(records[-1][1]["to_status"]) if records else None
        if expected_from is not None and current != expected_from:
            raise RadarConflictError(
                f"stale lifecycle transition: expected {expected_from}, current is {current}"
            )
        target = _planned_target(root, planned.relative_path)
        if target.parent != _lifecycle_directory(root, identity):
            raise RadarValidationError("lifecycle plan path does not match its paper identity")
        if target.exists():
            return _write_immutable(target, planned.content)
        sequence = len(records) + 1
        digest = hashlib.sha256(planned.content).hexdigest()
        if target.name != f"{sequence:04d}-{digest}.json":
            raise RadarValidationError("lifecycle plan filename does not match its content")
        planned_evidence = value.get("evidence")
        authority_hash = (
            authorized_primary_entries.get(identity.version_key)
            if authorized_primary_entries is not None
            else None
        )
        version_path = _planned_target(
            root,
            f"papers/{_safe_base_path(identity.base_id)}/versions/v{identity.version}.json",
        )
        version_value = (
            _json_bytes(version_path.read_bytes(), label="immutable paper version")
            if version_path.is_file()
            else None
        )
        expected_primary = (
            _metadata_verified_lifecycle(version_value, records[-1][2])
            if isinstance(version_value, dict) and records
            else None
        )
        allow_primary_verification = (
            value.get("from_status") == "new"
            and value.get("to_status") == "metadata_verified"
            and isinstance(planned_evidence, list)
            and authority_hash is not None
            and planned_evidence == [authority_hash]
            and expected_primary == value
        )
        _validate_lifecycle_value(
            value,
            identity,
            expected_sequence=sequence,
            previous_sha256=records[-1][2] if records else None,
            previous_status=current,
            previous_occurred_at=(
                datetime.fromisoformat(str(records[-1][1]["occurred_at"]).replace("Z", "+00:00"))
                if records
                else None
            ),
            allow_primary_verification=allow_primary_verification,
            label=planned.relative_path,
        )
        state = _write_immutable(target, planned.content)
        load_lifecycle(root, identity)
        return state


def _lifecycle_directory(root: Path, identity: ArxivIdentity) -> Path:
    if identity.version is None:
        raise RadarValidationError("lifecycle operations require a versioned arXiv ID")
    return _planned_target(
        root,
        f"lifecycle/{_safe_base_path(identity.base_id)}/v{identity.version}",
    )


def _has_persisted_primary_authority(root: Path, identity: ArxivIdentity, entry_hash: str) -> bool:
    directory_relative = f"authority/arxiv/{_safe_base_path(identity.base_id)}/v{identity.version}"
    directory = _planned_target(root, directory_relative)
    if not directory.exists():
        return False
    receipt_paths = sorted(directory.glob("*.json"))
    if len(receipt_paths) > 100:
        raise RadarValidationError("primary authority receipt count exceeds the bound")
    for receipt_path in receipt_paths:
        relative = receipt_path.relative_to(root.expanduser().resolve()).as_posix()
        content = receipt_path.read_bytes()
        planned = PlannedWrite("primary_receipt", relative, content, "unchanged")
        _target, receipt = _validate_planned_write(root, planned)
        if (
            receipt.get("paper_version_key") != identity.version_key
            or receipt.get("atom_entry_sha256") != entry_hash
        ):
            continue
        observation_path = str(receipt["observation_path"])
        observation_target = _planned_target(root, observation_path)
        if not observation_target.is_file():
            continue
        observation_content = observation_target.read_bytes()
        if _sha256(observation_content) != receipt.get("observation_sha256"):
            continue
        observation_item = PlannedWrite(
            "source_observation", observation_path, observation_content, "unchanged"
        )
        _observation_target, observation = _validate_planned_write(root, observation_item)
        entries = observation.get("entries")
        if (
            observation.get("provider") == "arxiv_atom_primary"
            and observation.get("primary_metadata_verified") is True
            and observation.get("source_locator") == receipt.get("request_locator")
            and observation.get("response_sha256") == receipt.get("response_sha256")
            and isinstance(entries, list)
            and {
                "version_key": identity.version_key,
                "atom_entry_sha256": entry_hash,
            }
            in entries
        ):
            return True
    return False


def load_lifecycle(
    root: Path,
    identity: ArxivIdentity,
    *,
    allow_unresolved_primary: bool = False,
) -> list[tuple[Path, dict[str, object], str]]:
    directory = _lifecycle_directory(root, identity)
    version_path = (
        root.expanduser().resolve()
        / "papers"
        / _safe_base_path(identity.base_id)
        / "versions"
        / f"v{identity.version}.json"
    )
    if directory.exists() and not version_path.is_file():
        raise RadarValidationError(
            f"lifecycle exists without immutable paper version: {identity.version_key}"
        )
    records: list[tuple[Path, dict[str, object], str]] = []
    previous_sha256: str | None = None
    previous_status: str | None = None
    previous_occurred_at: datetime | None = None
    for expected_sequence, path in enumerate(sorted(directory.glob("*.json")), start=1):
        try:
            content = path.read_bytes()
            loaded = _json_bytes(content, label=f"lifecycle record {path}")
            if not isinstance(loaded, dict):
                raise RadarValidationError(f"lifecycle record is not an object: {path}")
            value = loaded
        except OSError as exc:
            raise RadarValidationError(f"invalid lifecycle record {path}: {exc}") from exc
        digest = _sha256(content)
        expected_name = f"{expected_sequence:04d}-{digest.removeprefix('sha256:')}.json"
        if path.name != expected_name:
            raise RadarValidationError(f"lifecycle record name/hash mismatch: {path.name}")
        evidence = value.get("evidence") if isinstance(value, dict) else None
        primary_authority = bool(
            isinstance(value, dict)
            and value.get("from_status") == "new"
            and value.get("to_status") == "metadata_verified"
            and isinstance(evidence, list)
            and len(evidence) == 1
            and isinstance(evidence[0], str)
            and (
                allow_unresolved_primary
                or _has_persisted_primary_authority(root, identity, evidence[0])
            )
        )
        to_status, occurred_at = _validate_lifecycle_value(
            value,
            identity,
            expected_sequence=expected_sequence,
            previous_sha256=previous_sha256,
            previous_status=previous_status,
            previous_occurred_at=previous_occurred_at,
            allow_primary_verification=primary_authority,
            label=str(path),
        )
        previous_sha256 = digest
        previous_status = to_status
        previous_occurred_at = occurred_at
        records.append((path, value, digest))
    return records


def plan_transition(
    root: Path,
    identifier: str,
    *,
    expected_from: str,
    to_status: str,
    owner: str,
    reason: str,
    evidence: Sequence[str],
    revisit_trigger: str | None,
    occurred_at: str,
) -> PlannedWrite:
    identity = parse_arxiv_id(identifier, require_version=True)
    if expected_from not in LIFECYCLE_STATUSES or to_status not in LIFECYCLE_STATUSES:
        raise RadarValidationError("unknown lifecycle status")
    if to_status not in ALLOWED_TRANSITIONS.get(expected_from, frozenset()):
        raise RadarValidationError(f"invalid lifecycle transition: {expected_from} -> {to_status}")
    if expected_from == "new" and to_status == "metadata_verified":
        raise RadarValidationError(
            "new -> metadata_verified is reserved for a matching live primary-arXiv intake"
        )
    owner = _clean_text(owner, required=True, label="owner") or ""
    reason = _clean_text(reason, required=True, label="reason") or ""
    revisit = _clean_text(revisit_trigger)
    clean_evidence = sorted(
        {_clean_text(item, required=True, label="evidence") for item in evidence}
    )
    if any(not _SHA256_RE.fullmatch(item) for item in clean_evidence):
        raise RadarValidationError("transition evidence must be canonical sha256 digests")
    if to_status in _GOVERNED_EXPERIMENT_RESULTS or (
        expected_from == "experiment_candidate" and to_status == "rejected"
    ):
        raise RadarValidationError(
            f"{expected_from} -> {to_status} requires a governed experiment capsule, "
            "result artifact, budgets, and independent verifier not implemented in Radar v1"
        )
    if to_status in _REVISIT_REQUIRED and revisit is None:
        raise RadarValidationError(f"{to_status} requires a revisit trigger")
    if to_status in _EVIDENCE_REQUIRED and not clean_evidence:
        raise RadarValidationError(f"{to_status} requires evidence")
    records = load_lifecycle(root, identity)
    if not records:
        raise RadarValidationError(f"no ingested lifecycle exists for {identity.version_key}")
    current = str(records[-1][1]["to_status"])
    if current != expected_from:
        raise RadarConflictError(
            f"stale lifecycle transition: expected {expected_from}, current is {current}"
        )
    timestamp = _iso8601(occurred_at, "occurred_at")
    previous_time = datetime.fromisoformat(
        str(records[-1][1]["occurred_at"]).replace("Z", "+00:00")
    )
    next_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if next_time < previous_time:
        raise RadarValidationError("lifecycle occurred_at cannot move backwards")
    verdict_class = (
        to_status
        if to_status
        in {
            "accepted",
            "rejected",
            "inconclusive",
            "evaluator_invalid",
            "runtime_blocked",
        }
        else None
    )
    sequence = len(records) + 1
    event = {
        "schema": LIFECYCLE_SCHEMA,
        "paper_version_key": identity.version_key,
        "sequence": sequence,
        "previous_record_sha256": records[-1][2],
        "from_status": expected_from,
        "to_status": to_status,
        "occurred_at": timestamp,
        "owner": owner,
        "reason": reason,
        "evidence": clean_evidence,
        "revisit_trigger": revisit,
        "verdict_class": verdict_class,
    }
    content = _canonical_bytes(event)
    relative = (
        f"lifecycle/{_safe_base_path(identity.base_id)}/v{identity.version}/"
        f"{sequence:04d}-{hashlib.sha256(content).hexdigest()}.json"
    )
    return PlannedWrite("lifecycle", relative, content, "create")


def apply_transition(root: Path, planned: PlannedWrite, *, expected_from: str) -> str:
    if planned.kind != "lifecycle" or planned.state != "create":
        raise RadarValidationError("transition plan is not an immutable lifecycle create")
    _target, value = _validate_planned_write(root, planned)
    _preflight_lifecycle_plan(
        root,
        [planned],
        {planned.relative_path: value},
        {},
    )
    return _apply_lifecycle_write(root, planned, expected_from=expected_from)


def _strict_object(
    value: object, *, label: str, required_keys: frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RadarValidationError(f"{label} must be an object")
    missing = sorted(required_keys - set(value))
    unknown = sorted(set(value) - required_keys)
    if missing:
        raise RadarValidationError(f"{label} is missing keys: {', '.join(missing)}")
    if unknown:
        raise RadarValidationError(f"{label} has unknown keys: {', '.join(unknown)}")
    return value


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RadarValidationError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise RadarValidationError(f"{label} must be between {minimum} and {maximum}")
    return value


def _bounded_float(value: object, *, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RadarValidationError(f"{label} must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise RadarValidationError(f"{label} must be between {minimum} and {maximum}")
    return result


def _text_list(
    value: object,
    *,
    label: str,
    minimum: int = 1,
    maximum: int = 64,
    casefold: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise RadarValidationError(
            f"{label} must be a list containing between {minimum} and {maximum} values"
        )
    cleaned: list[str] = []
    for index, item in enumerate(value):
        text_value = _clean_text(item, required=True, label=f"{label}[{index}]") or ""
        if len(text_value) > 128:
            raise RadarValidationError(f"{label}[{index}] exceeds 128 characters")
        cleaned.append(text_value.casefold() if casefold else text_value)
    if len(set(cleaned)) != len(cleaned):
        raise RadarValidationError(f"{label} contains duplicate values")
    return tuple(sorted(cleaned))


def _valid_discovery_term(value: str) -> bool:
    return bool(
        _DISCOVERY_TERM_RE.fullmatch(value) and value.upper() not in {"AND", "OR", "ANDNOT"}
    )


def _term_groups(value: object, *, label: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise RadarValidationError(f"{label} must contain between 1 and 32 named groups")
    groups: list[tuple[str, tuple[str, ...]]] = []
    for raw_name, raw_terms in value.items():
        name = _clean_text(raw_name, required=True, label=f"{label} name") or ""
        if len(name) > 64:
            raise RadarValidationError(f"{label} group name exceeds 64 characters")
        groups.append((name, _text_list(raw_terms, label=f"{label}.{name}", maximum=32)))
    return tuple(sorted(groups))


def load_sync_config(path: Path) -> RadarSyncConfig:
    resolved = path.expanduser().resolve()
    try:
        raw = _json_bytes(resolved.read_bytes(), label="Research Radar sync config")
    except OSError as exc:
        raise ResearchRadarError(f"cannot load Research Radar sync config: {exc}") from exc
    config = _strict_object(
        raw,
        label="sync config",
        required_keys=frozenset(
            {
                "schema",
                "categories",
                "discovery_terms",
                "triage",
                "budgets",
                "cadence",
                "verify_artifacts",
            }
        ),
    )
    if config["schema"] != SYNC_CONFIG_SCHEMA:
        raise RadarValidationError("unexpected Research Radar sync config schema")
    categories = _text_list(config["categories"], label="categories", maximum=16, casefold=False)
    if any(_CATEGORY_RE.fullmatch(category) is None for category in categories):
        raise RadarValidationError("categories contain an invalid arXiv category")
    discovery_terms = _text_list(
        config["discovery_terms"],
        label="discovery_terms",
        maximum=32,
        casefold=False,
    )
    if any(not _valid_discovery_term(term) for term in discovery_terms):
        raise RadarValidationError("discovery_terms contain an unsafe arXiv query term")
    triage_raw = _strict_object(
        config["triage"],
        label="triage",
        required_keys=frozenset(
            {
                "profile_id",
                "active_questions",
                "observed_failures",
                "mutable_layers",
                "supported_categories",
            }
        ),
    )
    profile_id = (
        _clean_text(triage_raw["profile_id"], required=True, label="triage.profile_id") or ""
    )
    if len(profile_id) > 128:
        raise RadarValidationError("triage.profile_id exceeds 128 characters")
    supported = _text_list(
        triage_raw["supported_categories"],
        label="triage.supported_categories",
        maximum=32,
        casefold=False,
    )
    if any(_CATEGORY_RE.fullmatch(category) is None for category in supported):
        raise RadarValidationError("triage supported categories contain an invalid value")
    triage = TriageProfile(
        profile_id=profile_id,
        active_questions=_term_groups(
            triage_raw["active_questions"], label="triage.active_questions"
        ),
        observed_failures=_term_groups(
            triage_raw["observed_failures"], label="triage.observed_failures"
        ),
        mutable_layers=_term_groups(triage_raw["mutable_layers"], label="triage.mutable_layers"),
        supported_categories=supported,
    )
    budgets = _strict_object(
        config["budgets"],
        label="budgets",
        required_keys=frozenset(
            {
                "max_requests",
                "max_response_bytes",
                "max_items",
                "max_discovery_items",
                "max_frontier_items",
                "max_backlog_items",
                "max_recheck_items",
                "max_artifacts",
                "timeout_seconds",
                "max_wall_seconds",
                "retry_count",
            }
        ),
    )
    if isinstance(budgets["retry_count"], bool) or budgets["retry_count"] != 0:
        raise RadarValidationError("Research Radar v1 retry_count must be exactly zero")
    cadence = _strict_object(
        config["cadence"],
        label="cadence",
        required_keys=frozenset({"discovery_lookback_hours", "version_recheck_days"}),
    )
    verify_artifacts = config["verify_artifacts"]
    if not isinstance(verify_artifacts, bool):
        raise RadarValidationError("verify_artifacts must be boolean")
    result = RadarSyncConfig(
        categories=categories,
        discovery_terms=discovery_terms,
        triage=triage,
        max_requests=_bounded_int(
            budgets["max_requests"], label="budgets.max_requests", minimum=1, maximum=64
        ),
        max_response_bytes=_bounded_int(
            budgets["max_response_bytes"],
            label="budgets.max_response_bytes",
            minimum=1024,
            maximum=64 * 1024 * 1024,
        ),
        max_items=_bounded_int(
            budgets["max_items"], label="budgets.max_items", minimum=1, maximum=200
        ),
        max_discovery_items=_bounded_int(
            budgets["max_discovery_items"],
            label="budgets.max_discovery_items",
            minimum=1,
            maximum=100,
        ),
        max_frontier_items=_bounded_int(
            budgets["max_frontier_items"],
            label="budgets.max_frontier_items",
            minimum=1,
            maximum=100,
        ),
        max_backlog_items=_bounded_int(
            budgets["max_backlog_items"],
            label="budgets.max_backlog_items",
            minimum=1,
            maximum=100,
        ),
        max_recheck_items=_bounded_int(
            budgets["max_recheck_items"],
            label="budgets.max_recheck_items",
            minimum=1,
            maximum=100,
        ),
        max_artifacts=_bounded_int(
            budgets["max_artifacts"],
            label="budgets.max_artifacts",
            minimum=0,
            maximum=32,
        ),
        timeout_seconds=_bounded_float(
            budgets["timeout_seconds"],
            label="budgets.timeout_seconds",
            minimum=1,
            maximum=60,
        ),
        max_wall_seconds=_bounded_float(
            budgets["max_wall_seconds"],
            label="budgets.max_wall_seconds",
            minimum=5,
            maximum=600,
        ),
        discovery_lookback_hours=_bounded_int(
            cadence["discovery_lookback_hours"],
            label="cadence.discovery_lookback_hours",
            minimum=1,
            maximum=MAX_DISCOVERY_LOOKBACK_HOURS,
        ),
        version_recheck_days=_bounded_int(
            cadence["version_recheck_days"],
            label="cadence.version_recheck_days",
            minimum=1,
            maximum=30,
        ),
        verify_artifacts=verify_artifacts,
    )
    if result.max_discovery_items + result.max_recheck_items > result.max_items:
        raise RadarValidationError(
            "discovery and recheck item budgets exceed the aggregate item budget"
        )
    if result.max_frontier_items + result.max_backlog_items > result.max_discovery_items:
        raise RadarValidationError(
            "frontier and backlog item budgets exceed the discovery item budget"
        )
    return result


def sync_config_as_dict(config: RadarSyncConfig) -> dict[str, object]:
    return {
        "schema": SYNC_CONFIG_SCHEMA,
        "categories": list(config.categories),
        "discovery_terms": list(config.discovery_terms),
        "triage": {
            "profile_id": config.triage.profile_id,
            "active_questions": {
                name: list(terms) for name, terms in config.triage.active_questions
            },
            "observed_failures": {
                name: list(terms) for name, terms in config.triage.observed_failures
            },
            "mutable_layers": {name: list(terms) for name, terms in config.triage.mutable_layers},
            "supported_categories": list(config.triage.supported_categories),
        },
        "budgets": {
            "max_requests": config.max_requests,
            "max_response_bytes": config.max_response_bytes,
            "max_items": config.max_items,
            "max_discovery_items": config.max_discovery_items,
            "max_frontier_items": config.max_frontier_items,
            "max_backlog_items": config.max_backlog_items,
            "max_recheck_items": config.max_recheck_items,
            "max_artifacts": config.max_artifacts,
            "timeout_seconds": config.timeout_seconds,
            "max_wall_seconds": config.max_wall_seconds,
            "retry_count": 0,
        },
        "cadence": {
            "discovery_lookback_hours": config.discovery_lookback_hours,
            "version_recheck_days": config.version_recheck_days,
        },
        "verify_artifacts": config.verify_artifacts,
    }


def _validate_sync_config_instance(config: RadarSyncConfig) -> None:
    if (
        not isinstance(config.categories, tuple)
        or not 1 <= len(config.categories) <= 16
        or any(_CATEGORY_RE.fullmatch(item) is None for item in config.categories)
    ):
        raise RadarValidationError("RadarSyncConfig categories are invalid")
    if (
        not isinstance(config.discovery_terms, tuple)
        or not 1 <= len(config.discovery_terms) <= 32
        or any(not _valid_discovery_term(item) for item in config.discovery_terms)
    ):
        raise RadarValidationError("RadarSyncConfig discovery terms are invalid")
    integer_bounds = {
        "max_requests": (1, 64),
        "max_response_bytes": (1024, 64 * 1024 * 1024),
        "max_items": (1, 200),
        "max_discovery_items": (1, 100),
        "max_frontier_items": (1, 100),
        "max_backlog_items": (1, 100),
        "max_recheck_items": (1, 100),
        "max_artifacts": (0, 32),
        "discovery_lookback_hours": (1, MAX_DISCOVERY_LOOKBACK_HOURS),
        "version_recheck_days": (1, 30),
    }
    for name, (minimum, maximum) in integer_bounds.items():
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise RadarValidationError(f"RadarSyncConfig {name} is outside its bound")
    for name, minimum, maximum in (
        ("timeout_seconds", 1.0, 60.0),
        ("max_wall_seconds", 5.0, 600.0),
    ):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RadarValidationError(f"RadarSyncConfig {name} must be numeric")
        if not minimum <= float(value) <= maximum:
            raise RadarValidationError(f"RadarSyncConfig {name} is outside its bound")
    if not isinstance(config.verify_artifacts, bool):
        raise RadarValidationError("RadarSyncConfig verify_artifacts must be boolean")
    if config.max_discovery_items + config.max_recheck_items > config.max_items:
        raise RadarValidationError("RadarSyncConfig aggregate item budget is invalid")
    if config.max_frontier_items + config.max_backlog_items > config.max_discovery_items:
        raise RadarValidationError("RadarSyncConfig discovery lane budget is invalid")


def _parse_as_of(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RadarValidationError("as_of must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RadarValidationError("as_of must include a timezone")
    return parsed.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def discovery_locator(
    config: RadarSyncConfig,
    *,
    window_start: datetime,
    window_end: datetime,
    start_offset: int = 0,
    result_limit: int | None = None,
) -> str:
    start = window_start.astimezone(UTC)
    end = window_end.astimezone(UTC)
    if end <= start:
        raise RadarValidationError("discovery window end must be after its start")
    if end - start > timedelta(hours=MAX_DISCOVERY_LOOKBACK_HOURS):
        raise RadarValidationError("discovery window exceeds the maximum lookback")
    if not 0 <= start_offset <= 10_000_000:
        raise RadarValidationError("discovery start offset is outside the sanity bound")
    limit = config.max_discovery_items if result_limit is None else result_limit
    if not 1 <= limit <= config.max_discovery_items:
        raise RadarValidationError("discovery result limit exceeds the configured budget")
    category_query = " OR ".join(f"cat:{item}" for item in config.categories)
    relevance_query = " OR ".join(f'all:"{item}"' for item in config.discovery_terms)
    start_text = start.strftime("%Y%m%d%H%M%S")
    inclusive_end = (end - timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    search_query = (
        f"({category_query}) AND ({relevance_query}) "
        f"AND submittedDate:[{start_text} TO {inclusive_end}]"
    )
    query = urllib.parse.urlencode(
        {
            "search_query": search_query,
            "start": start_offset,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    return f"{ARXIV_API_URL}?{query}"


def exact_id_locators(identifiers: Sequence[str], *, page_size: int = 20) -> list[str]:
    if not identifiers:
        return []
    identities = [parse_arxiv_id(item) for item in identifiers]
    if len({item.base_id for item in identities}) != len(identities):
        raise RadarValidationError("duplicate exact-ID recheck identifiers")
    canonical = sorted(item.versioned_id for item in identities)
    locators: list[str] = []
    for offset in range(0, len(canonical), page_size):
        page = canonical[offset : offset + page_size]
        query = urllib.parse.urlencode(
            {"id_list": ",".join(page), "start": 0, "max_results": len(page)}
        )
        locators.append(f"{ARXIV_API_URL}?{query}")
    return locators


def _cache_directory(root: Path, cache_day: date, locator: str) -> Path:
    locator_hash = hashlib.sha256(locator.encode("utf-8")).hexdigest()
    return _planned_target(
        root,
        f"cache/http/{cache_day.isoformat()}/{locator_hash}",
    )


def _load_cached_fetch(
    root: Path, cache_day: date, locator: str, *, expected_provider: str
) -> CachedFetch | None:
    directory = _cache_directory(root, cache_day, locator)
    if not directory.exists():
        return None
    manifests = sorted(directory.glob("*.json"))
    if len(manifests) > 1:
        raise RadarConflictError(f"multiple cache manifests exist for {locator}")
    if not manifests:
        return None
    try:
        manifest_content = manifests[0].read_bytes()
        manifest = _json_bytes(manifest_content, label="Research Radar HTTP cache manifest")
    except OSError as exc:
        raise RadarValidationError("Research Radar HTTP cache manifest is corrupt") from exc
    required = {
        "schema",
        "locator",
        "final_locator",
        "provider",
        "fetched_at",
        "content_type",
        "response_sha256",
        "byte_count",
        "body_file",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise RadarValidationError("Research Radar HTTP cache manifest has invalid keys")
    if (
        _canonical_bytes(manifest) != manifest_content
        or manifests[0].name != hashlib.sha256(manifest_content).hexdigest() + ".json"
    ):
        raise RadarValidationError("Research Radar HTTP cache manifest hash is invalid")
    if (
        manifest["schema"] != CACHE_ENTRY_SCHEMA
        or manifest["locator"] != locator
        or manifest["final_locator"] != locator
        or manifest["provider"] != expected_provider
    ):
        raise RadarValidationError("Research Radar HTTP cache identity mismatch")
    request = urllib.parse.urlsplit(locator)
    final = urllib.parse.urlsplit(str(manifest["final_locator"]))
    if (
        request.scheme != "https"
        or (request.hostname or "").lower() not in _ALLOWED_NETWORK_HOSTS
        or final.scheme != "https"
        or (final.hostname or "").lower() not in _ALLOWED_NETWORK_HOSTS
    ):
        raise RadarValidationError("Research Radar HTTP cache endpoint is not allowlisted")
    fetched_at = _iso8601(manifest["fetched_at"], "cache fetched_at")
    if manifest["fetched_at"] != fetched_at:
        raise RadarValidationError("Research Radar HTTP cache timestamp is not canonical")
    if _parse_as_of(fetched_at).date() != cache_day:
        raise RadarValidationError("Research Radar HTTP cache day does not match fetched_at")
    content_type = manifest["content_type"]
    if content_type is not None and (not isinstance(content_type, str) or len(content_type) > 256):
        raise RadarValidationError("Research Radar HTTP cache content type is invalid")
    response_sha256 = manifest["response_sha256"]
    if not isinstance(response_sha256, str) or _SHA256_RE.fullmatch(response_sha256) is None:
        raise RadarValidationError("Research Radar HTTP cache response hash is invalid")
    byte_count = manifest["byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise RadarValidationError("Research Radar HTTP cache byte count is invalid")
    body_name = manifest["body_file"]
    if (
        not isinstance(body_name, str)
        or Path(body_name).name != body_name
        or not body_name.endswith(".bin")
        or body_name != f"{response_sha256.split(':', 1)[1]}.bin"
    ):
        raise RadarValidationError("Research Radar HTTP cache body path is invalid")
    body_path = directory / body_name
    try:
        body = body_path.read_bytes()
    except OSError as exc:
        raise RadarValidationError("Research Radar HTTP cache body is missing") from exc
    if len(body) != byte_count or _sha256(body) != response_sha256:
        raise RadarValidationError("Research Radar HTTP cache body failed integrity validation")
    return CachedFetch(
        locator=locator,
        content=body,
        response_sha256=response_sha256,
        source="daily_cache",
        final_locator=str(manifest["final_locator"]),
        content_type=(
            str(manifest["content_type"]) if manifest["content_type"] is not None else None
        ),
    )


def _store_cached_fetch(
    root: Path,
    cache_day: date,
    *,
    provider: str,
    fetched_at: str,
    result: CachedFetch,
) -> None:
    directory = _cache_directory(root, cache_day, result.locator)
    digest = result.response_sha256.split(":", 1)[1]
    body_name = f"{digest}.bin"
    body_state = _write_immutable(directory / body_name, result.content)
    if body_state not in {"created", "unchanged"}:
        raise ResearchRadarError("unexpected HTTP cache body write state")
    manifest = {
        "schema": CACHE_ENTRY_SCHEMA,
        "locator": result.locator,
        "final_locator": result.final_locator,
        "provider": provider,
        "fetched_at": fetched_at,
        "content_type": result.content_type,
        "response_sha256": result.response_sha256,
        "byte_count": len(result.content),
        "body_file": body_name,
    }
    manifest_content = _canonical_bytes(manifest)
    manifest_name = hashlib.sha256(manifest_content).hexdigest() + ".json"
    _write_immutable(directory / manifest_name, manifest_content)


class BoundedNetworkClient:
    def __init__(
        self,
        *,
        root: Path,
        cache_day: date,
        as_of: str,
        config: RadarSyncConfig,
        write_cache: bool,
        offline: bool,
        opener: Callable[..., object] = _safe_urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if type(write_cache) is not bool or type(offline) is not bool:
            raise RadarValidationError(
                "BoundedNetworkClient write_cache and offline must be exact booleans"
            )
        self.root = root.expanduser().resolve()
        self.cache_day = cache_day
        self.as_of = as_of
        self.config = config
        self.write_cache = write_cache
        self.offline = offline
        self.opener = opener
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.started = monotonic()
        self.requests = 0
        self.response_bytes = 0
        self.cache_hits = 0
        self._last_request_at: dict[str, float] = {}

    def _check_wall(self) -> None:
        if self.monotonic() - self.started > self.config.max_wall_seconds:
            raise RadarNetworkError(
                "Research Radar network wall-time budget exhausted",
                classification="runtime_blocked_wall_budget",
            )

    def fetch(self, locator: str, *, provider: str, accept: str) -> CachedFetch:
        parsed = urllib.parse.urlsplit(locator)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in _ALLOWED_NETWORK_HOSTS:
            raise RadarValidationError("Research Radar network target is not allowlisted")
        cached = _load_cached_fetch(self.root, self.cache_day, locator, expected_provider=provider)
        if cached is not None:
            self.response_bytes += len(cached.content)
            if self.response_bytes > self.config.max_response_bytes:
                raise RadarNetworkError(
                    "Research Radar cached-response byte budget exhausted",
                    classification="runtime_blocked_byte_budget",
                )
            self.cache_hits += 1
            return cached
        if self.offline:
            raise RadarNetworkError(
                f"no daily cache exists for required {provider} request",
                classification="runtime_blocked_cache_miss",
            )
        self._check_wall()
        if self.requests >= self.config.max_requests:
            raise RadarNetworkError(
                "Research Radar request budget exhausted",
                classification="runtime_blocked_request_budget",
            )
        interval = (
            MIN_PAGE_INTERVAL_SECONDS
            if host == "export.arxiv.org"
            else MIN_ARTIFACT_INTERVAL_SECONDS
        )
        previous = self._last_request_at.get(host)
        now = self.monotonic()
        if previous is not None and now - previous < interval:
            self.sleeper(interval - (now - previous))
            self._check_wall()
        request = urllib.request.Request(
            locator,
            headers={
                "Accept": accept,
                "User-Agent": "CodingIntelligenceResearchRadar/1.1 (bounded metadata intake)",
            },
        )
        self.requests += 1
        self._last_request_at[host] = self.monotonic()
        remaining = self.config.max_response_bytes - self.response_bytes
        if remaining <= 0:
            raise RadarNetworkError(
                "Research Radar response byte budget exhausted",
                classification="runtime_blocked_byte_budget",
            )
        try:
            response = self.opener(request, timeout=self.config.timeout_seconds)
            with response:  # type: ignore[attr-defined]
                final_locator = (
                    response.geturl()  # type: ignore[attr-defined]
                    if hasattr(response, "geturl")
                    else locator
                )
                final = urllib.parse.urlsplit(str(final_locator))
                if (
                    final.scheme != "https"
                    or (final.hostname or "").lower() not in _ALLOWED_NETWORK_HOSTS
                ):
                    raise RadarNetworkError(
                        f"{provider} redirected outside the network allowlist",
                        classification="unverified_redirect_target",
                    )
                content = response.read(remaining + 1)  # type: ignore[attr-defined]
                headers = getattr(response, "headers", None)
                content_type = headers.get("Content-Type") if headers is not None else None
        except urllib.error.HTTPError as exc:
            raise RadarNetworkError(
                f"{provider} primary metadata returned HTTP {exc.code}",
                classification=f"unverified_http_{exc.code}",
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise RadarNetworkError(
                f"{provider} primary metadata request failed",
                classification="unverified_network_failure",
            ) from exc
        self._check_wall()
        self.response_bytes += len(content)
        if self.response_bytes > self.config.max_response_bytes:
            raise RadarNetworkError(
                "Research Radar response byte budget exhausted",
                classification="runtime_blocked_byte_budget",
            )
        result = CachedFetch(
            locator=locator,
            content=content,
            response_sha256=_sha256(content),
            source="network",
            final_locator=str(final_locator),
            content_type=str(content_type) if content_type is not None else None,
        )
        return result

    def persist(self, result: CachedFetch, *, provider: str) -> None:
        if self.write_cache and result.source == "network":
            _store_cached_fetch(
                self.root,
                self.cache_day,
                provider=provider,
                fetched_at=self.as_of,
                result=result,
            )

    def report(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "response_bytes": self.response_bytes,
            "cache_hits": self.cache_hits,
            "retry_count": 0,
            "max_requests": self.config.max_requests,
            "max_response_bytes": self.config.max_response_bytes,
            "max_wall_seconds": self.config.max_wall_seconds,
        }


def extract_artifact_candidates(version: dict[str, object]) -> list[ArtifactCandidate]:
    metadata_text: list[str] = []
    for key in ("abstract", "comment", "journal_reference"):
        value = version.get(key)
        if isinstance(value, str):
            metadata_text.append(value)
    links = version.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and isinstance(link.get("href"), str):
                metadata_text.append(str(link["href"]))
    candidates: dict[str, ArtifactCandidate] = {}
    for text_value in metadata_text:
        for match in _URL_RE.finditer(text_value):
            observed = match.group(0).rstrip(".,;:!?)]}>")
            parsed = urllib.parse.urlsplit(observed)
            host = (parsed.hostname or "").lower()
            segments = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
            candidate: ArtifactCandidate | None = None
            if host in {"github.com", "www.github.com"} and len(segments) >= 2:
                namespace, name = segments[0], segments[1]
                if name.endswith(".git"):
                    name = name[:-4]
                if _ARTIFACT_NAME_RE.fullmatch(namespace) and _ARTIFACT_NAME_RE.fullmatch(name):
                    candidate = ArtifactCandidate(
                        provider="github",
                        artifact_kind="repository",
                        namespace=namespace,
                        name=name,
                        observed_url=observed,
                    )
            elif host == "huggingface.co":
                artifact_kind = "model"
                offset = 0
                if segments and segments[0] in {"datasets", "spaces"}:
                    artifact_kind = segments[0][:-1] if segments[0].endswith("s") else segments[0]
                    offset = 1
                if len(segments) >= offset + 2:
                    namespace, name = segments[offset], segments[offset + 1]
                    if _ARTIFACT_NAME_RE.fullmatch(namespace) and _ARTIFACT_NAME_RE.fullmatch(name):
                        candidate = ArtifactCandidate(
                            provider="huggingface",
                            artifact_kind=artifact_kind,
                            namespace=namespace,
                            name=name,
                            observed_url=observed,
                        )
            if candidate is not None:
                candidates.setdefault(candidate.normalized_id, candidate)
    return [candidates[key] for key in sorted(candidates)]


def _pending_artifact(
    version_key: str,
    candidate: ArtifactCandidate,
    *,
    attempts: int = 0,
    next_attempt_date: str | None = None,
) -> dict[str, object]:
    _identity_from_version_key(version_key)
    return {
        "version_key": version_key,
        "artifact_id": candidate.normalized_id,
        "provider": candidate.provider,
        "artifact_kind": candidate.artifact_kind,
        "namespace": candidate.namespace,
        "name": candidate.name,
        "observed_url": candidate.observed_url,
        "attempts": attempts,
        "next_attempt_date": next_attempt_date,
    }


def _candidate_from_pending(
    value: object,
) -> tuple[str, ArtifactCandidate, int, str | None]:
    if not isinstance(value, dict) or set(value) != {
        "version_key",
        "artifact_id",
        "provider",
        "artifact_kind",
        "namespace",
        "name",
        "observed_url",
        "attempts",
        "next_attempt_date",
    }:
        raise RadarValidationError("pending artifact entry keys are invalid")
    version_key = str(value.get("version_key"))
    _identity_from_version_key(version_key)
    provider = value.get("provider")
    artifact_kind = value.get("artifact_kind")
    namespace = value.get("namespace")
    name = value.get("name")
    observed_url = value.get("observed_url")
    attempts = value.get("attempts")
    next_attempt_date = value.get("next_attempt_date")
    if (
        provider not in {"github", "huggingface"}
        or artifact_kind not in {"repository", "model", "dataset", "space"}
        or not isinstance(namespace, str)
        or not isinstance(name, str)
        or not isinstance(observed_url, str)
        or _ARTIFACT_NAME_RE.fullmatch(namespace) is None
        or _ARTIFACT_NAME_RE.fullmatch(name) is None
        or (provider == "github" and artifact_kind != "repository")
        or (provider == "huggingface" and artifact_kind == "repository")
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 0 <= attempts <= 100
    ):
        raise RadarValidationError("pending artifact identity is invalid")
    candidate = ArtifactCandidate(
        provider=str(provider),
        artifact_kind=str(artifact_kind),
        namespace=namespace,
        name=name,
        observed_url=observed_url,
    )
    if value.get("artifact_id") != candidate.normalized_id:
        raise RadarValidationError("pending artifact normalized ID is invalid")
    parsed = urllib.parse.urlsplit(observed_url)
    expected_host = "github.com" if provider == "github" else "huggingface.co"
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        expected_host,
        f"www.{expected_host}",
    }:
        raise RadarValidationError("pending artifact observed URL is invalid")
    if next_attempt_date is not None:
        if not isinstance(next_attempt_date, str):
            raise RadarValidationError("pending artifact retry date is invalid")
        try:
            parsed_date = date.fromisoformat(next_attempt_date)
        except ValueError as exc:
            raise RadarValidationError("pending artifact retry date is invalid") from exc
        if parsed_date.isoformat() != next_attempt_date:
            raise RadarValidationError("pending artifact retry date is not canonical")
    return version_key, candidate, attempts, next_attempt_date


def _json_primary(result: CachedFetch, *, label: str, max_keys: int = 256) -> dict[str, object]:
    try:
        value = _json_bytes(result.content, label=f"{label} primary metadata")
    except RadarValidationError as exc:
        raise RadarNetworkError(
            f"{label} primary metadata was not valid UTF-8 JSON",
            classification="unverified_malformed_primary",
        ) from exc
    if not isinstance(value, dict) or len(value) > max_keys:
        raise RadarNetworkError(
            f"{label} primary metadata was not a bounded object",
            classification="unverified_malformed_primary",
        )
    return value


def _artifact_base_record(
    *, candidate: ArtifactCandidate, version_key: str, observed_at: str
) -> dict[str, object]:
    return {
        "schema": ARTIFACT_OBSERVATION_SCHEMA,
        "paper_version_key": version_key,
        "artifact_id": candidate.normalized_id,
        "provider": candidate.provider,
        "artifact_kind": candidate.artifact_kind,
        "observed_url": candidate.observed_url,
        "provenance": {
            "linkage": "explicit_url_in_untrusted_paper_metadata",
            "author_project_linkage": "unverified",
            "code_executed": False,
            "content_downloaded": False,
        },
        "observed_at": observed_at,
    }


def verify_artifact_candidate(
    candidate: ArtifactCandidate,
    *,
    version_key: str,
    observed_at: str,
    client: BoundedNetworkClient,
) -> dict[str, object]:
    record = _artifact_base_record(
        candidate=candidate, version_key=version_key, observed_at=observed_at
    )
    record.update(
        {
            "verification_status": "unverified_not_checked",
            "canonical_id": None,
            "canonical_url": None,
            "revision": None,
            "revision_kind": None,
            "license": {"status": "unverified", "value": None},
            "primary_responses": [],
        }
    )
    try:
        if candidate.provider == "github":
            repo_locator = (
                f"https://api.github.com/repos/{urllib.parse.quote(candidate.namespace)}/"
                f"{urllib.parse.quote(candidate.name)}"
            )
            repo_result = client.fetch(
                repo_locator, provider="github", accept="application/vnd.github+json"
            )
            repo = _json_primary(repo_result, label="GitHub repository")
            full_name = repo.get("full_name")
            default_branch = repo.get("default_branch")
            html_url = repo.get("html_url")
            if not all(
                isinstance(item, str) and item for item in (full_name, default_branch, html_url)
            ):
                raise RadarNetworkError(
                    "GitHub repository metadata lacks canonical identity",
                    classification="unverified_malformed_primary",
                )
            canonical_parts = str(full_name).split("/", 1)
            if len(canonical_parts) != 2 or any(
                _ARTIFACT_NAME_RE.fullmatch(item) is None for item in canonical_parts
            ):
                raise RadarNetworkError(
                    "GitHub repository metadata has invalid canonical identity",
                    classification="unverified_malformed_primary",
                )
            requested_name = f"{candidate.namespace}/{candidate.name}"
            html = urllib.parse.urlsplit(str(html_url))
            if (
                str(full_name).casefold() != requested_name.casefold()
                or html.scheme != "https"
                or (html.hostname or "").lower() not in {"github.com", "www.github.com"}
                or html.path.strip("/").casefold() != str(full_name).casefold()
            ):
                raise RadarNetworkError(
                    "GitHub primary metadata identity differs from the requested artifact",
                    classification="unverified_identity_mismatch",
                )
            license_value = repo.get("license")
            license_spdx = None
            if isinstance(license_value, dict):
                for key in ("spdx_id", "key", "name"):
                    item = license_value.get(key)
                    if isinstance(item, str) and item and item.upper() != "NOASSERTION":
                        license_spdx = item
                        break
            client.persist(repo_result, provider="github")
            record.update(
                {
                    "verification_status": "unverified_missing_revision",
                    "canonical_id": f"github:{full_name}",
                    "canonical_url": html_url,
                    "default_branch": default_branch,
                    "license": {
                        "status": "verified" if license_spdx else "unverified_missing",
                        "value": license_spdx,
                    },
                    "archived": (
                        repo.get("archived") if isinstance(repo.get("archived"), bool) else None
                    ),
                    "fork": repo.get("fork") if isinstance(repo.get("fork"), bool) else None,
                    "primary_responses": [
                        {
                            "locator": repo_result.final_locator,
                            "sha256": repo_result.response_sha256,
                        }
                    ],
                }
            )
            commit_locator = (
                f"https://api.github.com/repos/{urllib.parse.quote(canonical_parts[0])}/"
                f"{urllib.parse.quote(canonical_parts[1])}/commits/"
                f"{urllib.parse.quote(str(default_branch))}"
            )
            commit_result = client.fetch(
                commit_locator, provider="github", accept="application/vnd.github+json"
            )
            commit = _json_primary(commit_result, label="GitHub commit")
            revision = commit.get("sha")
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
                raise RadarNetworkError(
                    "GitHub commit metadata lacks an exact revision",
                    classification="unverified_missing_revision",
                )
            client.persist(commit_result, provider="github")
            primary_responses = list(record["primary_responses"])  # type: ignore[arg-type]
            primary_responses.append(
                {
                    "locator": commit_result.final_locator,
                    "sha256": commit_result.response_sha256,
                }
            )
            record.update(
                {
                    "verification_status": "verified_primary_metadata",
                    "revision": revision.lower(),
                    "revision_kind": "commit",
                    "primary_responses": primary_responses,
                }
            )
        elif candidate.provider == "huggingface":
            prefix = {
                "model": "models",
                "dataset": "datasets",
                "space": "spaces",
            }[candidate.artifact_kind]
            locator = (
                f"https://huggingface.co/api/{prefix}/"
                f"{urllib.parse.quote(candidate.namespace)}/{urllib.parse.quote(candidate.name)}"
            )
            result = client.fetch(locator, provider="huggingface", accept="application/json")
            metadata = _json_primary(result, label="Hugging Face artifact")
            canonical_id = metadata.get("id") or metadata.get("modelId")
            revision = metadata.get("sha")
            if not isinstance(canonical_id, str) or "/" not in canonical_id:
                raise RadarNetworkError(
                    "Hugging Face metadata lacks canonical identity",
                    classification="unverified_malformed_primary",
                )
            requested_id = f"{candidate.namespace}/{candidate.name}"
            if canonical_id.casefold() != requested_id.casefold():
                raise RadarNetworkError(
                    "Hugging Face primary metadata identity differs from the requested artifact",
                    classification="unverified_identity_mismatch",
                )
            if (
                not isinstance(revision, str)
                or re.fullmatch(r"[0-9a-fA-F]{40,64}", revision) is None
            ):
                raise RadarNetworkError(
                    "Hugging Face metadata lacks an exact revision",
                    classification="unverified_missing_revision",
                )
            client.persist(result, provider="huggingface")
            license_value = None
            card_data = metadata.get("cardData")
            if isinstance(card_data, dict) and isinstance(card_data.get("license"), str):
                license_value = str(card_data["license"])
            if license_value is None and isinstance(metadata.get("tags"), list):
                license_tags = sorted(
                    item.split(":", 1)[1]
                    for item in metadata["tags"]
                    if isinstance(item, str) and item.startswith("license:") and ":" in item
                )
                license_value = license_tags[0] if license_tags else None
            record.update(
                {
                    "verification_status": "verified_primary_metadata",
                    "canonical_id": f"huggingface:{candidate.artifact_kind}:{canonical_id}",
                    "canonical_url": (
                        f"https://huggingface.co/{canonical_id}"
                        if candidate.artifact_kind == "model"
                        else f"https://huggingface.co/{prefix}/{canonical_id}"
                    ),
                    "revision": revision.lower(),
                    "revision_kind": "hub_revision",
                    "license": {
                        "status": "verified" if license_value else "unverified_missing",
                        "value": license_value,
                    },
                    "primary_responses": [
                        {
                            "locator": result.final_locator,
                            "sha256": result.response_sha256,
                        }
                    ],
                }
            )
        else:
            raise RadarValidationError("unknown artifact provider")
    except RadarNetworkError as exc:
        record["verification_status"] = exc.classification
    record["trust"] = {
        "classification": "untrusted_external_metadata",
        "instructions_are_data": True,
        "execution_permitted": False,
    }
    return record


def artifact_planned_write(root: Path, record: dict[str, object]) -> PlannedWrite:
    version = _identity_from_version_key(record.get("paper_version_key"))
    provider = str(record.get("provider"))
    artifact_id = str(record.get("artifact_id"))
    content = _canonical_bytes(record)
    identity_hash = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()[:20]
    digest = hashlib.sha256(content).hexdigest()
    relative = (
        f"artifacts/{provider}/{_safe_base_path(version.base_id)}/v{version.version}/"
        f"{identity_hash}-{digest}.json"
    )
    target = root.expanduser().resolve() / relative
    state = "unchanged" if target.exists() and target.read_bytes() == content else "create"
    return PlannedWrite("artifact", relative, content, state)


def _artifact_observations_for_version(root: Path, version_key: str) -> list[dict[str, object]]:
    identity = _identity_from_version_key(version_key)
    paths: list[Path] = []
    for provider in ("github", "huggingface"):
        directory = _planned_target(
            root,
            f"artifacts/{provider}/{_safe_base_path(identity.base_id)}/v{identity.version}",
        )
        if directory.exists():
            paths.extend(sorted(directory.glob("*.json")))
    if len(paths) > 200:
        raise RadarValidationError("artifact observation count exceeds the v1 bound")
    latest: dict[str, tuple[datetime, dict[str, object]]] = {}
    for path in paths:
        relative = path.relative_to(root.expanduser().resolve()).as_posix()
        item = PlannedWrite("artifact", relative, path.read_bytes(), "unchanged")
        _target, value = _validate_planned_write(root, item)
        if value.get("paper_version_key") != identity.version_key:
            raise RadarValidationError("artifact observation version identity mismatch")
        artifact_id = value.get("artifact_id")
        observed_at = value.get("observed_at")
        if not isinstance(artifact_id, str) or not isinstance(observed_at, str):
            raise RadarValidationError("artifact observation identity or time is invalid")
        observed = _parse_as_of(observed_at)
        previous = latest.get(artifact_id)
        if previous is None or observed > previous[0]:
            latest[artifact_id] = (observed, value)
    return [latest[key][1] for key in sorted(latest)]


def _artifact_has_terminal_observation(
    root: Path, version_key: str, candidate: ArtifactCandidate
) -> bool:
    return any(
        record.get("artifact_id") == candidate.normalized_id
        and (
            record.get("verification_status") == "verified_primary_metadata"
            or record.get("retry_disposition") == "manual_review_required_max_attempts"
        )
        for record in _artifact_observations_for_version(root, version_key)
    )


def _matching_groups(
    corpus: str, groups: Sequence[tuple[str, tuple[str, ...]]]
) -> list[dict[str, object]]:
    normalized = " " + " ".join(corpus.casefold().split()) + " "
    matches: list[dict[str, object]] = []
    for name, terms in groups:
        hit_terms = sorted(
            term
            for term in terms
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized) is not None
        )
        if hit_terms:
            matches.append({"group": name, "terms": hit_terms})
    return matches


def triage_version(
    version: dict[str, object],
    *,
    profile: TriageProfile,
    as_of: str,
    primary_metadata_verified: bool,
    artifact_records: Sequence[dict[str, object]],
) -> dict[str, object]:
    identity = version.get("identity")
    if not isinstance(identity, dict):
        raise RadarValidationError("triage version lacks identity")
    version_key = _identity_from_version_key(identity.get("version_key")).version_key
    title = version.get("title") if isinstance(version.get("title"), str) else ""
    abstract = version.get("abstract") if isinstance(version.get("abstract"), str) else ""
    comment = version.get("comment") if isinstance(version.get("comment"), str) else ""
    corpus = f"{title} {abstract} {comment}"
    question_hits = _matching_groups(corpus, profile.active_questions)
    failure_hits = _matching_groups(corpus, profile.observed_failures)
    layer_hits = _matching_groups(corpus, profile.mutable_layers)
    categories = tuple(
        sorted(item for item in version.get("categories", []) if isinstance(item, str))
    )
    supported_hits = sorted(set(categories) & set(profile.supported_categories))
    applicability = min(
        100,
        (40 if question_hits else 0)
        + min(10, max(0, len(question_hits) - 1) * 5)
        + (30 if failure_hits else 0)
        + (15 if layer_hits else 0)
        + (5 if supported_hits else 0),
    )
    candidates = extract_artifact_candidates(version)
    verified_artifacts = [
        item
        for item in artifact_records
        if item.get("verification_status") == "verified_primary_metadata"
    ]
    verified_revisions = [item for item in verified_artifacts if item.get("revision")]
    verified_licenses = [
        item
        for item in verified_artifacts
        if isinstance(item.get("license"), dict) and item["license"].get("status") == "verified"  # type: ignore[union-attr]
    ]
    evidence = min(
        80,
        (35 if primary_metadata_verified else 0)
        + 15
        + (10 if version.get("license_uri") else 0)
        + (5 if candidates else 0)
        + (10 if verified_artifacts else 0)
        + (5 if verified_revisions else 0),
    )
    feasibility = min(
        75,
        (30 if supported_hits else 0)
        + (10 if candidates else 0)
        + (20 if verified_artifacts else 0)
        + (15 if verified_licenses else 0),
    )
    as_of_time = _parse_as_of(as_of)
    revised = _parse_as_of(str(version.get("revised_at")))
    age_days = max(0, (as_of_time - revised).days)
    if age_days <= 30:
        recency = 100
    elif age_days <= 90:
        recency = 80
    elif age_days <= 365:
        recency = 60
    elif age_days <= 730:
        recency = 40
    else:
        recency = 20
    if applicability >= 65 and evidence >= 45 and feasibility >= 45:
        suggestion = "experiment"
    elif applicability >= 45:
        suggestion = "watch"
    else:
        suggestion = "manual_review"
    profile_value = {
        "profile_id": profile.profile_id,
        "active_questions": {name: list(terms) for name, terms in profile.active_questions},
        "observed_failures": {name: list(terms) for name, terms in profile.observed_failures},
        "mutable_layers": {name: list(terms) for name, terms in profile.mutable_layers},
        "supported_categories": list(profile.supported_categories),
    }
    return {
        "schema": TRIAGE_SCHEMA,
        "paper_version_key": version_key,
        "as_of": _iso_z(as_of_time),
        "profile": {
            "profile_id": profile.profile_id,
            "sha256": _sha256(_canonical_bytes(profile_value)),
        },
        "scores": {
            "applicability": applicability,
            "evidence": evidence,
            "feasibility": feasibility,
            "recency": recency,
        },
        "features": {
            "active_question_matches": question_hits,
            "observed_failure_matches": failure_hits,
            "mutable_layer_matches": layer_hits,
            "supported_category_matches": supported_hits,
            "primary_metadata_verified": primary_metadata_verified,
            "artifact_candidates": len(candidates),
            "verified_artifacts": len(verified_artifacts),
            "verified_revisions": len(verified_revisions),
            "verified_licenses": len(verified_licenses),
            "age_days": age_days,
        },
        "suggestion": suggestion,
        "authority": {
            "automatic_lifecycle_transition": False,
            "automatic_adoption": False,
            "automatic_rejection": False,
            "operator_decision_required": True,
        },
        "evidence_boundary": {
            "scope": "metadata_only",
            "full_text_claim_extraction": "deferred_not_performed",
            "benchmark_claims_validated": False,
            "compute_requirements_validated": False,
        },
        "trust": {
            "external_text_used_only_for_literal_term_matching": True,
            "instructions_are_data": True,
            "execution_permitted": False,
        },
    }


def triage_planned_write(root: Path, record: dict[str, object]) -> PlannedWrite:
    identity = _identity_from_version_key(record.get("paper_version_key"))
    content = _canonical_bytes(record)
    digest = hashlib.sha256(content).hexdigest()
    relative = f"triage/{_safe_base_path(identity.base_id)}/v{identity.version}/{digest}.json"
    target = root.expanduser().resolve() / relative
    state = "unchanged" if target.exists() and target.read_bytes() == content else "create"
    return PlannedWrite("triage", relative, content, state)


def _default_sync_state() -> dict[str, object]:
    return {
        "generation": 0,
        "last_daily_discovery_date": None,
        "last_discovery_window_end": None,
        "last_discovery_config_sha256": None,
        "discovery_cycle_start": None,
        "discovery_cycle_end": None,
        "discovery_next_start": None,
        "discovery_cycle_total_results": None,
        "discovery_cycle_config_sha256": None,
        "last_version_recheck_completed_at": None,
        "recheck_cycle_started_at": None,
        "recheck_after_base_id": None,
        "last_run_id": None,
        "artifact_pending": [],
    }


def _sync_state_with_integrity(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema": SYNC_STATE_SCHEMA,
        "payload": payload,
        "payload_sha256": _sha256(_canonical_bytes(payload)),
    }


def _load_sync_state(root: Path) -> dict[str, object]:
    state_path = _planned_target(root, "state/sync-state.json")
    if not state_path.exists():
        return _default_sync_state()
    try:
        value = _json_bytes(state_path.read_bytes(), label="Research Radar sync state")
    except OSError as exc:
        raise RadarValidationError("Research Radar sync state is corrupt") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "payload", "payload_sha256"}
        or value.get("schema") != SYNC_STATE_SCHEMA
        or not isinstance(value.get("payload"), dict)
        or value.get("payload_sha256") != _sha256(_canonical_bytes(value["payload"]))
    ):
        raise RadarValidationError("Research Radar sync state failed integrity validation")
    payload = value["payload"]
    expected = set(_default_sync_state())
    legacy_optional = {
        "discovery_cycle_total_results",
        "discovery_cycle_config_sha256",
        "last_discovery_config_sha256",
        "artifact_pending",
    }
    if set(payload) - expected or (expected - set(payload)) - legacy_optional:
        raise RadarValidationError("Research Radar sync state has invalid keys")
    payload = {
        **payload,
        "discovery_cycle_total_results": payload.get(
            "discovery_cycle_total_results",
            payload.get("discovery_next_start"),
        ),
        "artifact_pending": payload.get("artifact_pending", []),
        "discovery_cycle_config_sha256": payload.get("discovery_cycle_config_sha256"),
        "last_discovery_config_sha256": payload.get(
            "last_discovery_config_sha256",
            (
                _LEGACY_UNBOUND_CONFIG_SHA256
                if payload.get("last_discovery_window_end") is not None
                else None
            ),
        ),
    }
    if (
        payload.get("discovery_cycle_start") is not None
        and payload.get("discovery_cycle_config_sha256") is None
    ):
        payload["discovery_cycle_config_sha256"] = _LEGACY_UNBOUND_CONFIG_SHA256
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise RadarValidationError("Research Radar sync state generation is invalid")
    for key in (
        "last_daily_discovery_date",
        "last_discovery_window_end",
        "last_discovery_config_sha256",
        "discovery_cycle_start",
        "discovery_cycle_end",
        "last_version_recheck_completed_at",
        "recheck_cycle_started_at",
        "recheck_after_base_id",
        "last_run_id",
    ):
        if payload.get(key) is not None and not isinstance(payload.get(key), str):
            raise RadarValidationError(f"Research Radar sync state {key} is invalid")
    daily_date = payload.get("last_daily_discovery_date")
    if daily_date is not None:
        try:
            parsed_date = date.fromisoformat(str(daily_date))
        except ValueError as exc:
            raise RadarValidationError("Research Radar daily date is invalid") from exc
        if parsed_date.isoformat() != daily_date:
            raise RadarValidationError("Research Radar daily date is not canonical")
    for key in (
        "last_discovery_window_end",
        "discovery_cycle_start",
        "discovery_cycle_end",
        "last_version_recheck_completed_at",
        "recheck_cycle_started_at",
    ):
        timestamp = payload.get(key)
        if timestamp is not None and _iso_z(_parse_as_of(str(timestamp))) != timestamp:
            raise RadarValidationError(f"Research Radar sync state {key} is not canonical UTC")
    recheck_after = payload.get("recheck_after_base_id")
    if recheck_after is not None:
        recheck_identity = parse_arxiv_id(str(recheck_after))
        if recheck_identity.version is not None or recheck_identity.base_id != recheck_after:
            raise RadarValidationError("Research Radar recheck cursor is not a canonical base ID")
        if recheck_after not in _known_base_ids(root):
            raise RadarValidationError("Research Radar recheck cursor is absent from the catalog")
    recheck_started = payload.get("recheck_cycle_started_at")
    if (recheck_started is None) != (recheck_after is None):
        raise RadarValidationError("Research Radar recheck cycle fields are inconsistent")
    last_run_id = payload.get("last_run_id")
    if last_run_id is not None and re.fullmatch(r"radar-[0-9a-f]{64}", str(last_run_id)) is None:
        raise RadarValidationError("Research Radar last run ID is invalid")
    discovery_next = payload.get("discovery_next_start")
    if discovery_next is not None and (
        isinstance(discovery_next, bool)
        or not isinstance(discovery_next, int)
        or discovery_next < 0
    ):
        raise RadarValidationError("Research Radar sync state discovery_next_start is invalid")
    discovery_total = payload.get("discovery_cycle_total_results")
    if discovery_total is not None and (
        isinstance(discovery_total, bool)
        or not isinstance(discovery_total, int)
        or discovery_total < 0
    ):
        raise RadarValidationError(
            "Research Radar sync state discovery_cycle_total_results is invalid"
        )
    discovery_cycle_values = (
        payload.get("discovery_cycle_start"),
        payload.get("discovery_cycle_end"),
        discovery_next,
        discovery_total,
        payload.get("discovery_cycle_config_sha256"),
    )
    if any(item is not None for item in discovery_cycle_values) and not all(
        item is not None for item in discovery_cycle_values
    ):
        raise RadarValidationError("Research Radar discovery cycle state is incomplete")
    if payload.get("discovery_cycle_start") is None and discovery_total is not None:
        raise RadarValidationError("Research Radar inactive discovery cycle retains a total")
    cycle_config_sha = payload.get("discovery_cycle_config_sha256")
    if cycle_config_sha is not None and (
        not isinstance(cycle_config_sha, str) or _SHA256_RE.fullmatch(cycle_config_sha) is None
    ):
        raise RadarValidationError("Research Radar discovery cycle config hash is invalid")
    last_config_sha = payload.get("last_discovery_config_sha256")
    if last_config_sha is not None and (
        not isinstance(last_config_sha, str) or _SHA256_RE.fullmatch(last_config_sha) is None
    ):
        raise RadarValidationError("Research Radar last discovery config hash is invalid")
    if (payload.get("last_discovery_window_end") is None) != (last_config_sha is None):
        raise RadarValidationError(
            "Research Radar last discovery window/config fields are inconsistent"
        )
    if payload.get("discovery_cycle_start") is not None:
        start = _parse_as_of(str(payload["discovery_cycle_start"]))
        end = _parse_as_of(str(payload["discovery_cycle_end"]))
        if start >= end:
            raise RadarValidationError("Research Radar discovery cycle window is empty")
        if int(discovery_next) > int(discovery_total):
            raise RadarValidationError("Research Radar discovery cursor exceeds its total")
    pending = payload.get("artifact_pending")
    if not isinstance(pending, list) or len(pending) > MAX_PENDING_ARTIFACTS:
        raise RadarValidationError("Research Radar pending artifact queue is invalid")
    pending_keys: list[tuple[str, str]] = []
    for entry in pending:
        version_key, candidate, _attempts, _next_attempt_date = _candidate_from_pending(entry)
        pending_keys.append((version_key, candidate.normalized_id))
    if pending_keys != sorted(set(pending_keys)):
        raise RadarValidationError("Research Radar pending artifact queue is not unique and sorted")
    return dict(payload)


def _write_sync_state(root: Path, payload: dict[str, object]) -> None:
    target = _planned_target(root, "state/sync-state.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_bytes(_sync_state_with_integrity(payload))
    descriptor, temporary_name = tempfile.mkstemp(prefix=".sync-state-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _sync_write_lock(root: Path) -> Iterator[None]:
    lock_path = _planned_target(root, ".locks/research-radar-sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    if not process_lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise RadarConflictError("timed out acquiring Research Radar sync lock")
    stream = None
    locked = False
    try:
        stream = lock_path.open("a+b", buffering=0)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ResearchRadarError(
                        f"cannot acquire Research Radar sync lock: {exc}"
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RadarConflictError(
                        "timed out acquiring Research Radar sync lock"
                    ) from exc
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        try:
            if stream is not None and locked:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if stream is not None:
                stream.close()
            process_lock.release()


def _known_base_ids(root: Path) -> list[str]:
    papers = _planned_target(root, "papers")
    if not papers.exists():
        return []
    identities = sorted(papers.rglob("identity.json"))
    if len(identities) > MAX_CATALOG_RECORDS:
        raise RadarValidationError("Research Radar catalog exceeds the v1 scan bound")
    base_ids: set[str] = set()
    for path in identities:
        relative = path.relative_to(root.expanduser().resolve()).as_posix()
        _target, value = _validate_planned_write(
            root,
            PlannedWrite("paper", relative, path.read_bytes(), "unchanged"),
        )
        identity = parse_arxiv_id(str(value.get("arxiv_base_id")))
        if identity.version is not None:
            raise RadarValidationError(f"paper identity unexpectedly contains a version: {path}")
        base_ids.add(identity.base_id)
    return sorted(base_ids)


def _observation_write(root: Path, observation: dict[str, object]) -> PlannedWrite:
    observation_id = str(observation.get("observation_id"))
    if not observation_id.startswith("arxiv-"):
        raise RadarValidationError("discovery observation has invalid identity")
    content = _canonical_bytes(observation)
    relative = f"observations/arxiv/{observation_id}.json"
    target = root.expanduser().resolve() / relative
    state = "unchanged" if target.exists() and target.read_bytes() == content else "create"
    return PlannedWrite("source_observation", relative, content, state)


def _apply_auxiliary_writes(root: Path, writes: Sequence[PlannedWrite]) -> dict[str, int]:
    if len({item.relative_path for item in writes}) != len(writes):
        raise RadarValidationError("auxiliary write plan contains duplicate targets")
    allowed = {"artifact", "triage", "source_observation", "sync_run"}
    for item in writes:
        if item.kind not in allowed:
            raise RadarValidationError(f"unsupported Research Radar auxiliary write: {item.kind}")
        _validate_planned_write(root, item)
    created = 0
    unchanged = 0
    for item in sorted(writes, key=lambda value: (value.kind, value.relative_path)):
        target, _value = _validate_planned_write(root, item)
        state = _write_immutable(target, item.content)
        created += state == "created"
        unchanged += state == "unchanged"
    return {"created": created, "unchanged": unchanged}


def _recheck_due(state: dict[str, object], as_of: datetime, days: int) -> bool:
    if state.get("recheck_cycle_started_at") is not None:
        return True
    completed = state.get("last_version_recheck_completed_at")
    if completed is None:
        return True
    return as_of - _parse_as_of(str(completed)) >= timedelta(days=days)


def _select_recheck_ids(
    known: Sequence[str], state: dict[str, object], *, limit: int
) -> tuple[list[str], bool]:
    after = state.get("recheck_after_base_id")
    if after is not None and str(after) not in known:
        raise RadarValidationError("Research Radar recheck cursor is absent from the catalog")
    eligible = [item for item in known if after is None or item > str(after)]
    selected = eligible[:limit]
    complete = len(eligible) <= limit
    return selected, complete


def _fetch_discovery_page(
    root: Path,
    config: RadarSyncConfig,
    client: BoundedNetworkClient,
    *,
    window_start: datetime,
    window_end: datetime,
    start_offset: int,
    result_limit: int,
) -> tuple[PlannedWrite, list[str], int, int]:
    locator = discovery_locator(
        config,
        window_start=window_start,
        window_end=window_end,
        start_offset=start_offset,
        result_limit=result_limit,
    )
    fetched = client.fetch(locator, provider="arxiv_discovery", accept="application/atom+xml")
    batch = SourceBatch(
        locator,
        fetched.content,
        provider="arxiv_atom_discovery",
        primary_metadata_verified=False,
        allow_empty=True,
    )
    parsed = parse_arxiv_atom(batch, max_entries=result_limit)
    pagination = parsed["pagination"]
    page_start = int(pagination["start_index"])
    if page_start != start_offset:
        raise RadarValidationError("arXiv discovery pagination start does not match the request")
    returned_entries = len(parsed["versions"])
    total_results = int(pagination["total_results"])
    if start_offset + returned_entries < total_results and returned_entries == 0:
        raise RadarValidationError("arXiv discovery page made no pagination progress")
    client.persist(fetched, provider="arxiv_discovery")
    identifiers = sorted(
        {
            str(version["identity"]["arxiv_base_id"])
            for version in parsed["versions"]  # type: ignore[index]
        }
    )
    return (
        _observation_write(root, parsed["observation"]),
        identifiers,
        total_results,
        returned_entries,
    )


def _last_currentness(
    root: Path, state: Mapping[str, object], *, as_of: datetime
) -> dict[str, object]:
    run_id = state.get("last_run_id")
    if run_id is None:
        return {
            "status": "inconclusive_no_currentness_evidence",
            "caught_up": False,
            "backlog_count": None,
            "oldest_unprocessed_age_seconds": None,
            "currentness_lag_seconds": None,
            "arrival_rate_per_day": None,
            "frontier_capacity_per_day": None,
            "discovery_capacity_per_day": None,
            "net_backfill_capacity_per_day": None,
            "backlog_capacity_per_run": None,
            "estimated_catch_up_runs": None,
        }
    directory = _planned_target(root, "sync/runs")
    paths = sorted(directory.glob(f"{run_id}-*.json")) if directory.exists() else []
    if len(paths) != 1:
        raise RadarValidationError("Research Radar last run evidence is missing or ambiguous")
    path = paths[0]
    relative = path.relative_to(root.expanduser().resolve()).as_posix()
    _target, run = _validate_planned_write(
        root,
        PlannedWrite("sync_run", relative, path.read_bytes(), "unchanged"),
    )
    currentness = run.get("currentness")
    required = {
        "status",
        "caught_up",
        "backlog_count",
        "oldest_unprocessed_age_seconds",
        "currentness_lag_seconds",
        "arrival_rate_per_day",
        "frontier_capacity_per_day",
        "discovery_capacity_per_day",
        "net_backfill_capacity_per_day",
        "backlog_capacity_per_run",
        "estimated_catch_up_runs",
    }
    if not isinstance(currentness, dict) or set(currentness) != required:
        raise RadarValidationError("Research Radar last run lacks currentness evidence")
    result = dict(currentness)
    if result.get("status") not in {
        "current",
        "frontier_current_backfill_active",
        "runtime_blocked_capacity",
    } or not isinstance(result.get("caught_up"), bool):
        raise RadarValidationError("Research Radar last currentness verdict is invalid")
    for key in required - {
        "status",
        "caught_up",
        "arrival_rate_per_day",
        "net_backfill_capacity_per_day",
    }:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RadarValidationError("Research Radar last currentness metric is invalid")
    arrival = result.get("arrival_rate_per_day")
    if isinstance(arrival, bool) or not isinstance(arrival, (int, float)) or arrival < 0:
        raise RadarValidationError("Research Radar last arrival-rate metric is invalid")
    net_capacity = result.get("net_backfill_capacity_per_day")
    if (
        isinstance(net_capacity, bool)
        or not isinstance(net_capacity, (int, float))
        or net_capacity < 0
    ):
        raise RadarValidationError("Research Radar last net-capacity metric is invalid")
    run_as_of = _parse_as_of(str(run.get("as_of")))
    if run_as_of > as_of:
        raise RadarValidationError("Research Radar last run is later than as_of")
    elapsed = int((as_of - run_as_of).total_seconds())
    result["currentness_lag_seconds"] = int(result["currentness_lag_seconds"]) + elapsed
    if int(result["backlog_count"]) > 0:
        result["oldest_unprocessed_age_seconds"] = (
            int(result["oldest_unprocessed_age_seconds"]) + elapsed
        )
    return result


def _sync_research_radar_once(
    root: Path,
    config: RadarSyncConfig,
    *,
    as_of: datetime,
    apply: bool,
    offline: bool,
    opener: Callable[..., object],
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
) -> dict[str, object]:
    root = root.expanduser().resolve()
    as_of_text = _iso_z(as_of)
    state = _load_sync_state(root)
    for timestamp_key in (
        "last_discovery_window_end",
        "discovery_cycle_start",
        "discovery_cycle_end",
        "last_version_recheck_completed_at",
        "recheck_cycle_started_at",
    ):
        timestamp_value = state.get(timestamp_key)
        if timestamp_value is not None and _parse_as_of(str(timestamp_value)) > as_of:
            raise RadarValidationError(
                f"Research Radar clock regression: {timestamp_key} is later than as_of"
            )
    day_text = as_of.date().isoformat()
    stored_cycle = state.get("discovery_cycle_start") is not None
    discovery_cycle_config_reset = bool(
        stored_cycle and state.get("discovery_cycle_config_sha256") != config.sha256
    )
    discovery_cycle_active = stored_cycle and not discovery_cycle_config_reset
    discovery_history_config_reset = bool(
        state.get("last_discovery_window_end") is not None
        and state.get("last_discovery_config_sha256") != config.sha256
    )
    daily_due = (
        discovery_cycle_active
        or discovery_cycle_config_reset
        or state.get("last_daily_discovery_date") != day_text
    )
    weekly_due = _recheck_due(state, as_of, config.version_recheck_days)
    if not daily_due and not weekly_due:
        currentness = _last_currentness(root, state, as_of=as_of)
        fast_status = (
            "runtime_blocked"
            if currentness["status"] == "runtime_blocked_capacity"
            else (
                "inconclusive"
                if currentness["status"] == "inconclusive_no_currentness_evidence"
                else "current"
            )
        )
        return {
            "status": fast_status,
            "mode": "explicit_apply" if apply else "dry_run_no_writes",
            "root": str(root),
            "as_of": as_of_text,
            "daily_discovery_due": False,
            "weekly_version_recheck_due": False,
            "currentness": currentness,
            "artifact_verification_deferred": len(state["artifact_pending"]),  # type: ignore[arg-type]
            "actions": [],
            "authority": {
                "automatic_adoption": False,
                "automatic_rejection": False,
                "automatic_implementation": False,
            },
            "network": {
                "requests": 0,
                "response_bytes": 0,
                "cache_hits": 0,
                "retry_count": 0,
                "max_requests": config.max_requests,
                "max_response_bytes": config.max_response_bytes,
                "max_wall_seconds": config.max_wall_seconds,
            },
        }

    client = BoundedNetworkClient(
        root=root,
        cache_day=as_of.date(),
        as_of=as_of_text,
        config=config,
        write_cache=apply,
        offline=offline,
        opener=opener,
        sleeper=sleeper,
        monotonic=monotonic,
    )
    discovery_writes: list[PlannedWrite] = []
    discovery_ids: set[str] = set()
    discovery_page_complete: bool | None = None
    discovery_total_results: int | None = None
    discovery_next_start: int | None = None
    discovery_pagination_restarted = False
    frontier_total_results: int | None = None
    frontier_returned = 0
    lookback = timedelta(hours=config.discovery_lookback_hours)
    frontier_start = as_of - lookback
    frontier_end = as_of
    window_start = frontier_start
    window_end = frontier_end
    discovery_start_offset = 0
    if daily_due:
        frontier_write, frontier_ids, frontier_total_results, frontier_returned = (
            _fetch_discovery_page(
                root,
                config,
                client,
                window_start=frontier_start,
                window_end=frontier_end,
                start_offset=0,
                result_limit=config.max_frontier_items,
            )
        )
        discovery_writes.append(frontier_write)
        discovery_ids.update(frontier_ids)
        if discovery_cycle_active:
            window_start = _parse_as_of(str(state["discovery_cycle_start"]))
            window_end = _parse_as_of(str(state["discovery_cycle_end"]))
            discovery_start_offset = int(state["discovery_next_start"])
            history_write, history_ids, history_total, history_returned = _fetch_discovery_page(
                root,
                config,
                client,
                window_start=window_start,
                window_end=window_end,
                start_offset=discovery_start_offset,
                result_limit=config.max_backlog_items,
            )
            discovery_writes.append(history_write)
            discovery_ids.update(history_ids)
            discovery_total_results = history_total
            candidate_next_start = discovery_start_offset + history_returned
            discovery_pagination_restarted = (
                int(state["discovery_cycle_total_results"]) != discovery_total_results
            )
            discovery_next_start = 0 if discovery_pagination_restarted else candidate_next_start
            discovery_page_complete = bool(
                not discovery_pagination_restarted
                and discovery_next_start >= discovery_total_results
            )
        else:
            previous_end_value = (
                state.get("last_discovery_window_end")
                if not discovery_history_config_reset
                else None
            )
            previous_end = (
                _parse_as_of(str(previous_end_value)) if previous_end_value is not None else None
            )
            if previous_end is not None and previous_end < frontier_start:
                window_start = previous_end - timedelta(seconds=1)
                window_end = min(as_of, window_start + lookback)
                history_write, history_ids, history_total, history_returned = _fetch_discovery_page(
                    root,
                    config,
                    client,
                    window_start=window_start,
                    window_end=window_end,
                    start_offset=0,
                    result_limit=config.max_backlog_items,
                )
                discovery_writes.append(history_write)
                discovery_ids.update(history_ids)
                discovery_total_results = history_total
                discovery_next_start = history_returned
                discovery_page_complete = discovery_next_start >= discovery_total_results
            else:
                discovery_total_results = frontier_total_results
                discovery_next_start = frontier_returned
                discovery_page_complete = discovery_next_start >= discovery_total_results

    arrival_rate = (
        float(frontier_total_results) * 24.0 / config.discovery_lookback_hours
        if frontier_total_results is not None
        else 0.0
    )
    discovery_capacity_per_day = config.max_frontier_items + config.max_backlog_items
    backlog_count = (
        max(0, int(discovery_total_results or 0) - int(discovery_next_start or 0))
        if not discovery_page_complete
        else 0
    )
    net_backfill_capacity_per_day = max(0.0, discovery_capacity_per_day - arrival_rate)
    capacity_status = (
        "runtime_blocked_capacity"
        if arrival_rate > discovery_capacity_per_day
        or (backlog_count > 0 and net_backfill_capacity_per_day <= 0)
        else "sustainable"
    )
    currentness_lag_seconds = max(0, int((as_of - window_end).total_seconds()))
    oldest_unprocessed_age_seconds = (
        max(0, int((as_of - window_start).total_seconds())) if backlog_count else 0
    )
    estimated_catch_up_runs = (
        math.ceil(backlog_count / net_backfill_capacity_per_day)
        if backlog_count and net_backfill_capacity_per_day > 0
        else 0
    )
    caught_up = backlog_count == 0 and capacity_status == "sustainable"
    currentness_status = (
        capacity_status
        if capacity_status != "sustainable"
        else ("current" if caught_up else "frontier_current_backfill_active")
    )

    known_ids = _known_base_ids(root) if weekly_due else []
    recheck_ids, recheck_complete = (
        _select_recheck_ids(known_ids, state, limit=config.max_recheck_items)
        if weekly_due
        else ([], True)
    )
    exact_ids = sorted(set(discovery_ids) | set(recheck_ids))
    if len(exact_ids) > config.max_items:
        raise RadarValidationError("Research Radar exact-ID work exceeds the item budget")
    exact_batches: list[SourceBatch] = []
    exact_versions: dict[str, dict[str, object]] = {}
    for offset in range(0, len(exact_ids), DEFAULT_PAGE_SIZE):
        page = exact_ids[offset : offset + DEFAULT_PAGE_SIZE]
        locator = exact_id_locators(page, page_size=DEFAULT_PAGE_SIZE)[0]
        fetched = client.fetch(locator, provider="arxiv_exact_id", accept="application/atom+xml")
        receipt = _mint_live_receipt(
            provider="arxiv_atom_primary",
            request_locator=locator,
            final_locator=fetched.final_locator,
            response=fetched.content,
            expected_identifiers=page,
            source=fetched.source,
        )
        batch = SourceBatch(
            locator,
            fetched.content,
            tuple(page),
            provider="arxiv_atom_primary",
            primary_metadata_verified=True,
            _receipt=receipt,
        )
        parsed = parse_arxiv_atom(batch, max_entries=len(page))
        client.persist(fetched, provider="arxiv_exact_id")
        for version in parsed["versions"]:  # type: ignore[union-attr]
            version_key = str(version["identity"]["version_key"])
            previous = exact_versions.get(version_key)
            if previous is not None and previous != version:
                raise RadarConflictError(f"conflicting exact-ID metadata for {version_key}")
            exact_versions[version_key] = version
        exact_batches.append(batch)

    ingestion_plan = (
        plan_ingestion(
            root,
            exact_batches,
            max_entries_total=config.max_items,
            max_response_bytes_total=config.max_response_bytes,
        )
        if exact_batches
        else []
    )
    artifact_records: dict[str, list[dict[str, object]]] = {
        key: _artifact_observations_for_version(root, key) for key in exact_versions
    }
    artifact_writes: list[PlannedWrite] = []
    pending_artifacts: dict[tuple[str, str], tuple[str, ArtifactCandidate, int, str | None]] = {}
    for pending_value in state["artifact_pending"]:  # type: ignore[union-attr]
        version_key, candidate, attempts, next_attempt_date = _candidate_from_pending(pending_value)
        if _artifact_has_terminal_observation(root, version_key, candidate):
            continue
        pending_artifacts[(version_key, candidate.normalized_id)] = (
            version_key,
            candidate,
            attempts,
            next_attempt_date,
        )
    new_artifact_candidates = 0
    for version_key in sorted(exact_versions):
        for candidate in extract_artifact_candidates(exact_versions[version_key]):
            key = (version_key, candidate.normalized_id)
            if key not in pending_artifacts and _artifact_has_terminal_observation(
                root, version_key, candidate
            ):
                continue
            if key not in pending_artifacts:
                new_artifact_candidates += 1
            pending_artifacts.setdefault(key, (version_key, candidate, 0, None))
    if len(pending_artifacts) > MAX_PENDING_ARTIFACTS:
        raise RadarValidationError("pending artifact queue exceeds the configured bound")
    artifact_candidates = [pending_artifacts[key] for key in sorted(pending_artifacts)]
    eligible_candidates = [
        item
        for item in artifact_candidates
        if item[3] is None or date.fromisoformat(item[3]) <= as_of.date()
    ]
    selected_candidates = (
        eligible_candidates[: config.max_artifacts] if config.verify_artifacts else []
    )
    selected_keys = {
        (version_key, candidate.normalized_id)
        for version_key, candidate, _attempts, _next_attempt in selected_candidates
    }
    pending_after = [
        _pending_artifact(
            version_key,
            candidate,
            attempts=attempts,
            next_attempt_date=next_attempt,
        )
        for version_key, candidate, attempts, next_attempt in artifact_candidates
        if (version_key, candidate.normalized_id) not in selected_keys
    ]
    manual_review_artifacts: list[str] = []
    for version_key, candidate, attempts, _next_attempt in selected_candidates:
        record = verify_artifact_candidate(
            candidate,
            version_key=version_key,
            observed_at=as_of_text,
            client=client,
        )
        if record.get("verification_status") != "verified_primary_metadata":
            if attempts >= 100:
                record["retry_disposition"] = "manual_review_required_max_attempts"
                manual_review_artifacts.append(f"{version_key}|{candidate.normalized_id}")
            else:
                retry_days = min(7, 2 ** min(attempts, 2))
                pending_after.append(
                    _pending_artifact(
                        version_key,
                        candidate,
                        attempts=attempts + 1,
                        next_attempt_date=(as_of.date() + timedelta(days=retry_days)).isoformat(),
                    )
                )
        previous_records = artifact_records.setdefault(version_key, [])
        artifact_records[version_key] = [
            item for item in previous_records if item.get("artifact_id") != candidate.normalized_id
        ] + [record]
        artifact_writes.append(artifact_planned_write(root, record))
    pending_after.sort(key=lambda item: (str(item["version_key"]), str(item["artifact_id"])))

    triage_writes: list[PlannedWrite] = []
    for version_key in sorted(exact_versions):
        triage_record = triage_version(
            exact_versions[version_key],
            profile=config.triage,
            as_of=as_of_text,
            primary_metadata_verified=True,
            artifact_records=artifact_records[version_key],
        )
        triage_writes.append(triage_planned_write(root, triage_record))

    logical_writes = [
        *discovery_writes,
        *ingestion_plan,
        *artifact_writes,
        *triage_writes,
    ]
    logical_actions = [
        {
            "kind": item.kind,
            "path": item.relative_path,
            "sha256": item.sha256,
        }
        for item in sorted(logical_writes, key=lambda item: (item.kind, item.relative_path))
    ]
    run_identity = {
        "as_of": as_of_text,
        "config_sha256": config.sha256,
        "prior_generation": state["generation"],
        "daily_discovery_due": daily_due,
        "weekly_version_recheck_due": weekly_due,
        "discovery_start_offset": discovery_start_offset if daily_due else None,
        "actions": logical_actions,
    }
    run_id = "radar-" + _digest_id(run_identity)
    run_record = {
        "schema": SYNC_RUN_SCHEMA,
        "run_id": run_id,
        **run_identity,
        "window": (
            {"start": _iso_z(window_start), "end": _iso_z(window_end)} if daily_due else None
        ),
        "discovery_page_complete": discovery_page_complete,
        "discovery_total_results": discovery_total_results,
        "discovery_next_start": discovery_next_start,
        "discovery_pagination_restarted": discovery_pagination_restarted,
        "discovery_cycle_config_reset": discovery_cycle_config_reset,
        "discovery_history_config_reset": discovery_history_config_reset,
        "currentness": {
            "status": currentness_status,
            "caught_up": caught_up,
            "backlog_count": backlog_count,
            "oldest_unprocessed_age_seconds": oldest_unprocessed_age_seconds,
            "currentness_lag_seconds": currentness_lag_seconds,
            "arrival_rate_per_day": arrival_rate,
            "frontier_capacity_per_day": config.max_frontier_items,
            "discovery_capacity_per_day": discovery_capacity_per_day,
            "net_backfill_capacity_per_day": net_backfill_capacity_per_day,
            "backlog_capacity_per_run": config.max_backlog_items,
            "estimated_catch_up_runs": estimated_catch_up_runs,
        },
        "discovered_base_ids": sorted(discovery_ids),
        "rechecked_base_ids": recheck_ids,
        "recheck_cycle_complete": recheck_complete if weekly_due else None,
        "artifact_candidates": len(artifact_candidates),
        "new_artifact_candidates": new_artifact_candidates,
        "artifact_verification_selected": len(selected_candidates),
        "artifact_verification_deferred": len(pending_after),
        "artifact_manual_review_required": manual_review_artifacts,
        "network_policy": {
            "max_requests": config.max_requests,
            "max_response_bytes": config.max_response_bytes,
            "max_wall_seconds": config.max_wall_seconds,
            "retry_count": 0,
            "cache_scope": "utc_day_content_addressed",
        },
        "authority": {
            "automatic_adoption": False,
            "automatic_rejection": False,
            "automatic_implementation": False,
        },
    }
    run_content = _canonical_bytes(run_record)
    run_write = PlannedWrite(
        "sync_run",
        f"sync/runs/{run_id}-{hashlib.sha256(run_content).hexdigest()}.json",
        run_content,
        "create",
    )

    write_results = {"created": 0, "unchanged": 0}
    if apply:
        if discovery_writes:
            result = _apply_auxiliary_writes(root, discovery_writes)
            write_results["created"] += result["created"]
            write_results["unchanged"] += result["unchanged"]
        if ingestion_plan:
            result = apply_plan(root, ingestion_plan)
            write_results["created"] += result["created"]
            write_results["unchanged"] += result["unchanged"]
        result = _apply_auxiliary_writes(root, [*artifact_writes, *triage_writes, run_write])
        write_results["created"] += result["created"]
        write_results["unchanged"] += result["unchanged"]
        next_state = dict(state)
        next_state["generation"] = int(state["generation"]) + 1
        next_state["last_run_id"] = run_id
        next_state["artifact_pending"] = pending_after
        if daily_due:
            if discovery_page_complete:
                next_state["last_discovery_window_end"] = _iso_z(window_end)
                next_state["last_discovery_config_sha256"] = config.sha256
                next_state["discovery_cycle_start"] = None
                next_state["discovery_cycle_end"] = None
                next_state["discovery_next_start"] = None
                next_state["discovery_cycle_total_results"] = None
                next_state["discovery_cycle_config_sha256"] = None
                next_state["last_daily_discovery_date"] = day_text
            else:
                next_state["discovery_cycle_start"] = _iso_z(window_start)
                next_state["discovery_cycle_end"] = _iso_z(window_end)
                next_state["discovery_next_start"] = discovery_next_start
                next_state["discovery_cycle_total_results"] = discovery_total_results
                next_state["discovery_cycle_config_sha256"] = config.sha256
                next_state["last_daily_discovery_date"] = None
        if weekly_due:
            if recheck_complete:
                next_state["last_version_recheck_completed_at"] = as_of_text
                next_state["recheck_cycle_started_at"] = None
                next_state["recheck_after_base_id"] = None
            else:
                next_state["recheck_cycle_started_at"] = (
                    state.get("recheck_cycle_started_at") or as_of_text
                )
                next_state["recheck_after_base_id"] = recheck_ids[-1]
        _write_sync_state(root, next_state)

    top_status = (
        "runtime_blocked"
        if currentness_status == "runtime_blocked_capacity"
        else ("applied" if apply else "planned")
    )
    return {
        "status": top_status,
        "write_outcome": "applied" if apply else "not_applied",
        "mode": "explicit_apply" if apply else "dry_run_no_writes",
        "root": str(root),
        "as_of": as_of_text,
        "run_id": run_id,
        "daily_discovery_due": daily_due,
        "weekly_version_recheck_due": weekly_due,
        "recheck_cycle_complete": recheck_complete if weekly_due else None,
        "discovery_page_complete": discovery_page_complete,
        "discovery_total_results": discovery_total_results,
        "discovery_next_start": discovery_next_start,
        "discovery_pagination_restarted": discovery_pagination_restarted,
        "discovery_cycle_config_reset": discovery_cycle_config_reset,
        "discovery_history_config_reset": discovery_history_config_reset,
        "currentness": {
            "status": currentness_status,
            "caught_up": caught_up,
            "backlog_count": backlog_count,
            "oldest_unprocessed_age_seconds": oldest_unprocessed_age_seconds,
            "currentness_lag_seconds": currentness_lag_seconds,
            "arrival_rate_per_day": arrival_rate,
            "frontier_capacity_per_day": config.max_frontier_items,
            "discovery_capacity_per_day": discovery_capacity_per_day,
            "net_backfill_capacity_per_day": net_backfill_capacity_per_day,
            "backlog_capacity_per_run": config.max_backlog_items,
            "estimated_catch_up_runs": estimated_catch_up_runs,
        },
        "discovered_items": len(discovery_ids),
        "rechecked_items": len(recheck_ids),
        "exact_versions": len(exact_versions),
        "artifact_candidates": len(artifact_candidates),
        "new_artifact_candidates": new_artifact_candidates,
        "artifact_verification_selected": len(selected_candidates),
        "artifact_verification_deferred": len(pending_after),
        "artifact_manual_review_required": manual_review_artifacts,
        "triage_records": len(triage_writes),
        "network": client.report(),
        "write_results": write_results if apply else None,
        "actions": logical_actions,
        "authority": {
            "automatic_adoption": False,
            "automatic_rejection": False,
            "automatic_implementation": False,
        },
    }


def sync_research_radar(
    root: Path,
    config: RadarSyncConfig,
    *,
    as_of: str | datetime,
    apply: bool = False,
    offline: bool = False,
    opener: Callable[..., object] = _safe_urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if type(apply) is not bool or type(offline) is not bool:
        raise RadarValidationError("Radar apply and offline flags must be exact booleans")
    parsed_as_of = _parse_as_of(as_of)
    if apply:
        with _sync_write_lock(root):
            return _sync_research_radar_once(
                root,
                config,
                as_of=parsed_as_of,
                apply=True,
                offline=offline,
                opener=opener,
                sleeper=sleeper,
                monotonic=monotonic,
            )
    return _sync_research_radar_once(
        root,
        config,
        as_of=parsed_as_of,
        apply=False,
        offline=offline,
        opener=opener,
        sleeper=sleeper,
        monotonic=monotonic,
    )
