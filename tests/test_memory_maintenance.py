from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.memory_maintenance import (  # noqa: E402
    MAX_BYTES,
    MAX_HOT_BLOB_BUDGET_BYTES,
    MAX_ITEMS,
    MAX_LOCK_TIMEOUT_SECONDS,
    MAX_WALL_SECONDS,
    DiskPressure,
    MaintenanceBusy,
    MaintenanceLimits,
    load_maintenance_evidence,
    run_maintenance,
)
from agent_continuity.memory_store import (  # noqa: E402
    ChainValidationError,
    MemoryScope,
    MemoryStore,
    MemoryStoreError,
    ProjectionError,
)

HOST = "maintenance-test"
OWNER = "operator-a"
ACTOR = {"kind": "system", "id": "maintenance-test"}
CREATED = "2026-01-01T00:00:00Z"
TERMINAL = "2026-01-02T00:00:00Z"
AFTER_TTL = "2026-01-03T00:00:01Z"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def pressure(basis_points: int):
    def probe(_: Path) -> DiskPressure:
        return DiskPressure(
            total_bytes=10_000,
            used_bytes=basis_points,
            free_bytes=10_000 - basis_points,
            basis_points=basis_points,
            source="injected-test",
        )

    return probe


class MemoryMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root, blob_threshold=256)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def append_scratch(
        self, marker: str, *, workspace: str = "workspace-a"
    ) -> tuple[dict[str, object], bytes, MemoryScope, str, str]:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        scope = MemoryScope(OWNER, workspace, task_id, None, None)
        payload = {"marker": marker, "content": marker * 2_048}
        content = canonical_bytes(payload)
        source = self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="tool/result",
            actor=ACTOR,
            scope=scope,
            payload=payload,
            occurred_at=CREATED,
            retention_class="scratch",
        )
        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="task/completed",
            actor=ACTOR,
            scope=scope,
            payload={"status": "tested"},
            occurred_at=TERMINAL,
        )
        digest = str(source["payload"]["$blob"]["sha256"])
        return source, content, scope, task_id, digest

    def evicted(self, marker: str = "rehydrate"):
        source, content, scope, task_id, digest = self.append_scratch(marker)
        self.store.run_gc(self.store.plan_retention(now=AFTER_TTL), apply=True)
        receipt = self.store._read_tombstone(digest)
        return source, content, scope, task_id, digest, receipt

    def provenance(self, digest: str) -> dict[str, str]:
        return {
            "source_kind": "local_backup",
            "source_locator": "test://offline-exact-copy",
            "source_sha256": digest,
            "authority": "maintenance-test",
        }

    def create_directory_link(self, link: Path, target: Path) -> bool:
        if os.name != "nt":
            os.symlink(target, link, target_is_directory=True)
            return True
        created = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:CI_TEST_LINK "
                "-Target $env:CI_TEST_TARGET | Out-Null",
            ],
            env={
                **os.environ,
                "CI_TEST_LINK": str(link),
                "CI_TEST_TARGET": str(target),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return created.returncode == 0

    def catalog(self, digest: str) -> dict[str, object]:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_below_eighty_percent_is_dry_run_even_when_apply_requested(self) -> None:
        source, _, _, _, digest = self.append_scratch("below-pressure")
        before = self.store.verify().event_count
        report = run_maintenance(
            self.root,
            apply=True,
            now=AFTER_TTL,
            pressure_probe=pressure(7_999),
        )
        self.assertEqual(report["status"], "below_pressure_dry_run")
        self.assertIsNone(report["execution"])
        self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertEqual(MemoryStore(self.root).verify().event_count, before)
        self.assertEqual(report["dry_run"]["actions_planned"], 1)
        self.assertEqual(
            source["event_id"],
            self.store._read_segment(self.store._segment_path(HOST, str(source["session_id"])))[0][
                1
            ]["event_id"],
        )

    def test_malformed_mutation_authority_is_rejected_before_any_write(self) -> None:
        for index, malformed in enumerate(("false", "true", 0, 1, None, [], {})):
            root = self.root / f"malformed-apply-{index}"
            with self.subTest(value=malformed), self.assertRaisesRegex(ValueError, "exact boolean"):
                run_maintenance(root, apply=malformed)  # type: ignore[arg-type]
            self.assertFalse(root.exists())

        source, _, _, _, digest = self.append_scratch("run-gc-authority")
        plan = self.store.plan_retention(now=AFTER_TTL)
        before = self.store.verify()
        for malformed in ("false", "true", 0, 1, None):
            with (
                self.subTest(run_gc=malformed),
                self.assertRaisesRegex(MemoryStoreError, "exact boolean"),
            ):
                self.store.run_gc(plan, apply=malformed)  # type: ignore[arg-type]
            self.assertEqual(self.store.verify(), before)
            self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertEqual(source["event_id"], plan.actionable[0].source_event_ids[0])

        unopened = self.root / "invalid-constructor"
        with self.assertRaisesRegex(MemoryStoreError, "rebuild_on_open"):
            MemoryStore(unopened, rebuild_on_open="false")  # type: ignore[arg-type]
        self.assertFalse(unopened.exists())
        with self.assertRaisesRegex(MemoryStoreError, "rebuild must"):
            self.store.replay(rebuild="false")  # type: ignore[arg-type]
        new_task = str(uuid.uuid4())
        new_session = str(uuid.uuid4())
        with self.assertRaisesRegex(MemoryStoreError, "_lock_already_held"):
            self.store.append(
                host_id=HOST,
                session_id=new_session,
                task_id=new_task,
                event_type="tool/result",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-a", new_task),
                payload={"value": "must-not-append"},
                _lock_already_held="false",  # type: ignore[arg-type]
            )
        self.assertFalse(self.store._segment_path(HOST, new_session).exists())

    def test_canonical_limits_reject_every_over_limit_path_before_root_creation(self) -> None:
        invalid_limits = (
            {"max_items": MAX_ITEMS + 1},
            {"max_bytes": MAX_BYTES + 1},
            {"max_wall_seconds": MAX_WALL_SECONDS + 0.001},
        )
        for index, override in enumerate(invalid_limits):
            root = self.root / f"invalid-limit-{index}"
            with self.subTest(override=override), self.assertRaises(ValueError):
                limits = MaintenanceLimits(**override)
                run_maintenance(root, apply=False, limits=limits)
            self.assertFalse(root.exists())

        class DerivedLimits(MaintenanceLimits):
            pass

        derived_root = self.root / "derived-limits"
        with self.assertRaisesRegex(ValueError, "exact MaintenanceLimits"):
            run_maintenance(derived_root, apply=False, limits=DerivedLimits())
        self.assertFalse(derived_root.exists())
        for name, kwargs in (
            (
                "hot-budget",
                {"hot_blob_budget_bytes": MAX_HOT_BLOB_BUDGET_BYTES + 1},
            ),
            ("lock-timeout", {"lock_timeout": MAX_LOCK_TIMEOUT_SECONDS + 0.001}),
        ):
            root = self.root / name
            with self.subTest(name=name), self.assertRaises(ValueError):
                run_maintenance(root, apply=False, **kwargs)
            self.assertFalse(root.exists())

        cli_root = self.root / "cli-over-limit"
        environment = {**os.environ, "PYTHONPATH": str(PACKAGE / "src")}
        command = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_continuity.memory_maintenance",
                "--root",
                str(cli_root),
                "--max-items",
                str(MAX_ITEMS + 1),
            ],
            cwd=PACKAGE,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(command.returncode, 1)
        self.assertFalse(cli_root.exists())

    def test_filesystem_root_is_rejected_before_maintenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            run_maintenance(
                Path(self.root.anchor),
                apply=False,
                pressure_probe=pressure(0),
            )

    def test_maintenance_evidence_rejects_junction_or_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            redirected = self.root / "maintenance"
            if os.name == "nt":
                created = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "New-Item -ItemType Junction -Path "
                        "$env:CI_MAINTENANCE_TEST_REDIRECTED "
                        "-Target $env:CI_MAINTENANCE_TEST_OUTSIDE | Out-Null",
                    ],
                    env={
                        **os.environ,
                        "CI_MAINTENANCE_TEST_REDIRECTED": str(redirected),
                        "CI_MAINTENANCE_TEST_OUTSIDE": str(outside),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if created.returncode != 0:
                    self.skipTest(f"junction creation unavailable: {created.stderr}")
            else:
                os.symlink(outside, redirected, target_is_directory=True)
            try:
                with self.assertRaisesRegex(MemoryStoreError, "escapes"):
                    run_maintenance(
                        self.root,
                        apply=False,
                        pressure_probe=pressure(1_000),
                    )
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                redirected.rmdir()

    def test_store_rejects_event_database_and_lock_link_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            event_root = self.root / "event-link-store"
            event_root.mkdir()
            if not self.create_directory_link(event_root / "events", outside):
                self.skipTest("junction creation unavailable")
            try:
                with self.assertRaisesRegex(MemoryStoreError, "symlink or junction"):
                    MemoryStore(event_root)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                (event_root / "events").rmdir()

        for internal_name in ("memory.sqlite3", ".memory-v1.lock"):
            with (
                self.subTest(internal=internal_name),
                tempfile.TemporaryDirectory() as outside_name,
            ):
                outside = Path(outside_name)
                target = outside / "external.bin"
                target.write_bytes(b"")
                root = self.root / ("hardlink-" + internal_name.replace(".", "-"))
                root.mkdir()
                link = root / internal_name
                os.link(target, link)
                before = target.read_bytes()
                with self.assertRaisesRegex(MemoryStoreError, "hard-linked"):
                    MemoryStore(root)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(target.stat().st_nlink, 2)

        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            root_link = self.root / "linked-memory-root"
            if not self.create_directory_link(root_link, outside):
                self.skipTest("junction creation unavailable")
            try:
                with self.assertRaisesRegex(MemoryStoreError, "symlink or junction"):
                    MemoryStore(root_link)
                with self.assertRaisesRegex(ValueError, "symlink or junction"):
                    run_maintenance(root_link, apply=False)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                root_link.rmdir()

    def test_pressure_apply_is_scope_item_and_byte_bounded(self) -> None:
        _, _, scope_a, _, digest_a = self.append_scratch("scope-a", workspace="workspace-a")
        _, _, _, _, digest_b = self.append_scratch("scope-b", workspace="workspace-b")
        report = run_maintenance(
            self.root,
            apply=True,
            now=AFTER_TTL,
            scopes=[scope_a],
            limits=MaintenanceLimits(max_items=1, max_bytes=1_000_000, max_wall_seconds=5),
            pressure_probe=pressure(8_000),
        )
        self.assertEqual(report["status"], "pressure_applied")
        self.assertEqual(report["execution"]["actions_applied"], 1)
        self.assertFalse(self.store._blob_path(digest_a).exists())
        self.assertTrue(self.store._blob_path(digest_b).is_file())
        self.assertEqual(report["exact_scope_keys"], [scope_a.key])

        _, _, _, _, digest_c = self.append_scratch("byte-bound")
        limited = run_maintenance(
            self.root,
            apply=True,
            now=AFTER_TTL,
            limits=MaintenanceLimits(max_items=25, max_bytes=1, max_wall_seconds=5),
            pressure_probe=pressure(8_500),
        )
        self.assertEqual(limited["status"], "pressure_bounded_noop")
        self.assertEqual(limited["execution"]["actions_applied"], 0)
        self.assertEqual(limited["execution"]["stop_reason"], "max_bytes")
        self.assertTrue(self.store._blob_path(digest_c).is_file())

    def test_injected_wall_deadline_prevents_first_mutation(self) -> None:
        _, _, _, _, digest = self.append_scratch("wall-bound")
        moments = iter((0.0, 31.0, 31.0))
        report = run_maintenance(
            self.root,
            apply=True,
            now=AFTER_TTL,
            limits=MaintenanceLimits(max_items=25, max_bytes=1_000_000, max_wall_seconds=30),
            pressure_probe=pressure(9_000),
            monotonic=lambda: next(moments),
        )
        self.assertEqual(report["status"], "pressure_bounded_noop")
        self.assertEqual(report["execution"]["actions_applied"], 0)
        self.assertEqual(report["execution"]["stop_reason"], "wall_deadline")
        self.assertTrue(self.store._blob_path(digest).is_file())

    def test_every_run_writes_canonical_digest_verified_evidence(self) -> None:
        report = run_maintenance(
            self.root,
            apply=False,
            now=AFTER_TTL,
            pressure_probe=pressure(8_100),
        )
        path = Path(report["evidence_path"])
        raw = path.read_bytes()
        value = json.loads(raw)
        supplied = value.pop("report_sha256")
        expected = "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
        self.assertEqual(supplied, expected)
        self.assertEqual(raw, canonical_bytes({**value, "report_sha256": supplied}))
        self.assertEqual(value["status"], "pressure_dry_run")
        loaded = load_maintenance_evidence(path, expected_root=self.root)
        self.assertEqual(loaded["report_sha256"], supplied)
        self.assertEqual(loaded["memory_root"], str(self.root.resolve()))

    def test_evidence_loader_accepts_status_variants_and_rejects_mutations(self) -> None:
        reports = [
            run_maintenance(
                self.root,
                apply=False,
                now=AFTER_TTL,
                pressure_probe=pressure(1_000),
            ),
            run_maintenance(
                self.root,
                apply=False,
                now=AFTER_TTL,
                pressure_probe=pressure(8_100),
            ),
            run_maintenance(
                self.root,
                apply=True,
                now=AFTER_TTL,
                pressure_probe=pressure(8_100),
            ),
        ]
        self.append_scratch("evidence-status")
        reports.append(
            run_maintenance(
                self.root,
                apply=True,
                now=AFTER_TTL,
                limits=MaintenanceLimits(max_items=1, max_bytes=1, max_wall_seconds=5),
                pressure_probe=pressure(8_100),
            )
        )
        reports.append(
            run_maintenance(
                self.root,
                apply=True,
                now=AFTER_TTL,
                limits=MaintenanceLimits(max_items=1, max_bytes=1_000_000, max_wall_seconds=5),
                pressure_probe=pressure(8_100),
            )
        )
        self.assertEqual(
            [item["status"] for item in reports],
            [
                "below_pressure_dry_run",
                "pressure_dry_run",
                "pressure_no_eligible_items",
                "pressure_bounded_noop",
                "pressure_applied",
            ],
        )
        for report in reports:
            loaded = load_maintenance_evidence(report["evidence_path"], expected_root=self.root)
            self.assertEqual(loaded["status"], report["status"])

        path = Path(reports[1]["evidence_path"])
        original = path.read_bytes()
        value = json.loads(original)

        def sealed(changed: dict[str, object]) -> bytes:
            unsigned = dict(changed)
            unsigned.pop("report_sha256", None)
            changed["report_sha256"] = (
                "sha256:" + hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
            )
            return canonical_bytes(changed)

        mutations: list[tuple[str, bytes]] = []
        changed = dict(value)
        changed["unknown"] = True
        mutations.append(("fields", sealed(changed)))
        changed = dict(value)
        changed.pop("limits")
        mutations.append(("fields", sealed(changed)))
        changed = dict(value)
        changed["run_id"] = str(uuid.uuid4())
        mutations.append(("locator", sealed(changed)))
        changed = dict(value)
        changed["report_sha256"] = "sha256:" + "0" * 64
        mutations.append(("digest", canonical_bytes(changed)))
        mutations.append(("malformed", b"{not-json"))
        mutations.append(("canonical", json.dumps(value, indent=2).encode("utf-8")))
        for label, encoded in mutations:
            with self.subTest(label=label):
                path.write_bytes(encoded)
                with self.assertRaises(MemoryStoreError):
                    load_maintenance_evidence(path, expected_root=self.root)
        path.write_bytes(original)
        wrong_path = path.with_name(f"{uuid.uuid4()}.json")
        wrong_path.write_bytes(original)
        try:
            with self.assertRaisesRegex(MemoryStoreError, "path"):
                load_maintenance_evidence(wrong_path, expected_root=self.root)
        finally:
            wrong_path.unlink()
        with tempfile.TemporaryDirectory() as other_root:
            with self.assertRaisesRegex(MemoryStoreError, "different memory root"):
                load_maintenance_evidence(path, expected_root=other_root)

    def test_maintenance_has_one_kernel_owned_instance(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def held_probe(_: Path) -> DiskPressure:
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return pressure(1_000)(self.root)

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                run_maintenance,
                self.root,
                apply=False,
                now=AFTER_TTL,
                pressure_probe=held_probe,
            )
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaisesRegex(MaintenanceBusy, "already running"):
                run_maintenance(
                    self.root,
                    apply=False,
                    now=AFTER_TTL,
                    pressure_probe=pressure(1_000),
                    lock_timeout=0.05,
                )
            release.set()
            self.assertEqual(first.result(timeout=5)["status"], "below_pressure_dry_run")

    def test_exact_rehydration_is_canonical_and_preserves_tombstone_history(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted()
        tombstone_path = self.store._tombstone_path(digest, receipt["action_event_id"])
        original_promote = self.store._promote_rehydration_stage
        ordering_observed: list[tuple[bool, str]] = []

        def observe_order(event_id: str, *args: object, **kwargs: object) -> str:
            canonical = any(
                candidate["event_id"] == event_id
                for path in self.store.events_dir.glob("*/*.jsonl")
                for _, candidate in self.store._read_segment(path)
            )
            with closing(self.store._connect()) as connection:
                row = connection.execute(
                    "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
                ).fetchone()
            ordering_observed.append((canonical, str(row["availability"])))
            return original_promote(event_id, *args, **kwargs)

        with mock.patch.object(self.store, "_promote_rehydration_stage", side_effect=observe_order):
            record = self.store.rehydrate_blob(
                content,
                expected_blob_sha256=digest,
                expected_size_bytes=len(content),
                expected_tombstone_sha256=receipt["tombstone_sha256"],
                expected_scopes=[scope],
                expected_source_event_ids=[source["event_id"]],
                provenance=self.provenance(digest),
                occurred_at="2026-02-01T00:00:00Z",
            )
        self.assertEqual(ordering_observed, [(True, "evicted")])
        self.assertEqual(record["type"], "blob/rehydrated")
        self.assertEqual(self.catalog(digest)["availability"], "hot")
        self.assertEqual(self.store._blob_path(digest).read_bytes(), content)
        self.assertTrue(tombstone_path.is_file())
        self.assertEqual(self.store._read_tombstone(digest), receipt)
        self.assertEqual(self.store.verify().event_count, 4)
        rebuilt = self.store.replay(rebuild=True)
        self.assertEqual(rebuilt.verified_events, 4)
        self.assertEqual(self.catalog(digest)["availability"], "hot")

    def test_rehydration_rejects_mismatches_without_write(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted("mismatch")
        before = self.store.verify().event_count
        cases = [
            {
                "content": content + b"x",
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [scope],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": self.provenance(digest),
                "error": ChainValidationError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": "sha256:" + "0" * 64,
                "expected_scopes": [scope],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": self.provenance(digest),
                "error": ProjectionError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [MemoryScope("other-owner")],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": self.provenance(digest),
                "error": ProjectionError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [scope],
                "expected_source_event_ids": [str(uuid.uuid4())],
                "provenance": self.provenance(digest),
                "error": ProjectionError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [scope],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": {
                    **self.provenance(digest),
                    "source_sha256": "sha256:" + "f" * 64,
                },
                "error": ChainValidationError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [scope],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": {
                    **self.provenance(digest),
                    "source_locator": "   ",
                },
                "error": MemoryStoreError,
            },
            {
                "content": content,
                "expected_size_bytes": len(content),
                "expected_tombstone_sha256": receipt["tombstone_sha256"],
                "expected_scopes": [scope],
                "expected_source_event_ids": [source["event_id"]],
                "provenance": {
                    **self.provenance(digest),
                    "authority": "bad\u0000authority",
                },
                "error": MemoryStoreError,
            },
        ]
        for case in cases:
            error = case.pop("error")
            with self.subTest(error=error.__name__), self.assertRaises(error):
                self.store.rehydrate_blob(
                    case.pop("content"),
                    expected_blob_sha256=digest,
                    **case,
                )
        self.assertEqual(self.store.verify().event_count, before)
        self.assertEqual(list(self.store.rehydration_dir.glob("*.blob")), [])
        self.assertFalse(self.store._blob_path(digest).exists())

    def test_rehydration_evaluator_controls_accept_equivalence_and_detect_mutation(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted("control")
        equivalent = {
            "source_kind": "operator_supplied",
            "source_locator": "operator://verified-export",
            "source_sha256": digest,
            "authority": "operator-a",
        }
        self.store.rehydrate_blob(
            content,
            expected_blob_sha256=digest,
            expected_size_bytes=len(content),
            expected_tombstone_sha256=receipt["tombstone_sha256"],
            expected_scopes=[scope],
            expected_source_event_ids=[source["event_id"]],
            provenance=equivalent,
            occurred_at="2026-02-01T00:00:00Z",
        )
        self.store.verify()
        hot = self.store._blob_path(digest)
        original = hot.read_bytes()
        hot.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ChainValidationError, "integrity"):
            self.store.verify()

    def test_rehydrate_then_re_evict_causal_chain_rebuild_and_predecessor_mutation(self) -> None:
        source, content, scope, _, digest, first_receipt = self.evicted("causal-cycle")
        self.store.rehydrate_blob(
            content,
            expected_blob_sha256=digest,
            expected_size_bytes=len(content),
            expected_tombstone_sha256=first_receipt["tombstone_sha256"],
            expected_scopes=[scope],
            expected_source_event_ids=[source["event_id"]],
            provenance=self.provenance(digest),
            occurred_at="2026-02-01T00:00:00Z",
        )
        later_plan = self.store.plan_retention(now="2026-03-01T00:00:00Z")
        self.store.run_gc(later_plan, apply=True)
        second_receipt = self.store._read_tombstone(digest)
        self.assertEqual(
            second_receipt["previous_tombstone_sha256"],
            first_receipt["tombstone_sha256"],
        )
        self.assertEqual(self.catalog(digest)["availability"], "evicted")
        self.store.verify()
        self.store.replay(rebuild=True)
        self.assertEqual(self.catalog(digest)["availability"], "evicted")

        path = self.store._tombstone_path(digest, second_receipt["action_event_id"])
        original = path.read_bytes()
        changed = dict(second_receipt)
        changed["previous_tombstone_sha256"] = "sha256:" + "0" * 64
        changed.pop("tombstone_sha256")
        changed["tombstone_sha256"] = (
            "sha256:" + hashlib.sha256(canonical_bytes(changed)).hexdigest()
        )
        path.write_bytes(canonical_bytes(changed))
        try:
            with self.assertRaisesRegex(ChainValidationError, "history is broken"):
                self.store.verify()
        finally:
            path.write_bytes(original)
        self.store.verify()

    def test_orphan_and_both_durable_rehydration_crash_phases_recover(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted("crash-event")
        orphan_id = str(uuid.uuid4())
        orphan = self.store._write_rehydration_stage(orphan_id, digest, content)
        self.assertTrue((self.root / orphan).is_file())
        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse((self.root / orphan).exists())

        original_apply = recovered._apply_event
        calls = 0

        def fail_actual_projection(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected death after canonical event")
            original_apply(*args, **kwargs)

        with mock.patch.object(recovered, "_apply_event", side_effect=fail_actual_projection):
            with self.assertRaisesRegex(ProjectionError, "event is durable"):
                recovered.rehydrate_blob(
                    content,
                    expected_blob_sha256=digest,
                    expected_size_bytes=len(content),
                    expected_tombstone_sha256=receipt["tombstone_sha256"],
                    expected_scopes=[scope],
                    expected_source_event_ids=[source["event_id"]],
                    provenance=self.provenance(digest),
                    occurred_at="2026-02-01T00:00:00Z",
                )
        self.assertFalse(recovered._blob_path(digest).exists())
        self.assertEqual(len(list(recovered.rehydration_dir.glob("*.blob"))), 1)
        replayed = MemoryStore(self.root, blob_threshold=256)
        self.assertEqual(self.catalog(digest)["availability"], "hot")
        self.assertEqual(replayed._blob_path(digest).read_bytes(), content)
        self.assertEqual(list(replayed.rehydration_dir.glob("*.blob")), [])

        source2, content2, scope2, _, digest2, receipt2 = self.append_then_evict_on(
            replayed, "crash-projection"
        )
        original_promote = replayed._promote_rehydration_stage

        def promote_then_die(*args: object, **kwargs: object) -> str:
            original_promote(*args, **kwargs)
            raise RuntimeError("injected death after atomic promotion")

        with mock.patch.object(
            replayed, "_promote_rehydration_stage", side_effect=promote_then_die
        ):
            with self.assertRaisesRegex(ProjectionError, "event is durable"):
                replayed.rehydrate_blob(
                    content2,
                    expected_blob_sha256=digest2,
                    expected_size_bytes=len(content2),
                    expected_tombstone_sha256=receipt2["tombstone_sha256"],
                    expected_scopes=[scope2],
                    expected_source_event_ids=[source2["event_id"]],
                    provenance=self.provenance(digest2),
                    occurred_at="2026-02-02T00:00:00Z",
                )
        self.assertTrue(replayed._blob_path(digest2).is_file())
        with closing(replayed._connect()) as connection:
            stale = connection.execute(
                "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?", (digest2,)
            ).fetchone()
        self.assertEqual(stale["availability"], "evicted")
        final = MemoryStore(self.root, blob_threshold=256)
        with closing(final._connect()) as connection:
            current = connection.execute(
                "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?", (digest2,)
            ).fetchone()
        self.assertEqual(current["availability"], "hot")
        final.verify()
        final.replay(rebuild=True)
        with closing(final._connect()) as connection:
            rebuilt = connection.execute(
                "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?", (digest2,)
            ).fetchone()
        self.assertEqual(rebuilt["availability"], "hot")

    def append_then_evict_on(self, store: MemoryStore, marker: str):
        original = self.store
        self.store = store
        try:
            return self.evicted(marker)
        finally:
            self.store = original

    def test_concurrent_duplicate_rehydration_has_one_canonical_winner(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted("concurrent")
        stores = [MemoryStore(self.root, blob_threshold=256) for _ in range(2)]
        gate = threading.Barrier(2)

        def worker(store: MemoryStore) -> str:
            gate.wait(timeout=5)
            try:
                store.rehydrate_blob(
                    content,
                    expected_blob_sha256=digest,
                    expected_size_bytes=len(content),
                    expected_tombstone_sha256=receipt["tombstone_sha256"],
                    expected_scopes=[scope],
                    expected_source_event_ids=[source["event_id"]],
                    provenance=self.provenance(digest),
                    occurred_at="2026-02-01T00:00:00Z",
                )
            except ProjectionError:
                return "rejected"
            return "accepted"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, stores))
        self.assertEqual(sorted(outcomes), ["accepted", "rejected"])
        verified = MemoryStore(self.root, blob_threshold=256).verify()
        self.assertEqual(verified.event_count, 4)
        self.assertTrue(self.store._blob_path(digest).is_file())

    def test_cross_process_duplicate_rehydration_has_one_winner(self) -> None:
        source, content, scope, _, digest, receipt = self.evicted("process-race")
        content_path = self.root / "exact-offline-copy.bin"
        config_path = self.root / "rehydration-request.json"
        gate_path = self.root / "rehydration-race.go"
        content_path.write_bytes(content)
        config_path.write_text(
            json.dumps(
                {
                    "digest": digest,
                    "size": len(content),
                    "tombstone": receipt["tombstone_sha256"],
                    "scope": {
                        "owner_id": scope.owner_id,
                        "workspace_id": scope.workspace_id,
                        "task_id": scope.task_id,
                        "agent_role": scope.agent_role,
                        "session_id": scope.session_id,
                    },
                    "source_event_id": source["event_id"],
                    "provenance": self.provenance(digest),
                }
            ),
            encoding="utf-8",
        )
        worker = r"""
import json
import sys
import time
from pathlib import Path

package, root, request_path, content_path, gate_path, ready_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(package / "src"))
from agent_continuity.memory_store import MemoryStore

request = json.loads(request_path.read_text(encoding="utf-8"))
store = MemoryStore(root, blob_threshold=256)
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 20
while not gate_path.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("rehydration race gate did not open")
    time.sleep(0.01)
try:
    store.rehydrate_blob(
        content_path.read_bytes(),
        expected_blob_sha256=request["digest"],
        expected_size_bytes=request["size"],
        expected_tombstone_sha256=request["tombstone"],
        expected_scopes=[request["scope"]],
        expected_source_event_ids=[request["source_event_id"]],
        provenance=request["provenance"],
        occurred_at="2026-02-01T00:00:00Z",
    )
except Exception as error:
    print(json.dumps({"outcome": "rejected", "error_type": type(error).__name__}))
else:
    print(json.dumps({"outcome": "accepted"}))
"""
        processes: list[subprocess.Popen[str]] = []
        try:
            for index in range(2):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker,
                            str(PACKAGE),
                            str(self.root),
                            str(config_path),
                            str(content_path),
                            str(gate_path),
                            str(self.root / f"rehydrate-worker-{index}.ready"),
                        ],
                        cwd=PACKAGE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 20
            while not all(
                (self.root / f"rehydrate-worker-{index}.ready").exists() for index in range(2)
            ):
                if time.monotonic() >= deadline:
                    self.fail("rehydration workers did not become ready")
                time.sleep(0.01)
            gate_path.write_text("go", encoding="utf-8")
            outputs = [process.communicate(timeout=30) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
        observations = []
        for process, (stdout, stderr) in zip(processes, outputs, strict=True):
            self.assertEqual(process.returncode, 0, f"stdout={stdout} stderr={stderr}")
            observations.append(json.loads(stdout))
        self.assertEqual(
            sorted(item["outcome"] for item in observations),
            ["accepted", "rejected"],
        )
        rejected = next(item for item in observations if item["outcome"] == "rejected")
        self.assertEqual(rejected["error_type"], "ProjectionError")
        self.assertEqual(MemoryStore(self.root, blob_threshold=256).verify().event_count, 4)

    @unittest.skipUnless(os.name == "nt", "Windows Scheduled Task contract")
    def test_windows_installer_dry_run_and_empty_root_runner(self) -> None:
        installer = PACKAGE / "windows" / "Install-MemoryMaintenance.ps1"
        runner = PACKAGE / "windows" / "Run-MemoryMaintenance.ps1"
        runtime = self.root / "runtime"
        plan = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(installer),
                "-MemoryRoot",
                str(self.root),
                "-RuntimeRoot",
                str(runtime),
            ],
            cwd=PACKAGE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        contract = json.loads(plan.stdout)
        self.assertEqual(contract["mode"], "dry_run_no_writes")
        self.assertEqual(contract["task_name"], "Coding Intelligence - Memory Maintenance")
        self.assertEqual(contract["run_level"], "Limited")
        self.assertEqual(contract["multiple_instances"], "IgnoreNew")
        self.assertFalse(runtime.exists())

        config = self.root / "runner-config.json"
        module_root = self.root / "python"
        deployed_package = module_root / "agent_continuity"
        deployed_package.mkdir(parents=True)
        deployed_modules = (
            "__init__.py",
            "memory_maintenance.py",
            "memory_store.py",
            "retention.py",
            "compaction.py",
        )
        for module in deployed_modules:
            shutil.copy2(
                PACKAGE / "src" / "agent_continuity" / module,
                deployed_package / module,
            )
        config.write_text(
            json.dumps(
                {
                    "schema": "coding-intelligence.memory-maintenance-runtime/v1",
                    "python_path": sys.executable,
                    "module_root": str(module_root),
                    "memory_root": str(self.root),
                    "runner_root": str(self.root / "runner"),
                    "runner_sha256": "sha256:" + hashlib.sha256(runner.read_bytes()).hexdigest(),
                    "max_items": 1,
                    "max_bytes": 1_024,
                    "max_wall_seconds": 5,
                    "hot_blob_budget_bytes": 1_048_576,
                    "module_sha256": {
                        module: "sha256:"
                        + hashlib.sha256((deployed_package / module).read_bytes()).hexdigest()
                        for module in deployed_modules
                    },
                }
            ),
            encoding="utf-8",
        )
        executed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(runner),
                "-RuntimeConfig",
                str(config),
            ],
            cwd=PACKAGE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        result = json.loads(executed.stdout)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["child_exit_code"], 0)
        self.assertIn(
            result["maintenance_status"],
            {"below_pressure_dry_run", "pressure_no_eligible_items"},
        )

        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            runtime_link = self.root / "runtime-link-control"
            if not self.create_directory_link(runtime_link, outside):
                self.skipTest("runtime junction creation unavailable")
            task_name = "Coding Intelligence - Memory Maintenance Junction Control"
            try:
                for apply_switch in (False, True):
                    arguments = [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(installer),
                        "-TaskName",
                        task_name,
                        "-MemoryRoot",
                        str(self.root),
                        "-RuntimeRoot",
                        str(runtime_link),
                    ]
                    if apply_switch:
                        arguments.append("-Apply")
                    guarded = subprocess.run(
                        arguments,
                        cwd=PACKAGE,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(guarded.returncode, 0)
                    self.assertIn("reparse", guarded.stderr.lower())
                    self.assertEqual(list(outside.iterdir()), [])
            finally:
                subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "Unregister-ScheduledTask -TaskName "
                        "$env:CI_TEST_TASK -Confirm:$false -ErrorAction SilentlyContinue",
                    ],
                    env={**os.environ, "CI_TEST_TASK": task_name},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                runtime_link.rmdir()

        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            guard_root = self.root / "runner-junction-control"
            guard_package = guard_root / "python" / "agent_continuity"
            guard_package.mkdir(parents=True)
            for module in deployed_modules:
                shutil.copy2(
                    PACKAGE / "src" / "agent_continuity" / module,
                    guard_package / module,
                )
            runner_link = guard_root / "runner"
            if not self.create_directory_link(runner_link, outside):
                self.skipTest("runner junction creation unavailable")
            guard_config = guard_root / "runtime-config.json"
            guard_config.write_text(
                json.dumps(
                    {
                        "schema": "coding-intelligence.memory-maintenance-runtime/v1",
                        "python_path": sys.executable,
                        "module_root": str(guard_root / "python"),
                        "memory_root": str(self.root),
                        "runner_root": str(runner_link),
                        "runner_sha256": "sha256:"
                        + hashlib.sha256(runner.read_bytes()).hexdigest(),
                        "max_items": 1,
                        "max_bytes": 1_024,
                        "max_wall_seconds": 5,
                        "hot_blob_budget_bytes": 1_048_576,
                        "module_sha256": {
                            module: "sha256:"
                            + hashlib.sha256((guard_package / module).read_bytes()).hexdigest()
                            for module in deployed_modules
                        },
                    }
                ),
                encoding="utf-8",
            )
            try:
                guarded_runner = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(runner),
                        "-RuntimeConfig",
                        str(guard_config),
                    ],
                    cwd=PACKAGE,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(guarded_runner.returncode, 0)
                self.assertIn("reparse", guarded_runner.stderr.lower())
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                runner_link.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows Scheduled Task contract")
    def test_windows_task_validator_rejects_mutated_trigger(self) -> None:
        installer = PACKAGE / "windows" / "Install-MemoryMaintenance.ps1"
        task_name = "Coding Intelligence - Memory Maintenance Contract Control"
        runtime = self.root / "task-contract-runtime"
        try:
            installed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(installer),
                    "-TaskName",
                    task_name,
                    "-MemoryRoot",
                    str(self.root),
                    "-RuntimeRoot",
                    str(runtime),
                    "-IntervalMinutes",
                    "60",
                    "-Apply",
                ],
                cwd=PACKAGE,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            install_report = json.loads(installed.stdout)
            self.assertEqual(install_report["trigger_count"], 1)
            self.assertTrue(install_report["trigger_enabled"])
            verifier = runtime / "Test-MemoryMaintenanceTask.ps1"
            config = runtime / "runtime-config.json"
            valid = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(verifier),
                    "-RuntimeConfig",
                    str(config),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["status"], "valid")
            mutated = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$bad=New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) "
                    "-RepetitionInterval (New-TimeSpan -Minutes 15); "
                    "Set-ScheduledTask -TaskName $env:CI_TEST_TASK -Trigger $bad | Out-Null",
                ],
                env={**os.environ, "CI_TEST_TASK": task_name},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(mutated.returncode, 0, mutated.stderr)
            rejected = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(verifier),
                    "-RuntimeConfig",
                    str(config),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("ownership contract", rejected.stderr)
        finally:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Unregister-ScheduledTask -TaskName "
                    "$env:CI_TEST_TASK -Confirm:$false -ErrorAction SilentlyContinue",
                ],
                env={**os.environ, "CI_TEST_TASK": task_name},
                capture_output=True,
                text=True,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
