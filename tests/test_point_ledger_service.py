"""记分资格账本服务层：事务、幂等物化、业务时钟与权限。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from point_ledger.api import LedgerApplication
from point_ledger.clock import FrozenClock
from point_ledger.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from point_ledger.rules import MEASURE_FULL_STUDY, MEASURE_RESTORE, MEASURE_STUDY, MEASURE_SUSPEND
from point_ledger.service import PointLedgerService


def make_service(start: datetime | None = None) -> PointLedgerService:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(start or datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
    service = PointLedgerService(connection, clock)
    service.bootstrap()
    return service


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.service.register_driver("officer", "D1", "2020-06-10")

    def add(self, penalty, violation, points, occurred, *, key=None):
        return self.service.record_penalty(
            "officer", "D1", penalty_id=penalty, violation_id=violation,
            points=points, occurred_at=occurred, idempotency_key=key)

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_driver("auditor", "D2", "2020-06-10")
        with self.assertRaises(Forbidden):
            self.service.revoke_penalty("officer", "D1", penalty_id="P1", reason="revoked")
        with self.assertRaises(Forbidden):
            self.service.complete_study("officer", "D1", study_record_id="S1")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("officer")

    def test_unknown_driver_and_rule_gap_are_rejected(self) -> None:
        with self.assertRaises(NotFound):
            self.add("P1", "V1", 3, "2026-07-01T00:00:00Z") if False else \
                self.service.record_penalty(
                    "officer", "MISSING", penalty_id="P1", violation_id="V1",
                    points=3, occurred_at="2026-07-01T00:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.add("P1", "V1", 13, "2026-07-01T00:00:00Z")

    def test_penalty_recording_is_idempotent_by_penalty_and_key(self) -> None:
        first = self.add("P1", "V1", 3, "2026-07-01T00:00:00Z", key="k1")
        replay = self.add("P1", "V1", 3, "2026-07-01T00:00:00Z", key="k1")
        duplicate_decision = self.add("P1-DUP", "V1", 3, "2026-07-01T00:00:00Z")
        self.assertFalse(first["duplicate"])
        # 相同幂等键重放返回首次结果，不会产生第二笔记账
        self.assertEqual(replay["seq"], first["seq"])
        # 同违法的另一决定被判为重复决定
        self.assertTrue(duplicate_decision["duplicate"])
        self.assertEqual(self.service.ledger("auditor", "D1")["current_points"], 3)
        self.assertEqual(
            self.service.connection.execute(
                "SELECT COUNT(*) FROM point_ledger_events WHERE kind='penalty_added'"
            ).fetchone()[0], 1)

    def test_measures_materialize_once_across_repeated_runs(self) -> None:
        self.add("P1", "V1", 6, "2026-07-01T00:00:00Z")
        self.add("P2", "V2", 6, "2026-07-02T00:00:00Z")
        runs = [self.service.advance("officer") for _ in range(3)]
        ledger = self.service.ledger("auditor", "D1")
        self.assertEqual(len(ledger["measures"]), 3)  # study, full_study, suspend
        self.assertTrue(all(run["changed"] == [] for run in runs[1:]))

    def test_full_study_clears_points_and_restore_waits_for_suspend_period(self) -> None:
        self.add("P1", "V1", 6, "2026-09-01T00:00:00Z")
        self.add("P2", "V2", 6, "2026-09-02T00:00:00Z")
        with self.assertRaises(InvalidState):
            # 只能登记满分学习，不能走普通学习消分
            self.service.complete_study("reviewer", "D1", study_record_id="S1",
                                        kind="period_study")
        self.service.clock.current = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.service.complete_study("reviewer", "D1", study_record_id="S1",
                                    kind="full_study")
        self.assertEqual(self.service.ledger("auditor", "D1")["current_points"], 0)
        # 限制期未满，不生成恢复
        self.service.advance("officer")
        measures = self.service.ledger("auditor", "D1")["measures"]
        self.assertFalse(any(m["measure"] == MEASURE_RESTORE for m in measures))
        # 届满后恢复，且重复学习登记被拒绝
        self.service.clock.current = datetime(2026, 9, 10, tzinfo=timezone.utc)
        self.service.advance("officer")
        measures = self.service.ledger("auditor", "D1")["measures"]
        self.assertTrue(any(m["measure"] == MEASURE_RESTORE and m["status"] == "released"
                            for m in measures))
        with self.assertRaises(Conflict):
            self.service.complete_study("reviewer", "D1", study_record_id="S1",
                                        kind="full_study")

    def test_appeal_reversal_rescinds_measures_and_keeps_history(self) -> None:
        self.add("P1", "V1", 6, "2026-07-01T00:00:00Z")
        self.add("P2", "V2", 6, "2026-07-02T00:00:00Z")
        before = self.service.events("auditor", "D1")
        self.service.revoke_penalty("reviewer", "D1", penalty_id="P2", reason="appeal",
                                    occurred_at="2026-08-20T00:00:00Z")
        # 再次撤销幂等
        again = self.service.revoke_penalty("reviewer", "D1", penalty_id="P2", reason="appeal")
        self.assertTrue(again["duplicate"])
        ledger = self.service.ledger("auditor", "D1")
        self.assertEqual(ledger["current_points"], 6)
        self.assertTrue(all(m["status"] == "rescinded" for m in ledger["measures"]))
        after = self.service.events("auditor", "D1")
        self.assertEqual(len(after), len(before) + 1)  # 只追加，不改旧行
        with self.assertRaises(NotFound):
            self.service.revoke_penalty("reviewer", "D1", penalty_id="NOPE", reason="revoked")

    def test_period_study_flow_at_nine_points(self) -> None:
        self.add("P1", "V1", 3, "2026-07-01T00:00:00Z")
        self.add("P2", "V2", 3, "2026-07-02T00:00:00Z")
        self.add("P3", "V3", 3, "2026-07-03T00:00:00Z")
        ledger = self.service.ledger("auditor", "D1")
        self.assertEqual([m["measure"] for m in ledger["measures"]], [MEASURE_STUDY])
        self.service.complete_study("reviewer", "D1", study_record_id="S1")
        ledger = self.service.ledger("auditor", "D1")
        self.assertEqual(ledger["current_points"], 3)
        study = next(m for m in ledger["measures"] if m["measure"] == MEASURE_STUDY)
        self.assertEqual(study["status"], "released")

    def test_cycle_carryover_resets_at_boundary(self) -> None:
        self.service.register_driver("officer", "D2", "2021-09-01")
        self.service.record_penalty("officer", "D2", penalty_id="P1", violation_id="V1",
                                    points=3, occurred_at="2026-03-01T00:00:00Z")
        self.service.advance("officer")
        ledger = self.service.ledger("auditor", "D2")
        self.assertEqual(ledger["current_cycle_start"], "2026-09-01")
        self.assertEqual(ledger["current_points"], 0)
        reset = [e for e in self.service.events("auditor", "D2") if e["kind"] == "cycle_reset"]
        self.assertEqual(len(reset), 1)
        # 再次推进不产生第二个结转事件
        self.service.advance("officer")
        self.assertEqual(len([e for e in self.service.events("auditor", "D2")
                              if e["kind"] == "cycle_reset"]), 1)

    def test_new_rule_version_only_affects_its_range(self) -> None:
        # 2027 起学习阈值降为 6 分；2026 年的违法仍适用旧规则
        self.service.register_rule(
            "admin", "v2027", "2027-01-01",
            {MEASURE_STUDY: 6, MEASURE_FULL_STUDY: 12, MEASURE_SUSPEND: 12}, 7)
        self.add("P1", "V1", 6, "2026-07-01T00:00:00Z")
        old = self.service.ledger("auditor", "D1")
        self.assertEqual(old["measures"], [])
        self.service.register_driver("officer", "D2", "2020-06-10")
        self.service.clock.current = datetime(2027, 2, 1, tzinfo=timezone.utc)
        self.service.record_penalty("officer", "D2", penalty_id="P1", violation_id="V1",
                                    points=6, occurred_at="2027-01-02T00:00:00Z")
        self.service.advance("officer")
        new = self.service.ledger("auditor", "D2")
        self.assertEqual(new["measures"][0]["rule_version"], "v2027")
        with self.assertRaises(ValidationFailed):
            self.service.register_rule("admin", "bad", "2021-01-01",
                                       {MEASURE_STUDY: 9}, None)

    def test_ledger_query_is_read_only(self) -> None:
        self.add("P1", "V1", 12, "2026-07-01T00:00:00Z")
        count = self.service.connection.execute(
            "SELECT COUNT(*) FROM point_measures").fetchone()[0]
        self.assertEqual(count, 3)
        self.service.ledger("auditor", "D1")
        self.service.ledger("auditor", "D1")
        self.assertEqual(
            self.service.connection.execute(
                "SELECT COUNT(*) FROM point_measures").fetchone()[0],
            count,
        )

    def test_audit_chain_is_complete_and_valid(self) -> None:
        self.add("P1", "V1", 3, "2026-07-01T00:00:00Z")
        chain = self.service.audit_chain("auditor")
        self.assertTrue(chain["valid"])
        self.assertTrue(len(chain["events"]) >= 2)
        # 篡改任何一条都会被发现
        self.service.connection.execute(
            "UPDATE ledger_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.app = LedgerApplication(self.service)

    def request(self, method, path, body=None, actor="officer"):
        import json
        payload = json.dumps(body or {}).encode()
        return self.app.handle(method, path, {"X-Actor-Id": actor,
                                              "Content-Type": "application/json"}, payload)

    def test_health_and_full_flow(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.request("POST", "/drivers",
                                {"driver_id": "D1", "license_issued_on": "2020-06-10"})
        self.assertEqual(response.status, 201)
        response = self.request("POST", "/drivers/D1/penalties", {
            "penalty_id": "P1", "violation_id": "V1", "points": 6,
            "occurred_at": "2026-07-01T00:00:00Z"})
        self.assertEqual(response.status, 201)
        response = self.request("POST", "/drivers/D1/penalties", {
            "penalty_id": "P2", "violation_id": "V2", "points": 6,
            "occurred_at": "2026-07-02T00:00:00Z"})
        self.assertEqual(response.status, 201)
        ledger = self.request("GET", "/drivers/D1/ledger", actor="auditor")
        self.assertEqual(ledger.status, 200)
        self.assertEqual(ledger.body["current_points"], 12)
        revoked = self.request("POST", "/drivers/D1/revocations",
                               {"penalty_id": "P2", "reason": "appeal"}, actor="reviewer")
        self.assertEqual(revoked.status, 201)
        ledger = self.request("GET", "/drivers/D1/ledger", actor="auditor")
        self.assertEqual(ledger.body["current_points"], 6)

    def test_missing_actor_and_unknown_route(self) -> None:
        response = self.app.handle("GET", "/drivers/D1/ledger")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/nope", {"x-actor-id": "auditor"})
        self.assertEqual(response.status, 404)

    def test_forbidden_maps_to_403(self) -> None:
        self.request("POST", "/drivers", {"driver_id": "D1", "license_issued_on": "2020-06-10"})
        response = self.request("POST", "/drivers/D1/revocations",
                                {"penalty_id": "P1", "reason": "revoked"}, actor="auditor")
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
