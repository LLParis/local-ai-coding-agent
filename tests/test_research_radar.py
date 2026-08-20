from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from threading import Lock
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

import agent_continuity.research_radar as radar_module  # noqa: E402
from agent_continuity.cli import main  # noqa: E402
from agent_continuity.research_radar import (  # noqa: E402
    LIFECYCLE_SCHEMA,
    MIN_PAGE_INTERVAL_SECONDS,
    PlannedWrite,
    RadarConflictError,
    RadarNetworkError,
    RadarValidationError,
    SourceBatch,
    apply_plan,
    apply_transition,
    discovery_locator,
    extract_artifact_candidates,
    fetch_arxiv_batches,
    load_lifecycle,
    load_sync_config,
    parse_arxiv_atom,
    parse_arxiv_id,
    plan_ingestion,
    plan_transition,
    read_atom_file,
    sync_research_radar,
    triage_version,
)

FIXTURES = PACKAGE / "tests" / "fixtures" / "research_radar"
FEED = FIXTURES / "arxiv_feed.xml"
CONFLICT_FEED = FIXTURES / "arxiv_feed_conflict.xml"
CONTINUOUS_FEED = FIXTURES / "continuous_feed.xml"
EMPTY_FEED = FIXTURES / "empty_feed.xml"
SYNC_CONFIG = FIXTURES / "sync_config.json"
GITHUB_REPO = FIXTURES / "github_repo.json"
GITHUB_COMMIT = FIXTURES / "github_commit.json"
HF_MODEL = FIXTURES / "hf_model.json"
PAGED_FEED_0 = FIXTURES / "paged_feed_0.xml"
PAGED_FEED_1 = FIXTURES / "paged_feed_1.xml"
WINDOWS = PACKAGE / "windows"


class FakeResponse:
    def __init__(self, content: bytes, *, url: str = "https://export.arxiv.org/api/query"):
        self.content = content
        self.url = url
        self.headers = {
            "Content-Type": "application/json"
            if content.startswith(b"{")
            else "application/atom+xml"
        }

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.content[:limit]

    def geturl(self) -> str:
        return self.url


class ResearchRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "radar"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: object) -> tuple[int, dict[str, object] | None, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(argument) for argument in arguments])
        text = stdout.getvalue().strip()
        return code, json.loads(text) if text else None, stderr.getvalue()

    def apply_fixture(self) -> None:
        plan = plan_ingestion(self.root, [read_atom_file(FEED)])
        result = apply_plan(self.root, plan)
        self.assertEqual(result, {"created": 6, "unchanged": 0})

    def primary_fixture_batch(self) -> SourceBatch:
        xml_root = ET.fromstring(FEED.read_bytes())
        atom = "{http://www.w3.org/2005/Atom}"
        entries = xml_root.findall(f"{atom}entry")
        xml_root.remove(entries[0])
        single = ET.tostring(xml_root, encoding="utf-8", xml_declaration=True)

        def open_fixture(request: object, *, timeout: float) -> FakeResponse:
            return FakeResponse(single, url=request.full_url)  # type: ignore[attr-defined]

        return fetch_arxiv_batches(["2601.00001v2"], opener=open_fixture, sleeper=lambda _: None)[0]

    def apply_primary_fixture(self) -> None:
        plan = plan_ingestion(self.root, [self.primary_fixture_batch()])
        self.assertEqual(apply_plan(self.root, plan), {"created": 6, "unchanged": 0})

    def transition(self, from_status: str, to_status: str, sequence: int) -> None:
        evidence = (
            [self.evidence_sha()]
            if to_status
            in {
                "accepted",
                "rejected",
                "reproduced",
                "failed",
                "adopted",
                "evaluator_invalid",
                "runtime_blocked",
            }
            else []
        )
        revisit = (
            f"revisit-{sequence}"
            if to_status
            in {
                "watch",
                "deferred",
                "rejected",
                "inconclusive",
                "evaluator_invalid",
                "runtime_blocked",
            }
            else None
        )
        planned = plan_transition(
            self.root,
            "2601.00001v2",
            expected_from=from_status,
            to_status=to_status,
            owner="test-operator",
            reason=f"transition-{sequence}",
            evidence=evidence,
            revisit_trigger=revisit,
            occurred_at=f"2026-08-20T08:{sequence:02d}:00Z",
        )
        self.assertEqual(apply_transition(self.root, planned, expected_from=from_status), "created")

    def evidence_sha(self) -> str:
        version = json.loads(
            (self.root / "papers" / "2601.00001" / "versions" / "v2.json").read_text(
                encoding="utf-8"
            )
        )
        return version["artifact_integrity"]["atom_entry"]["sha256"]

    def sync_opener(
        self,
        calls: list[str],
        *,
        discovery: bytes | None = None,
        fail_artifacts: bool = False,
        fail_commit: bool = False,
        mismatch_github: bool = False,
    ) -> object:
        call_lock = Lock()

        def open_fixture(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 5.0)
            locator = request.full_url  # type: ignore[attr-defined]
            with call_lock:
                calls.append(locator)
            if "export.arxiv.org" in locator:
                content = (
                    discovery
                    if "search_query=" in locator and discovery is not None
                    else CONTINUOUS_FEED.read_bytes()
                )
            elif fail_artifacts:
                raise OSError("fixture network failure")
            elif "api.github.com/repos/ExampleOrg/RadarCode/commits/" in locator:
                if fail_commit:
                    raise OSError("fixture commit failure")
                content = GITHUB_COMMIT.read_bytes()
            elif "api.github.com/repos/ExampleOrg/RadarCode" in locator:
                if mismatch_github:
                    value = json.loads(GITHUB_REPO.read_text(encoding="utf-8"))
                    value["full_name"] = "OtherOrg/OtherRepo"
                    value["html_url"] = "https://github.com/OtherOrg/OtherRepo"
                    content = json.dumps(value).encode()
                else:
                    content = GITHUB_REPO.read_bytes()
            elif "huggingface.co/api/models/ExampleOrg/RadarModel" in locator:
                content = HF_MODEL.read_bytes()
            else:
                raise AssertionError(f"unexpected fixture request: {locator}")
            return FakeResponse(content, url=locator)

        return open_fixture

    def combined_paged_feed(self, *, total_results: int = 2) -> bytes:
        root = ET.fromstring(PAGED_FEED_0.read_bytes())
        second_root = ET.fromstring(PAGED_FEED_1.read_bytes())
        atom = "{http://www.w3.org/2005/Atom}"
        second_entry = second_root.find(f"{atom}entry")
        assert second_entry is not None
        root.append(ET.fromstring(ET.tostring(second_entry)))
        root.find(f"{radar_module.OPENSEARCH}totalResults").text = str(  # type: ignore[union-attr]
            total_results
        )
        root.find(f"{radar_module.OPENSEARCH}startIndex").text = "0"  # type: ignore[union-attr]
        root.find(f"{radar_module.OPENSEARCH}itemsPerPage").text = "2"  # type: ignore[union-attr]
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def test_arxiv_identity_is_version_aware_and_rejects_foreign_locators(self) -> None:
        current = parse_arxiv_id("https://arxiv.org/abs/2601.00001v12")
        self.assertEqual((current.base_id, current.version), ("2601.00001", 12))
        legacy = parse_arxiv_id("math.GT/0309136v2")
        self.assertEqual((legacy.base_id, legacy.version), ("math.gt/0309136", 2))
        self.assertEqual(parse_arxiv_id("2601.00001").version, None)
        with self.assertRaisesRegex(RadarValidationError, "not an arXiv locator"):
            parse_arxiv_id("https://example.com/abs/2601.00001v1")
        with self.assertRaisesRegex(RadarValidationError, "unsupported.*scheme"):
            parse_arxiv_id("file://arxiv.org/abs/2601.00001v1")
        with self.assertRaisesRegex(RadarValidationError, "missing an immutable version"):
            parse_arxiv_id("2601.00001", require_version=True)

    def test_atom_normalization_preserves_versions_lineage_and_untrusted_data(self) -> None:
        parsed = parse_arxiv_atom(read_atom_file(FEED), max_entries=2)
        self.assertEqual(len(parsed["papers"]), 1)
        versions = parsed["versions"]
        self.assertEqual(
            [version["identity"]["version_key"] for version in versions],
            ["arxiv:2601.00001:v1", "arxiv:2601.00001:v2"],
        )
        self.assertIsNone(versions[0]["lineage"]["previous_version_key"])
        self.assertEqual(versions[1]["lineage"]["previous_version_key"], "arxiv:2601.00001:v1")
        self.assertIn("execute install.sh", versions[0]["abstract"])
        self.assertTrue(versions[0]["trust"]["instructions_are_data"])
        self.assertEqual(versions[0]["artifact_integrity"]["pdf"]["status"], "not_fetched")
        self.assertEqual(versions[0]["doi"]["normalized"], "10.1234/example.one")
        self.assertTrue(versions[0]["status_observations"]["cross_listed"])

    def test_cli_defaults_to_no_write_dry_run_and_does_not_echo_abstract(self) -> None:
        code, report, error = self.run_cli("radar-ingest", "--root", self.root, "--atom-file", FEED)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(report["mode"], "dry_run_no_writes")
        self.assertEqual(report["counts"], {"create": 6, "unchanged": 0})
        self.assertFalse(self.root.exists())
        self.assertNotIn("install.sh", json.dumps(report))

    def test_explicit_apply_is_immutable_and_idempotent(self) -> None:
        code, report, error = self.run_cli(
            "radar-ingest", "--root", self.root, "--atom-file", FEED, "--apply"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual((report["created"], report["unchanged"]), (6, 0))
        v1 = self.root / "papers" / "2601.00001" / "versions" / "v1.json"
        first = v1.read_bytes()

        code, report, error = self.run_cli(
            "radar-ingest", "--root", self.root, "--atom-file", FEED, "--apply"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual((report["created"], report["unchanged"]), (0, 6))
        self.assertEqual(v1.read_bytes(), first)
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v1"))
        self.assertEqual(records[-1][1]["to_status"], "new")
        observation = json.loads(next((self.root / "observations").rglob("*.json")).read_text())
        self.assertEqual(observation["provider"], "offline_atom_fixture")
        self.assertFalse(observation["primary_metadata_verified"])
        before = list(self.root.rglob("*.json"))
        code, report, error = self.run_cli(
            "radar-transition",
            "--root",
            self.root,
            "--arxiv-id",
            "2601.00001v2",
            "--from-status",
            "new",
            "--to-status",
            "metadata_verified",
            "--owner",
            "operator",
            "--reason",
            "attempted offline promotion",
            "--evidence",
            self.evidence_sha(),
            "--occurred-at",
            "2026-08-20T10:00:00Z",
            "--apply",
        )
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("reserved for a matching live", error)
        self.assertEqual(list(self.root.rglob("*.json")), before)

    def test_interrupted_intake_is_idempotently_completed(self) -> None:
        initial = plan_ingestion(self.root, [read_atom_file(FEED)])
        partial = [item for item in initial if item.kind != "lifecycle"]
        self.assertEqual(apply_plan(self.root, partial), {"created": 4, "unchanged": 0})
        repaired = plan_ingestion(self.root, [read_atom_file(FEED)])
        self.assertEqual(
            sum(item.kind == "lifecycle" and item.state == "create" for item in repaired), 2
        )
        self.assertEqual(apply_plan(self.root, repaired), {"created": 2, "unchanged": 4})

    def test_live_exact_id_can_verify_metadata_but_offline_fixture_cannot(self) -> None:
        self.apply_primary_fixture()
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        self.assertEqual(
            [record[1]["to_status"] for record in records], ["new", "metadata_verified"]
        )
        observation = json.loads(next((self.root / "observations").rglob("*.json")).read_text())
        self.assertEqual(observation["provider"], "arxiv_atom_primary")
        self.assertTrue(observation["primary_metadata_verified"])

    def test_offline_primary_self_declaration_has_zero_live_authority(self) -> None:
        live = self.primary_fixture_batch()
        manual = SourceBatch(
            live.source_locator,
            live.content,
            live.expected_identifiers,
            provider="arxiv_atom_primary",
            primary_metadata_verified=True,
        )
        parsed = parse_arxiv_atom(manual, max_entries=1)
        self.assertFalse(parsed["observation"]["primary_metadata_verified"])
        plan = plan_ingestion(self.root, [manual])
        self.assertFalse(
            any(
                item.kind == "lifecycle"
                and json.loads(item.content)["to_status"] == "metadata_verified"
                for item in plan
            )
        )
        apply_plan(self.root, plan)
        lifecycle = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        self.assertEqual([item[1]["to_status"] for item in lifecycle], ["new"])

    def test_live_authority_is_plan_bound_and_forged_observation_writes_nothing(self) -> None:
        plan = plan_ingestion(self.root, [self.primary_fixture_batch()])
        primary_observation = next(item for item in plan if item.kind == "source_observation")
        with self.assertRaisesRegex(
            RadarValidationError, "primary source observation lacks bound live-plan"
        ):
            apply_plan(self.root, [primary_observation])
        self.assertFalse(self.root.exists())
        stripped = list(plan)
        before = list(self.root.rglob("*")) if self.root.exists() else []
        with self.assertRaisesRegex(RadarValidationError, "live-plan authority"):
            apply_plan(self.root, stripped)
        after = list(self.root.rglob("*")) if self.root.exists() else []
        self.assertEqual(after, before)

    def test_unresolved_legacy_authority_requires_exact_refetch_receipt_migration(self) -> None:
        self.apply_primary_fixture()
        identity = parse_arxiv_id("2601.00001v2")
        lifecycle_before = [
            path.read_bytes() for path, _value, _sha in load_lifecycle(self.root, identity)
        ]
        receipts = list((self.root / "authority").rglob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipts[0].unlink()
        with self.assertRaisesRegex(RadarValidationError, "lacks live exact-ID authority"):
            load_lifecycle(self.root, identity)

        migration = plan_ingestion(self.root, [self.primary_fixture_batch()])
        result = apply_plan(self.root, migration)
        self.assertGreaterEqual(result["created"], 1)
        lifecycle_after = [
            path.read_bytes() for path, _value, _sha in load_lifecycle(self.root, identity)
        ]
        self.assertEqual(lifecycle_after, lifecycle_before)
        self.assertEqual(len(list((self.root / "authority").rglob("*.json"))), 1)

    def test_aggregate_file_count_entries_and_bytes_are_bounded(self) -> None:
        batch = read_atom_file(FEED)
        with self.assertRaisesRegex(RadarValidationError, "batch count|aggregate"):
            plan_ingestion(self.root, [batch, batch], max_entries_total=1)
        with self.assertRaisesRegex(RadarValidationError, "aggregate byte"):
            plan_ingestion(
                self.root,
                [
                    SourceBatch("fixture:a", b"x" * (2 * 1024 * 1024 + 1)),
                    SourceBatch("fixture:b", b"x" * (2 * 1024 * 1024 + 1)),
                ],
            )
        code, report, error = self.run_cli(
            "radar-ingest",
            "--root",
            self.root,
            "--atom-file",
            FEED,
            "--atom-file",
            FEED,
            "--max-response-bytes",
            FEED.stat().st_size + 1,
        )
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("byte limit", error)
        self.assertFalse(self.root.exists())

    def test_conflicting_duplicate_or_existing_version_is_not_overwritten(self) -> None:
        original = read_atom_file(FEED)
        conflict = read_atom_file(CONFLICT_FEED)
        with self.assertRaisesRegex(RadarConflictError, "conflicting candidate records"):
            plan_ingestion(self.root, [original, conflict])

        self.apply_fixture()
        before = (self.root / "papers" / "2601.00001" / "versions" / "v2.json").read_bytes()
        with self.assertRaisesRegex(RadarConflictError, "immutable .*conflict"):
            plan_ingestion(self.root, [conflict])
        after = (self.root / "papers" / "2601.00001" / "versions" / "v2.json").read_bytes()
        self.assertEqual(after, before)

    def test_untrusted_xml_and_resource_overflow_fail_before_storage(self) -> None:
        unsafe = b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x "bad">]><feed/>'
        with self.assertRaisesRegex(RadarValidationError, "forbidden XML"):
            parse_arxiv_atom(SourceBatch("fixture:unsafe", unsafe), max_entries=1)
        utf16 = (
            '<?xml version="1.0" encoding="utf-16"?>'
            '<!DOCTYPE feed [<!ENTITY x "expanded">]><feed>&x;</feed>'
        ).encode("utf-16")
        with self.assertRaisesRegex(RadarValidationError, "must be UTF-8"):
            parse_arxiv_atom(SourceBatch("fixture:utf16", utf16), max_entries=1)
        false_declaration = b'<?xml version="1.0" encoding="iso-8859-1"?><feed/>'
        with self.assertRaisesRegex(RadarValidationError, "non-UTF-8"):
            parse_arxiv_atom(
                SourceBatch("fixture:wrong-encoding", false_declaration), max_entries=1
            )
        with self.assertRaisesRegex(RadarValidationError, "entry limit"):
            parse_arxiv_atom(read_atom_file(FEED), max_entries=1)
        self.assertFalse(self.root.exists())

    def test_discovery_requires_complete_pagination_and_unique_version_entries(self) -> None:
        missing = CONTINUOUS_FEED.read_text(encoding="utf-8").replace(
            "  <opensearch:totalResults>1</opensearch:totalResults>\n", ""
        )
        with self.assertRaisesRegex(RadarValidationError, "missing required OpenSearch"):
            parse_arxiv_atom(
                SourceBatch(
                    "fixture:missing-pagination",
                    missing.encode(),
                    provider="arxiv_atom_discovery",
                    allow_empty=True,
                ),
                max_entries=4,
            )
        root = ET.fromstring(CONTINUOUS_FEED.read_bytes())
        atom = "{http://www.w3.org/2005/Atom}"
        entry = root.find(f"{atom}entry")
        assert entry is not None
        root.append(ET.fromstring(ET.tostring(entry)))
        root.find(f"{radar_module.OPENSEARCH}totalResults").text = "2"  # type: ignore[union-attr]
        root.find(f"{radar_module.OPENSEARCH}itemsPerPage").text = "2"  # type: ignore[union-attr]
        duplicate = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(RadarValidationError, "duplicate arXiv version"):
            parse_arxiv_atom(SourceBatch("fixture:duplicate", duplicate), max_entries=2)

    def test_redirect_target_is_rejected_before_response_body_read(self) -> None:
        config = load_sync_config(SYNC_CONFIG)

        class RedirectedResponse(FakeResponse):
            def __init__(self) -> None:
                super().__init__(b"should-not-be-read", url="https://evil.example/steal")
                self.read_called = False

            def read(self, limit: int) -> bytes:
                self.read_called = True
                return super().read(limit)

        response = RedirectedResponse()

        def redirecting_opener(request: object, *, timeout: float) -> RedirectedResponse:
            return response

        client = radar_module.BoundedNetworkClient(
            root=self.root,
            cache_day=radar_module.date(2026, 8, 20),
            as_of="2026-08-20T10:00:00Z",
            config=config,
            write_cache=False,
            offline=False,
            opener=redirecting_opener,
            sleeper=lambda _: None,
        )
        with self.assertRaisesRegex(RadarNetworkError, "outside the network allowlist"):
            client.fetch(
                "https://api.github.com/repos/Example/Repo",
                provider="github",
                accept="application/json",
            )
        self.assertFalse(response.read_called)
        handler = radar_module._NoRedirectHandler()
        request = urllib.request.Request("https://export.arxiv.org/api/query")
        self.assertIsNone(
            handler.redirect_request(
                request,
                object(),
                302,
                "Found",
                {},
                "https://evil.example/steal",
            )
        )

    def test_every_planned_write_kind_binds_canonical_content_and_path(self) -> None:
        base_items = list(plan_ingestion(self.root, [read_atom_file(FEED)]))
        primary_receipt = next(
            item
            for item in plan_ingestion(self.root, [self.primary_fixture_batch()])
            if item.kind == "primary_receipt"
        )
        parsed = parse_arxiv_atom(read_atom_file(CONTINUOUS_FEED), max_entries=1)
        version = parsed["versions"][0]
        config = load_sync_config(SYNC_CONFIG)
        triage = triage_version(
            version,
            profile=config.triage,
            as_of="2026-08-20T10:00:00Z",
            primary_metadata_verified=False,
            artifact_records=[],
        )
        triage_item = radar_module.triage_planned_write(self.root, triage)
        candidate = extract_artifact_candidates(version)[0]
        artifact = radar_module._artifact_base_record(
            candidate=candidate,
            version_key="arxiv:2608.00001:v1",
            observed_at="2026-08-20T10:00:00Z",
        )
        artifact.update(
            {
                "verification_status": "unverified_not_checked",
                "canonical_id": None,
                "canonical_url": None,
                "revision": None,
                "revision_kind": None,
                "license": {"status": "unverified", "value": None},
                "primary_responses": [],
                "trust": {
                    "classification": "untrusted_external_metadata",
                    "instructions_are_data": True,
                    "execution_permitted": False,
                },
            }
        )
        artifact_item = radar_module.artifact_planned_write(self.root, artifact)
        run_value = {
            "schema": radar_module.SYNC_RUN_SCHEMA,
            "run_id": "radar-" + "0" * 64,
        }
        run_content = radar_module._canonical_bytes(run_value)
        run_item = PlannedWrite(
            "sync_run",
            f"sync/runs/{run_value['run_id']}-{hashlib.sha256(run_content).hexdigest()}.json",
            run_content,
            "create",
        )
        all_items = [*base_items, primary_receipt, artifact_item, triage_item, run_item]
        self.assertEqual(
            {item.kind for item in all_items},
            {
                "paper",
                "paper_version",
                "source_observation",
                "primary_receipt",
                "lifecycle",
                "artifact",
                "triage",
                "sync_run",
            },
        )
        for item in all_items:
            radar_module._validate_planned_write(self.root, item)
            altered = replace(item, relative_path="alternate/" + item.relative_path)
            with self.assertRaisesRegex(RadarValidationError, "path does not match"):
                radar_module._validate_planned_write(self.root, altered)

    def test_apply_preflight_rejects_traversal_absolute_and_symlink_before_write(self) -> None:
        plan = plan_ingestion(self.root, [read_atom_file(FEED)])
        paper = next(item for item in plan if item.kind == "paper")
        outside = Path(self.temporary.name) / "escaped.json"
        for relative in ("../escaped.json", str(outside).replace("\\", "/")):
            with self.assertRaisesRegex(RadarValidationError, "escapes|POSIX-relative"):
                apply_plan(self.root, [replace(paper, relative_path=relative)])
            self.assertFalse(outside.exists())
            self.assertFalse(self.root.exists())

        self.root.mkdir(parents=True)
        outside_directory = Path(self.temporary.name) / "outside"
        outside_directory.mkdir()
        try:
            os.symlink(outside_directory, self.root / "papers", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaisesRegex(RadarValidationError, "symlink or junction"):
            apply_plan(self.root, plan)
        self.assertEqual(list(outside_directory.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction containment")
    def test_apply_preflight_rejects_windows_junction_ancestor(self) -> None:
        plan = plan_ingestion(self.root, [read_atom_file(FEED)])
        self.root.mkdir(parents=True)
        outside = Path(self.temporary.name) / "junction-outside"
        outside.mkdir()
        link = self.root / "papers"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"junction creation unavailable: {created.stderr}")
        try:
            with self.assertRaisesRegex(RadarValidationError, "symlink or junction"):
                apply_plan(self.root, plan)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            os.rmdir(link)

    def test_transition_and_auxiliary_apply_preflight_rejects_alternate_paths(self) -> None:
        self.apply_primary_fixture()
        transition = plan_transition(
            self.root,
            "2601.00001v2",
            expected_from="metadata_verified",
            to_status="screened",
            owner="operator",
            reason="path preflight",
            evidence=[],
            revisit_trigger=None,
            occurred_at="2026-08-20T10:00:00Z",
        )
        with self.assertRaisesRegex(RadarValidationError, "path does not match"):
            apply_transition(
                self.root,
                replace(transition, relative_path="alternate/" + transition.relative_path),
                expected_from="metadata_verified",
            )
        lifecycle = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        self.assertEqual(lifecycle[-1][1]["to_status"], "metadata_verified")

        parsed = parse_arxiv_atom(read_atom_file(CONTINUOUS_FEED), max_entries=1)
        triage = triage_version(
            parsed["versions"][0],
            profile=load_sync_config(SYNC_CONFIG).triage,
            as_of="2026-08-20T10:00:00Z",
            primary_metadata_verified=False,
            artifact_records=[],
        )
        planned = radar_module.triage_planned_write(self.root, triage)
        before = set(self.root.rglob("*.json"))
        with self.assertRaisesRegex(RadarValidationError, "path does not match"):
            radar_module._apply_auxiliary_writes(
                self.root,
                [replace(planned, relative_path="alternate/" + planned.relative_path)],
            )
        self.assertEqual(set(self.root.rglob("*.json")), before)

    def test_live_adapter_is_exact_id_bounded_and_rate_limited_between_pages(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []

        def open_fixture(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 5.0)
            calls.append(request.full_url)  # type: ignore[attr-defined]
            return FakeResponse(FEED.read_bytes(), url=request.full_url)  # type: ignore[attr-defined]

        batches = fetch_arxiv_batches(
            ["2601.00002", "2601.00001"],
            max_ids=2,
            page_size=1,
            timeout_seconds=5.0,
            opener=open_fixture,
            sleeper=sleeps.append,
        )
        self.assertEqual(len(batches), 2)
        self.assertEqual(sleeps, [MIN_PAGE_INTERVAL_SECONDS])
        self.assertIn("id_list=2601.00001", calls[0])
        self.assertIn("id_list=2601.00002", calls[1])
        with self.assertRaisesRegex(RadarValidationError, "duplicate"):
            fetch_arxiv_batches(["2601.00001", "2601.00001"], opener=open_fixture)
        with self.assertRaisesRegex(RadarValidationError, "did not resolve exactly one"):
            parse_arxiv_atom(
                SourceBatch("fixture:unexpected", FEED.read_bytes(), ("2601.99999",)),
                max_entries=2,
            )

    def test_evaluator_invalid_is_recorded_without_becoming_rejection(self) -> None:
        self.apply_primary_fixture()
        self.transition("metadata_verified", "screened", 3)
        self.transition("screened", "evidence_extracted", 4)
        self.transition("evidence_extracted", "decision_required", 5)
        self.transition("decision_required", "experiment_candidate", 6)
        with self.assertRaisesRegex(RadarValidationError, "governed experiment capsule"):
            plan_transition(
                self.root,
                "2601.00001v2",
                expected_from="experiment_candidate",
                to_status="accepted",
                owner="test-operator",
                reason="unsupported promotion",
                evidence=[self.evidence_sha()],
                revisit_trigger=None,
                occurred_at="2026-08-20T08:07:00Z",
            )
        self.transition("experiment_candidate", "evaluator_invalid", 7)
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        self.assertEqual(records[-1][1]["to_status"], "evaluator_invalid")
        self.assertEqual(records[-1][1]["verdict_class"], "evaluator_invalid")
        self.assertNotIn("rejected", [record[1]["to_status"] for record in records])

        retry = plan_transition(
            self.root,
            "2601.00001v2",
            expected_from="evaluator_invalid",
            to_status="experiment_candidate",
            owner="test-operator",
            reason="Evaluator repaired and controls validated.",
            evidence=[self.evidence_sha()],
            revisit_trigger=None,
            occurred_at="2026-08-20T08:08:00Z",
        )
        self.assertEqual(json.loads(retry.content)["from_status"], "evaluator_invalid")
        with self.assertRaisesRegex(RadarConflictError, "stale lifecycle"):
            plan_transition(
                self.root,
                "2601.00001v2",
                expected_from="runtime_blocked",
                to_status="experiment_candidate",
                owner="test-operator",
                reason="stale negative verdict",
                evidence=[self.evidence_sha()],
                revisit_trigger=None,
                occurred_at="2026-08-20T08:09:00Z",
            )

    def test_negative_and_blocked_classifications_require_revisit_and_evidence(self) -> None:
        self.apply_primary_fixture()
        self.transition("metadata_verified", "screened", 3)
        with self.assertRaisesRegex(RadarValidationError, "revisit trigger"):
            plan_transition(
                self.root,
                "2601.00001v2",
                expected_from="screened",
                to_status="rejected",
                owner="test-operator",
                reason="unsupported claim",
                evidence=[self.evidence_sha()],
                revisit_trigger=None,
                occurred_at="2026-08-20T08:04:00Z",
            )
        with self.assertRaisesRegex(RadarValidationError, "requires evidence"):
            plan_transition(
                self.root,
                "2601.00001v2",
                expected_from="screened",
                to_status="rejected",
                owner="test-operator",
                reason="unsupported claim",
                evidence=[],
                revisit_trigger="new independent reproduction",
                occurred_at="2026-08-20T08:04:00Z",
            )

    def test_cli_transition_is_dry_run_by_default(self) -> None:
        self.apply_primary_fixture()
        before = list(self.root.rglob("*.json"))
        code, report, error = self.run_cli(
            "radar-transition",
            "--root",
            self.root,
            "--arxiv-id",
            "2601.00001v2",
            "--from-status",
            "metadata_verified",
            "--to-status",
            "screened",
            "--owner",
            "operator",
            "--reason",
            "Applicable to active context packing research.",
            "--occurred-at",
            "2026-08-20T09:00:00Z",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(report["mode"], "dry_run_no_writes")
        self.assertEqual(list(self.root.rglob("*.json")), before)

    def test_concurrent_transition_is_atomic_and_leaves_one_valid_event(self) -> None:
        self.apply_primary_fixture()
        plans = [
            plan_transition(
                self.root,
                "2601.00001v2",
                expected_from="metadata_verified",
                to_status="screened",
                owner=f"operator-{index}",
                reason=f"concurrent decision {index}",
                evidence=[self.evidence_sha()],
                revisit_trigger=None,
                occurred_at=f"2026-08-20T10:0{index}:00Z",
            )
            for index in (1, 2)
        ]

        def apply(planned: object) -> str:
            try:
                return apply_transition(self.root, planned, expected_from="metadata_verified")
            except RadarConflictError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, plans))
        self.assertEqual(sorted(results), ["created", "stale"])
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        self.assertEqual(len(records), 3)
        self.assertEqual(records[-1][1]["to_status"], "screened")

    def test_crafted_terminal_plan_is_rejected_before_ledger_write(self) -> None:
        self.apply_primary_fixture()
        self.transition("metadata_verified", "screened", 3)
        self.transition("screened", "evidence_extracted", 4)
        self.transition("evidence_extracted", "decision_required", 5)
        self.transition("decision_required", "experiment_candidate", 6)
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        event = {
            "schema": LIFECYCLE_SCHEMA,
            "paper_version_key": "arxiv:2601.00001:v2",
            "sequence": 7,
            "previous_record_sha256": records[-1][2],
            "from_status": "experiment_candidate",
            "to_status": "accepted",
            "occurred_at": "2026-08-20T11:00:00Z",
            "owner": "forged-operator",
            "reason": "attempted governance bypass",
            "evidence": [self.evidence_sha()],
            "revisit_trigger": None,
            "verdict_class": "accepted",
        }
        content = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        digest = hashlib.sha256(content).hexdigest()
        planned = PlannedWrite(
            "lifecycle",
            f"lifecycle/2601.00001/v2/0007-{digest}.json",
            content,
            "create",
        )
        before = list((self.root / "lifecycle" / "2601.00001" / "v2").glob("*.json"))
        with self.assertRaisesRegex(RadarValidationError, "governed experiment result"):
            apply_transition(self.root, planned, expected_from="experiment_candidate")
        self.assertEqual(
            list((self.root / "lifecycle" / "2601.00001" / "v2").glob("*.json")),
            before,
        )
        self.assertEqual(
            load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))[-1][1]["to_status"],
            "experiment_candidate",
        )

    def test_crafted_primary_verification_plan_has_no_authority(self) -> None:
        self.apply_fixture()
        records = load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))
        event = {
            "schema": LIFECYCLE_SCHEMA,
            "paper_version_key": "arxiv:2601.00001:v2",
            "sequence": 2,
            "previous_record_sha256": records[-1][2],
            "from_status": "new",
            "to_status": "metadata_verified",
            "occurred_at": "2026-08-20T12:00:00Z",
            "owner": "research-radar-arxiv-live-v1",
            "reason": "forged primary verification",
            "evidence": [self.evidence_sha()],
            "revisit_trigger": None,
            "verdict_class": None,
        }
        content = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        digest = hashlib.sha256(content).hexdigest()
        planned = PlannedWrite(
            "lifecycle",
            f"lifecycle/2601.00001/v2/0002-{digest}.json",
            content,
            "create",
        )
        directory = self.root / "lifecycle" / "2601.00001" / "v2"
        before = list(directory.glob("*.json"))
        with self.assertRaisesRegex(RadarValidationError, "lacks live exact-ID authority"):
            apply_transition(self.root, planned, expected_from="new")
        self.assertEqual(list(directory.glob("*.json")), before)
        self.assertEqual(
            load_lifecycle(self.root, parse_arxiv_id("2601.00001v2"))[-1][1]["to_status"],
            "new",
        )

    def test_sync_config_and_discovery_window_are_strict_and_deterministic(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        production = load_sync_config(WINDOWS / "research-radar.config.json")
        self.assertEqual(production.max_discovery_items, 30)
        self.assertEqual(production.max_recheck_items, 10)
        self.assertEqual(production.max_items, 40)
        start = radar_module._parse_as_of("2026-08-18T10:00:00Z")
        end = radar_module._parse_as_of("2026-08-20T10:00:00Z")
        first = discovery_locator(config, window_start=start, window_end=end)
        second = discovery_locator(config, window_start=start, window_end=end)
        self.assertEqual(first, second)
        self.assertIn("submittedDate%3A%5B20260818100000+TO+20260820095959%5D", first)
        self.assertIn("cat%3Acs.AI", first)
        self.assertEqual(config.max_requests, 12)
        self.assertEqual(config.max_items, 8)
        value = json.loads(SYNC_CONFIG.read_text(encoding="utf-8"))
        value["budgets"]["retry_count"] = 1
        bad = Path(self.temporary.name) / "bad-config.json"
        bad.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RadarValidationError, "exactly zero"):
            load_sync_config(bad)
        value["budgets"]["retry_count"] = False
        bad.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RadarValidationError, "exactly zero"):
            load_sync_config(bad)
        with self.assertRaisesRegex(RadarValidationError, "outside its bound"):
            replace(config, max_requests=10_000)
        with self.assertRaisesRegex(RadarValidationError, "discovery terms"):
            replace(config, discovery_terms=("OR",))
        duplicate = Path(self.temporary.name) / "duplicate-config.json"
        duplicate.write_text(
            SYNC_CONFIG.read_text(encoding="utf-8").replace(
                '"schema": "coding-intelligence.research-radar.sync-config/v1",',
                '"schema": "coding-intelligence.research-radar.sync-config/v1",'
                '"schema": "coding-intelligence.research-radar.sync-config/v1",',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RadarValidationError, "duplicate JSON key"):
            load_sync_config(duplicate)
        code, report, error = self.run_cli("radar-config-validate", "--config", SYNC_CONFIG)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["mode"], "read_only_no_network_no_writes")
        code, report, error = self.run_cli("radar-config-validate", "--config", bad)
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("exactly zero", error)

    def test_query_terms_are_applicability_scoped_and_injection_is_rejected(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        locator = discovery_locator(
            config,
            window_start=radar_module._parse_as_of("2026-08-18T10:00:00Z"),
            window_end=radar_module._parse_as_of("2026-08-20T10:00:00Z"),
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(locator).query)
        search = query["search_query"][0]
        self.assertIn('all:"agent memory"', search)
        self.assertIn('all:"coding agent"', search)
        self.assertIn("cat:cs.AI", search)
        malicious = json.loads(SYNC_CONFIG.read_text(encoding="utf-8"))
        malicious["discovery_terms"] = ['agent") OR cat:quant-ph']
        path = Path(self.temporary.name) / "injected.json"
        path.write_text(json.dumps(malicious), encoding="utf-8")
        with self.assertRaisesRegex(RadarValidationError, "unsafe arXiv query"):
            load_sync_config(path)

    def test_continuous_sync_dry_run_is_read_only_and_redacts_untrusted_text(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(report["mode"], "dry_run_no_writes")
        self.assertEqual(report["discovered_items"], 1)
        self.assertEqual(report["exact_versions"], 1)
        self.assertEqual(report["artifact_verification_selected"], 2)
        self.assertFalse(self.root.exists())
        self.assertEqual(len(calls), 5)
        serialized = json.dumps(report)
        self.assertNotIn("pip install malware", serialized)
        self.assertNotIn("RadarCode", serialized)

    def test_continuous_apply_persists_cache_artifacts_triage_and_cadence(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(len(calls), 5)
        cache_manifests = list((self.root / "cache" / "http").rglob("*.json"))
        self.assertEqual(len(cache_manifests), 5)
        artifacts = [
            json.loads(path.read_text()) for path in (self.root / "artifacts").rglob("*.json")
        ]
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            {item["verification_status"] for item in artifacts},
            {"verified_primary_metadata"},
        )
        self.assertEqual({item["revision_kind"] for item in artifacts}, {"commit", "hub_revision"})
        self.assertTrue(all(item["provenance"]["code_executed"] is False for item in artifacts))
        triage = json.loads(next((self.root / "triage").rglob("*.json")).read_text())
        self.assertEqual(triage["suggestion"], "experiment")
        self.assertFalse(triage["authority"]["automatic_adoption"])
        self.assertFalse(triage["authority"]["automatic_rejection"])
        self.assertEqual(
            triage["evidence_boundary"]["full_text_claim_extraction"],
            "deferred_not_performed",
        )
        lifecycle = load_lifecycle(self.root, parse_arxiv_id("2608.00001v1"))
        self.assertEqual([item[1]["to_status"] for item in lifecycle], ["new", "metadata_verified"])

        second_calls: list[str] = []
        second = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T12:00:00Z",
            apply=True,
            opener=self.sync_opener(second_calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(second["status"], "current")
        self.assertEqual(second["currentness"]["status"], "current")
        self.assertTrue(second["currentness"]["caught_up"])
        self.assertEqual(second_calls, [])
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        code, cli_report, error = self.run_cli(
            "radar-sync",
            "--root",
            self.root,
            "--config",
            SYNC_CONFIG,
            "--as-of",
            "2026-08-20T12:30:00Z",
            "--offline",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(cli_report["mode"], "dry_run_no_writes")
        self.assertEqual(cli_report["status"], "current")
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_network_failures_create_truthful_unverified_artifacts_not_rejection(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls, fail_artifacts=True),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(report["status"], "applied")
        artifacts = [
            json.loads(path.read_text()) for path in (self.root / "artifacts").rglob("*.json")
        ]
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            {item["verification_status"] for item in artifacts},
            {"unverified_network_failure"},
        )
        triage = json.loads(next((self.root / "triage").rglob("*.json")).read_text())
        self.assertFalse(triage["authority"]["automatic_rejection"])
        lifecycle = load_lifecycle(self.root, parse_arxiv_id("2608.00001v1"))
        self.assertNotIn("rejected", [item[1]["to_status"] for item in lifecycle])

    def test_partial_github_verification_retains_proven_repo_and_license(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls, fail_commit=True),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        artifacts = [
            json.loads(path.read_text()) for path in (self.root / "artifacts").rglob("*.json")
        ]
        github = next(item for item in artifacts if item["provider"] == "github")
        self.assertEqual(github["verification_status"], "unverified_network_failure")
        self.assertEqual(github["canonical_id"], "github:ExampleOrg/RadarCode")
        self.assertEqual(github["license"], {"status": "verified", "value": "Apache-2.0"})
        self.assertIsNone(github["revision"])
        self.assertEqual(len(github["primary_responses"]), 1)

    def test_primary_artifact_identity_mismatch_remains_unverified(self) -> None:
        config = replace(load_sync_config(SYNC_CONFIG), max_artifacts=1)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls, mismatch_github=True),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        record = json.loads(next((self.root / "artifacts").rglob("*.json")).read_text())
        self.assertEqual(record["verification_status"], "unverified_identity_mismatch")
        self.assertIsNone(record["canonical_id"])

    def test_primary_artifact_json_rejects_duplicate_keys(self) -> None:
        content = b'{"id":"Example/Good","id":"Example/Bad","sha":"' + b"a" * 40 + b'"}'
        fetched = radar_module.CachedFetch(
            locator="https://huggingface.co/api/models/Example/Good",
            content=content,
            response_sha256=radar_module._sha256(content),
            source="network",
            final_locator="https://huggingface.co/api/models/Example/Good",
            content_type="application/json",
        )
        with self.assertRaises(RadarNetworkError) as raised:
            radar_module._json_primary(fetched, label="Hugging Face artifact")
        self.assertEqual(raised.exception.classification, "unverified_malformed_primary")

    def test_artifact_pending_queue_advances_to_deferred_candidate_next_run(self) -> None:
        config = replace(load_sync_config(SYNC_CONFIG), max_artifacts=1)
        first_calls: list[str] = []
        first = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(first_calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(first["artifact_verification_selected"], 1)
        self.assertEqual(first["artifact_verification_deferred"], 1)
        state = json.loads((self.root / "state" / "sync-state.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        self.assertEqual(len(state["artifact_pending"]), 1)
        self.assertIn("huggingface", state["artifact_pending"][0]["artifact_id"])

        second_calls: list[str] = []
        second = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-21T10:00:00Z",
            apply=True,
            opener=self.sync_opener(second_calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(second["artifact_verification_selected"], 1)
        self.assertEqual(second["artifact_verification_deferred"], 0)
        state = json.loads((self.root / "state" / "sync-state.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        self.assertEqual(state["artifact_pending"], [])
        artifacts = [
            json.loads(path.read_text()) for path in (self.root / "artifacts").rglob("*.json")
        ]
        self.assertEqual({item["provider"] for item in artifacts}, {"github", "huggingface"})

    def test_transient_artifact_failure_is_retried_next_run_without_same_run_retry(self) -> None:
        config = replace(load_sync_config(SYNC_CONFIG), max_artifacts=1)
        first_calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(first_calls, fail_artifacts=True),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(len([item for item in first_calls if "api.github.com" in item]), 1)
        state = json.loads((self.root / "state" / "sync-state.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        github_pending = next(
            item for item in state["artifact_pending"] if "github" in item["artifact_id"]
        )
        self.assertEqual(github_pending["attempts"], 1)
        self.assertEqual(github_pending["next_attempt_date"], "2026-08-21")

        second_calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-21T10:00:00Z",
            apply=True,
            opener=self.sync_opener(second_calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        github_records = [
            json.loads(path.read_text())
            for path in (self.root / "artifacts" / "github").rglob("*.json")
        ]
        self.assertIn(
            "verified_primary_metadata",
            {item["verification_status"] for item in github_records},
        )

    def test_artifact_attempt_100_moves_to_manual_review_without_invalid_state(self) -> None:
        base = replace(load_sync_config(SYNC_CONFIG), max_artifacts=0)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            base,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        state_path = self.root / "state" / "sync-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))["payload"]
        github = next(item for item in state["artifact_pending"] if "github" in item["artifact_id"])
        github["attempts"] = 100
        github["next_attempt_date"] = "2026-08-21"
        state_path.write_bytes(
            radar_module._canonical_bytes(radar_module._sync_state_with_integrity(state))
        )

        result = sync_research_radar(
            self.root,
            replace(base, max_artifacts=1),
            as_of="2026-08-21T10:00:00Z",
            apply=True,
            opener=self.sync_opener([], fail_artifacts=True),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(len(result["artifact_manual_review_required"]), 1)
        loaded = radar_module._load_sync_state(self.root)
        self.assertFalse(
            any("github" in item["artifact_id"] for item in loaded["artifact_pending"])
        )
        github_record = max(
            (
                json.loads(path.read_text())
                for path in (self.root / "artifacts" / "github").rglob("*.json")
            ),
            key=lambda item: item["observed_at"],
        )
        self.assertEqual(
            github_record["retry_disposition"],
            "manual_review_required_max_attempts",
        )
        later_calls: list[str] = []
        sync_research_radar(
            self.root,
            replace(base, max_artifacts=1),
            as_of="2026-08-22T10:00:00Z",
            apply=True,
            opener=self.sync_opener(later_calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertFalse(
            any("api.github.com/repos/ExampleOrg/RadarCode" in item for item in later_calls)
        )

    def test_public_apply_and_offline_flags_require_exact_booleans_before_writes(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        invalid = ("false", "true", 0, 1, None)
        for index, value in enumerate(invalid):
            for field in ("apply", "offline"):
                with self.subTest(field=field, value=value):
                    root = Path(self.temporary.name) / f"exact-bool-{field}-{index}"
                    kwargs = {"apply": False, "offline": False}
                    kwargs[field] = value
                    with self.assertRaisesRegex(RadarValidationError, "exact booleans"):
                        sync_research_radar(
                            root,
                            config,
                            as_of="2026-08-20T10:00:00Z",
                            **kwargs,  # type: ignore[arg-type]
                        )
                    self.assertFalse(root.exists())
            with self.assertRaisesRegex(RadarValidationError, "exact booleans"):
                radar_module.BoundedNetworkClient(
                    root=Path(self.temporary.name) / f"client-{index}",
                    cache_day=radar_module.date(2026, 8, 20),
                    as_of="2026-08-20T10:00:00Z",
                    config=config,
                    write_cache=value,  # type: ignore[arg-type]
                    offline=False,
                )

    def test_verified_artifact_evidence_survives_weekly_triage_recheck(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        first = max(
            (json.loads(path.read_text()) for path in (self.root / "triage").rglob("*.json")),
            key=lambda item: item["as_of"],
        )
        later_calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-28T10:00:00Z",
            apply=True,
            opener=self.sync_opener(later_calls, discovery=EMPTY_FEED.read_bytes()),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        later = max(
            (json.loads(path.read_text()) for path in (self.root / "triage").rglob("*.json")),
            key=lambda item: item["as_of"],
        )
        self.assertEqual(later["scores"]["evidence"], first["scores"]["evidence"])
        self.assertEqual(later["scores"]["feasibility"], first["scores"]["feasibility"])
        self.assertEqual(later["features"]["verified_artifacts"], 2)

    def test_crash_before_state_commit_recovers_from_daily_cache_idempotently(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        real_writer = radar_module._write_sync_state
        with patch(
            "agent_continuity.research_radar._write_sync_state",
            side_effect=RuntimeError("simulated process death before state commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated process death"):
                sync_research_radar(
                    self.root,
                    config,
                    as_of="2026-08-20T10:00:00Z",
                    apply=True,
                    opener=self.sync_opener(calls),  # type: ignore[arg-type]
                    sleeper=lambda _: None,
                )
        self.assertFalse((self.root / "state" / "sync-state.json").exists())
        initial_files = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and path.name != "research-radar-sync.lock"
        }
        retry_calls: list[str] = []
        with patch("agent_continuity.research_radar._write_sync_state", real_writer):
            report = sync_research_radar(
                self.root,
                config,
                as_of="2026-08-20T10:00:00Z",
                apply=True,
                opener=self.sync_opener(retry_calls),  # type: ignore[arg-type]
                sleeper=lambda _: None,
            )
        self.assertEqual(report["network"]["requests"], 0)
        self.assertEqual(report["network"]["cache_hits"], 2)
        self.assertEqual(retry_calls, [])
        for relative, content in initial_files.items():
            self.assertEqual((self.root / relative).read_bytes(), content)
        self.assertTrue((self.root / "state" / "sync-state.json").exists())

    def test_cache_manifest_filename_day_and_final_locator_are_integrity_bound(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        manifest_path = next((self.root / "cache" / "http").rglob("*.json"))
        original_content = manifest_path.read_bytes()
        original = json.loads(original_content)
        directory = manifest_path.parent

        wrong_name = directory / ("0" * 64 + ".json")
        manifest_path.rename(wrong_name)
        with self.assertRaisesRegex(RadarValidationError, "manifest hash"):
            radar_module._load_cached_fetch(
                self.root,
                radar_module.date(2026, 8, 20),
                original["locator"],
                expected_provider=original["provider"],
            )
        wrong_name.rename(manifest_path)

        def write_mutation(value: dict[str, object]) -> Path:
            for existing in directory.glob("*.json"):
                existing.unlink()
            content = radar_module._canonical_bytes(value)
            path = directory / (hashlib.sha256(content).hexdigest() + ".json")
            path.write_bytes(content)
            return path

        wrong_day = {**original, "fetched_at": "2026-08-21T10:00:00Z"}
        write_mutation(wrong_day)
        with self.assertRaisesRegex(RadarValidationError, "day does not match"):
            radar_module._load_cached_fetch(
                self.root,
                radar_module.date(2026, 8, 20),
                original["locator"],
                expected_provider=original["provider"],
            )

        wrong_final = {
            **original,
            "final_locator": "https://api.github.com/repos/Other/Target",
        }
        write_mutation(wrong_final)
        with self.assertRaisesRegex(RadarValidationError, "identity mismatch"):
            radar_module._load_cached_fetch(
                self.root,
                radar_module.date(2026, 8, 20),
                original["locator"],
                expected_provider=original["provider"],
            )

    def test_concurrent_sync_has_one_network_owner_and_one_current_observer(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        opener = self.sync_opener(calls)

        def run(_: int) -> str:
            result = sync_research_radar(
                self.root,
                config,
                as_of="2026-08-20T10:00:00Z",
                apply=True,
                opener=opener,  # type: ignore[arg-type]
                sleeper=lambda _: None,
            )
            return str(result["status"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(run, (1, 2)))
        self.assertEqual(statuses, ["applied", "current"])
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(list((self.root / "sync" / "runs").glob("*.json"))), 1)

    def test_weekly_recheck_is_exact_id_bounded_and_progresses_without_discovery_retry(
        self,
    ) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []
        sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=self.sync_opener(calls),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        later_calls: list[str] = []
        later = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-28T10:00:00Z",
            apply=True,
            opener=self.sync_opener(later_calls, discovery=EMPTY_FEED.read_bytes()),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertTrue(later["weekly_version_recheck_due"])
        self.assertEqual(later["rechecked_items"], 1)
        exact_calls = [item for item in later_calls if "id_list=" in item]
        self.assertEqual(len(exact_calls), 1)
        self.assertIn("2608.00001", exact_calls[0])

    def test_discovery_pagination_cursor_prevents_silent_window_truncation(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []

        def paged_opener(request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 5.0)
            locator = request.full_url  # type: ignore[attr-defined]
            calls.append(locator)
            if "id_list=" in locator and "2608.00011" in locator and "2608.00012" in locator:
                content = self.combined_paged_feed()
            elif "start=1" in locator or "id_list=2608.00012" in locator:
                content = PAGED_FEED_1.read_bytes()
            else:
                content = PAGED_FEED_0.read_bytes()
            return FakeResponse(content, url=locator)

        first = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=paged_opener,
            sleeper=lambda _: None,
        )
        self.assertFalse(first["discovery_page_complete"])
        self.assertEqual(first["discovery_next_start"], 1)
        state_path = self.root / "state" / "sync-state.json"
        state = json.loads(state_path.read_text())["payload"]
        self.assertEqual(state["discovery_next_start"], 1)
        self.assertIsNone(state["last_daily_discovery_date"])

        second = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=paged_opener,
            sleeper=lambda _: None,
        )
        self.assertTrue(second["discovery_page_complete"])
        self.assertEqual(second["discovery_next_start"], 2)
        state = json.loads(state_path.read_text())["payload"]
        self.assertIsNone(state["discovery_next_start"])
        self.assertEqual(state["last_daily_discovery_date"], "2026-08-20")
        self.assertTrue((self.root / "papers" / "2608.00011" / "versions" / "v1.json").exists())
        self.assertTrue((self.root / "papers" / "2608.00012" / "versions" / "v1.json").exists())
        self.assertEqual(len(calls), 4)

    def test_config_change_reseeds_historical_cursor_at_frontier(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        calls: list[str] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            locator = request.full_url  # type: ignore[attr-defined]
            calls.append(locator)
            return FakeResponse(PAGED_FEED_0.read_bytes(), url=locator)

        first = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=opener,
            sleeper=lambda _: None,
        )
        self.assertFalse(first["discovery_page_complete"])
        changed = replace(
            config,
            discovery_terms=tuple(sorted((*config.discovery_terms, "long context"))),
        )
        calls.clear()
        second = sync_research_radar(
            self.root,
            changed,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=opener,
            sleeper=lambda _: None,
        )
        self.assertTrue(second["discovery_cycle_config_reset"])
        self.assertFalse(any("start=1" in locator for locator in calls))
        state = json.loads((self.root / "state" / "sync-state.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        self.assertEqual(state["discovery_cycle_config_sha256"], changed.sha256)

    def test_high_relevant_arrival_rate_is_runtime_blocked_not_called_current(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        overloaded = (
            CONTINUOUS_FEED.read_text(encoding="utf-8")
            .replace(
                "<opensearch:totalResults>1</opensearch:totalResults>",
                "<opensearch:totalResults>100</opensearch:totalResults>",
            )
            .encode()
        )
        calls: list[str] = []
        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            opener=self.sync_opener(calls, discovery=overloaded),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        currentness = report["currentness"]
        self.assertEqual(currentness["status"], "runtime_blocked_capacity")
        self.assertFalse(currentness["caught_up"])
        self.assertGreater(currentness["backlog_count"], 0)
        self.assertGreater(
            currentness["arrival_rate_per_day"],
            currentness["discovery_capacity_per_day"],
        )
        self.assertEqual(report["status"], "runtime_blocked")
        with patch(
            "agent_continuity.cli.sync_research_radar",
            return_value=report,
        ):
            code, cli_report, error = self.run_cli(
                "radar-sync",
                "--root",
                self.root,
                "--config",
                SYNC_CONFIG,
            )
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(cli_report["currentness"]["status"], "runtime_blocked_capacity")

    def test_arrival_between_frontier_and_total_capacity_is_sustainable(self) -> None:
        base = load_sync_config(SYNC_CONFIG)
        config = replace(
            base,
            max_items=24,
            max_discovery_items=20,
            max_frontier_items=12,
            max_backlog_items=8,
            max_recheck_items=4,
        )
        fifteen_per_day = (
            CONTINUOUS_FEED.read_text(encoding="utf-8")
            .replace(
                "<opensearch:totalResults>1</opensearch:totalResults>",
                "<opensearch:totalResults>30</opensearch:totalResults>",
            )
            .encode()
        )
        calls: list[str] = []
        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            opener=self.sync_opener(calls, discovery=fifteen_per_day),  # type: ignore[arg-type]
            sleeper=lambda _: None,
        )
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["currentness"]["status"], "frontier_current_backfill_active")
        self.assertEqual(report["currentness"]["arrival_rate_per_day"], 15.0)
        self.assertEqual(report["currentness"]["discovery_capacity_per_day"], 20)

    def test_net_backfill_capacity_drives_catchup_math_and_equality_blocks(self) -> None:
        base = load_sync_config(SYNC_CONFIG)
        config = replace(
            base,
            max_items=34,
            max_discovery_items=30,
            max_frontier_items=20,
            max_backlog_items=10,
            max_recheck_items=4,
            discovery_lookback_hours=24,
        )
        for total, expected_status, expected_runs in (
            (29, "frontier_current_backfill_active", 28),
            (30, "runtime_blocked_capacity", 0),
        ):
            with self.subTest(total=total):
                content = (
                    CONTINUOUS_FEED.read_text(encoding="utf-8")
                    .replace(
                        "<opensearch:totalResults>1</opensearch:totalResults>",
                        f"<opensearch:totalResults>{total}</opensearch:totalResults>",
                    )
                    .encode()
                )
                root = Path(self.temporary.name) / f"capacity-{total}"
                report = sync_research_radar(
                    root,
                    config,
                    as_of="2026-08-20T10:00:00Z",
                    opener=self.sync_opener([], discovery=content),  # type: ignore[arg-type]
                    sleeper=lambda _: None,
                )
                self.assertEqual(report["currentness"]["status"], expected_status)
                self.assertEqual(report["currentness"]["estimated_catch_up_runs"], expected_runs)

    def test_frontier_and_contiguous_history_union_prevents_multiday_gap(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        payload = {
            **radar_module._default_sync_state(),
            "last_daily_discovery_date": "2026-08-20",
            "last_discovery_window_end": "2026-08-20T10:00:00Z",
            "last_discovery_config_sha256": config.sha256,
            "last_version_recheck_completed_at": "2026-08-20T10:00:00Z",
        }
        state_path = self.root / "state" / "sync-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_bytes(
            radar_module._canonical_bytes(radar_module._sync_state_with_integrity(payload))
        )
        frontier_feed = (
            PAGED_FEED_1.read_text(encoding="utf-8")
            .replace(
                "<opensearch:totalResults>2</opensearch:totalResults>",
                "<opensearch:totalResults>1</opensearch:totalResults>",
            )
            .replace(
                "<opensearch:startIndex>1</opensearch:startIndex>",
                "<opensearch:startIndex>0</opensearch:startIndex>",
            )
            .encode()
        )
        calls: list[str] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            locator = request.full_url  # type: ignore[attr-defined]
            calls.append(locator)
            if "id_list=" in locator:
                content = self.combined_paged_feed()
            else:
                search = urllib.parse.parse_qs(urllib.parse.urlsplit(locator).query)[
                    "search_query"
                ][0]
                content = frontier_feed if "20260823" in search else PAGED_FEED_0.read_bytes()
            return FakeResponse(content, url=locator)

        report = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-25T10:00:00Z",
            apply=True,
            opener=opener,
            sleeper=lambda _: None,
        )
        self.assertEqual(report["discovered_items"], 2)
        self.assertTrue((self.root / "papers" / "2608.00011" / "versions" / "v1.json").exists())
        self.assertTrue((self.root / "papers" / "2608.00012" / "versions" / "v1.json").exists())
        history_queries = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(item).query)["search_query"][0]
            for item in calls
            if "search_query=" in item
        ]
        self.assertTrue(any("20260820095959" in query for query in history_queries))

    def test_rehashed_but_semantically_corrupt_sync_cursors_are_rejected(self) -> None:
        base = radar_module._default_sync_state()
        cases = {
            "invalid recheck": {
                "recheck_cycle_started_at": "2026-08-20T10:00:00Z",
                "recheck_after_base_id": "zzzz",
            },
            "unpaired recheck": {
                "recheck_cycle_started_at": "2026-08-20T10:00:00Z",
            },
            "missing canonical recheck": {
                "recheck_cycle_started_at": "2026-08-20T10:00:00Z",
                "recheck_after_base_id": "9999.99999",
            },
            "cursor exceeds": {
                "discovery_cycle_start": "2026-08-18T10:00:00Z",
                "discovery_cycle_end": "2026-08-20T10:00:00Z",
                "discovery_next_start": 11,
                "discovery_cycle_total_results": 10,
                "discovery_cycle_config_sha256": "sha256:" + "1" * 64,
            },
            "bad run": {"last_run_id": "radar-not-a-hash"},
            "bad date": {"last_daily_discovery_date": "2026-99-99"},
        }
        state_path = self.root / "state" / "sync-state.json"
        state_path.parent.mkdir(parents=True)
        for label, mutation in cases.items():
            with self.subTest(label=label):
                payload = {**base, **mutation}
                state_path.write_bytes(
                    radar_module._canonical_bytes(radar_module._sync_state_with_integrity(payload))
                )
                with self.assertRaises(RadarValidationError):
                    radar_module._load_sync_state(self.root)

    def test_future_cadence_timestamp_is_clock_regression_not_silent_skip(self) -> None:
        payload = {
            **radar_module._default_sync_state(),
            "last_version_recheck_completed_at": "2026-08-21T10:00:00Z",
        }
        state_path = self.root / "state" / "sync-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_bytes(
            radar_module._canonical_bytes(radar_module._sync_state_with_integrity(payload))
        )
        with self.assertRaisesRegex(RadarValidationError, "clock regression"):
            sync_research_radar(
                self.root,
                load_sync_config(SYNC_CONFIG),
                as_of="2026-08-20T10:00:00Z",
                offline=True,
            )

    def test_changed_arxiv_result_total_restarts_fixed_window_instead_of_skipping(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        first_calls: list[str] = []

        def first_opener(request: object, *, timeout: float) -> FakeResponse:
            locator = request.full_url  # type: ignore[attr-defined]
            first_calls.append(locator)
            return FakeResponse(PAGED_FEED_0.read_bytes(), url=locator)

        first = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=first_opener,
            sleeper=lambda _: None,
        )
        self.assertEqual(first["discovery_next_start"], 1)

        changed_page = (
            PAGED_FEED_1.read_text(encoding="utf-8")
            .replace(
                "<opensearch:totalResults>2</opensearch:totalResults>",
                "<opensearch:totalResults>3</opensearch:totalResults>",
            )
            .encode()
        )

        def changed_opener(request: object, *, timeout: float) -> FakeResponse:
            locator = request.full_url  # type: ignore[attr-defined]
            if "id_list=" in locator and "2608.00011" in locator and "2608.00012" in locator:
                content = self.combined_paged_feed(total_results=3)
            elif "start=1" in locator or "id_list=2608.00012" in locator:
                content = changed_page
            else:
                content = PAGED_FEED_0.read_bytes()
            return FakeResponse(content, url=locator)

        second = sync_research_radar(
            self.root,
            config,
            as_of="2026-08-20T10:00:00Z",
            apply=True,
            opener=changed_opener,
            sleeper=lambda _: None,
        )
        self.assertTrue(second["discovery_pagination_restarted"])
        self.assertFalse(second["discovery_page_complete"])
        self.assertEqual(second["discovery_next_start"], 0)
        state = json.loads((self.root / "state" / "sync-state.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        self.assertEqual(state["discovery_next_start"], 0)
        self.assertEqual(state["discovery_cycle_total_results"], 3)

    def test_triage_evaluator_never_infers_rejection_or_adoption(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        parsed = parse_arxiv_atom(read_atom_file(CONTINUOUS_FEED), max_entries=1)
        version = parsed["versions"][0]
        candidates = extract_artifact_candidates(version)
        self.assertEqual(
            [candidate.normalized_id for candidate in candidates],
            [
                "github:repository:ExampleOrg/RadarCode",
                "huggingface:model:ExampleOrg/RadarModel",
            ],
        )
        record = triage_version(
            version,
            profile=config.triage,
            as_of="2026-08-20T10:00:00Z",
            primary_metadata_verified=False,
            artifact_records=[],
        )
        self.assertIn(record["suggestion"], {"experiment", "watch", "manual_review"})
        self.assertNotIn(record["suggestion"], {"reject", "adopt", "implement_now"})
        self.assertFalse(record["authority"]["automatic_rejection"])
        self.assertFalse(record["authority"]["automatic_adoption"])
        self.assertEqual(record["features"]["primary_metadata_verified"], False)

    def test_offline_cache_miss_is_runtime_blocked_and_read_only(self) -> None:
        config = load_sync_config(SYNC_CONFIG)
        with self.assertRaises(RadarNetworkError) as raised:
            sync_research_radar(
                self.root,
                config,
                as_of="2026-08-20T10:00:00Z",
                offline=True,
            )
        self.assertEqual(raised.exception.classification, "runtime_blocked_cache_miss")
        self.assertFalse(self.root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Scheduled Task contract")
    def test_windows_scheduler_sources_parse_and_installer_dry_run_writes_nothing(self) -> None:
        installer = WINDOWS / "Install-ResearchRadar.ps1"
        runner = WINDOWS / "Run-ResearchRadar.ps1"
        runtime_entry = WINDOWS / "research_radar_runtime_entry.py"
        owned_base = Path(os.environ["LOCALAPPDATA"]) / "CodingIntelligence"
        suffix = Path(self.temporary.name).name
        runtime = owned_base / f"RadarDryRuntime-{suffix}"
        radar_root = owned_base / f"RadarDryState-{suffix}"
        self.assertFalse(runtime.exists())
        self.assertFalse(radar_root.exists())
        parser_paths = ",".join(
            "'" + str(path).replace("'", "''") + "'" for path in (installer, runner)
        )
        parser_script = (
            f"$failed=$false; foreach($p in @({parser_paths})){{"
            "$t=$null;$e=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e);"
            "if($e.Count -ne 0){$failed=$true;$e|ForEach-Object{Write-Error $_.Message}}};"
            "if($failed){exit 2}"
        )
        parsed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                parser_script,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        entry_environment = dict(os.environ)
        entry_environment["PYTHONPATH"] = str(PACKAGE / "src")
        entry_check = subprocess.run(
            [
                sys.executable,
                str(runtime_entry),
                "--validate-config",
                "--config",
                str(SYNC_CONFIG),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=entry_environment,
        )
        self.assertEqual(entry_check.returncode, 0, entry_check.stderr)
        self.assertEqual(json.loads(entry_check.stdout)["status"], "valid")
        planned = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(installer),
                "-ConfigPath",
                str(SYNC_CONFIG),
                "-RadarRoot",
                str(radar_root),
                "-RuntimeRoot",
                str(runtime),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        report = json.loads(planned.stdout)
        self.assertEqual(report["mode"], "dry_run_no_writes")
        self.assertEqual(report["run_level"], "Limited")
        self.assertEqual(report["logon_type"], "Interactive")
        self.assertEqual(report["multiple_instances"], "IgnoreNew")
        self.assertEqual(report["deployed_module_count"], 2)
        self.assertTrue(report["source_module_manifest_sha256"].startswith("sha256:"))
        self.assertFalse(runtime.exists())
        self.assertFalse(radar_root.exists())


if __name__ == "__main__":
    unittest.main()
