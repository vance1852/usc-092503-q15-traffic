"""记分规则与滚动周期的纯领域测试。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from point_ledger.rules import (
    DEFAULT_RULE_VERSION,
    EVENT_CYCLE_RESET,
    EVENT_PENALTY_ADDED,
    EVENT_PENALTY_REMOVED,
    EVENT_POINTS_CLEARED,
    MEASURE_FULL_STUDY,
    MEASURE_STUDY,
    MEASURE_SUSPEND,
    DriverProfile,
    LedgerEvent,
    Rule,
    RuleBook,
    add_months,
    cycle_start_for,
    integer_points,
    next_cycle_start,
    recompute,
)


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event(seq, kind, points, occurred, cycle, *, penalty=None, violation=None,
          rule=DEFAULT_RULE_VERSION, reason="penalty_effective", dedupe=None, ref=None,
          clear_kind=None):
    return LedgerEvent(
        seq=seq, driver_id="D1", kind=kind, points=points,
        occurred_at=occurred, recorded_at=occurred, cycle_start=cycle,
        rule_version=rule, penalty_id=penalty, violation_id=violation,
        clear_kind=clear_kind, reason=reason,
        dedupe_key=dedupe or f"{kind}:{seq}", ref_event_seq=ref,
    )


DRIVER = DriverProfile.create("D1", "2020-06-10")
CYCLE = "2026-06-10"


class CycleTests(unittest.TestCase):
    def test_cycle_start_uses_license_anniversary(self) -> None:
        issued = date(2020, 6, 10)
        self.assertEqual(cycle_start_for(at("2026-06-09T23:59:00Z"), issued), date(2025, 6, 10))
        self.assertEqual(cycle_start_for(at("2026-06-10T00:00:00Z"), issued), date(2026, 6, 10))
        self.assertEqual(cycle_start_for(at("2027-01-01T00:00:00Z"), issued), date(2026, 6, 10))

    def test_add_months_handles_month_end(self) -> None:
        self.assertEqual(add_months(date(2024, 2, 29), 12), date(2025, 2, 28))
        self.assertEqual(next_cycle_start(date(2024, 2, 29)), date(2025, 2, 28))

    def test_integer_points_validation(self) -> None:
        self.assertEqual(integer_points(3), 3)
        self.assertEqual(integer_points("6"), 6)
        for bad in (-1, 13, 2.5, "x", True):
            with self.assertRaises(ValueError):
                integer_points(bad)


class RuleBookTests(unittest.TestCase):
    def test_rules_apply_only_within_explicit_range(self) -> None:
        book = RuleBook([
            Rule("old", "2020-01-01", "2022-04-01", {MEASURE_STUDY: 9}, None),
            Rule("new", "2022-04-01", None, {MEASURE_STUDY: 9}, 7),
        ])
        self.assertEqual(book.for_day(date(2022, 3, 31)).version, "old")
        self.assertEqual(book.for_day(date(2022, 4, 1)).version, "new")
        with self.assertRaises(LookupError):
            book.for_day(date(2019, 12, 31))

    def test_overlapping_and_open_ended_rules_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuleBook([
                Rule("a", "2020-01-01", None, {MEASURE_STUDY: 9}, None),
                Rule("b", "2021-01-01", None, {MEASURE_STUDY: 9}, None),
            ])
        with self.assertRaises(ValueError):
            RuleBook([
                Rule("a", "2020-01-01", "2022-01-01", {MEASURE_STUDY: 9}, None),
                Rule("b", "2021-01-01", None, {MEASURE_STUDY: 9}, None),
            ])


class RecomputeTests(unittest.TestCase):
    def test_thresholds_current_and_upcoming(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 6, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_ADDED, 3, "2026-08-01T00:00:00Z", CYCLE,
                  penalty="P2", violation="V2", dedupe="add:P2"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_points"], 9)
        measures = {c["measure"] for c in result["crossings"] if c["active"]}
        self.assertEqual(measures, {MEASURE_STUDY})
        self.assertEqual(result["due_actions"][0]["measure"], MEASURE_STUDY)
        self.assertEqual(result["upcoming"][0]["measure"], MEASURE_FULL_STUDY)
        self.assertEqual(result["upcoming"][0]["points_needed"], 3)

    def test_full_score_triggers_full_study_and_suspend_with_due_date(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 6, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_ADDED, 6, "2026-07-02T00:00:00Z", CYCLE,
                  penalty="P2", violation="V2", dedupe="add:P2"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        due = {d["measure"]: d for d in result["due_actions"]}
        self.assertEqual(set(due), {MEASURE_STUDY, MEASURE_FULL_STUDY, MEASURE_SUSPEND})
        self.assertEqual(due[MEASURE_SUSPEND]["suspend_days"], 7)

    def test_duplicate_decision_for_same_violation_is_suppressed(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 3, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_ADDED, 3, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P2", violation="V1", dedupe="add:P2"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_points"], 3)
        self.assertEqual(result["postings"][1]["effective"], False)
        self.assertEqual(result["postings"][1]["reason"], "duplicate_decision")

    def test_revocation_appends_reversal_without_touching_old_row(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 6, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_ADDED, 6, "2026-07-02T00:00:00Z", CYCLE,
                  penalty="P2", violation="V2", dedupe="add:P2"),
            event(3, EVENT_PENALTY_REMOVED, 6, "2026-08-01T00:00:00Z", CYCLE,
                  penalty="P2", reason="appeal", dedupe="rm:P2", ref=2),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_points"], 6)
        full = [c for c in result["crossings"] if c["measure"] == MEASURE_FULL_STUDY]
        self.assertEqual(full[0]["active"], False)
        self.assertEqual(full[0]["resolve"], "reversed")
        # 原加记事件保持不变
        self.assertEqual(events[1].kind, EVENT_PENALTY_ADDED)
        self.assertEqual(events[1].points, 6)

    def test_revocation_then_new_decision_for_same_violation_scores_again(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 6, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_REMOVED, 6, "2026-07-10T00:00:00Z", CYCLE,
                  penalty="P1", reason="revoked", dedupe="rm:P1", ref=1),
            event(3, EVENT_PENALTY_ADDED, 3, "2026-08-01T00:00:00Z", CYCLE,
                  penalty="P3", violation="V1", dedupe="add:P3"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_points"], 3)
        self.assertTrue(result["postings"][2]["effective"])

    def test_points_cleared_resolves_study_crossing(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 6, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_PENALTY_ADDED, 3, "2026-08-01T00:00:00Z", CYCLE,
                  penalty="P2", violation="V2", dedupe="add:P2"),
            event(3, EVENT_POINTS_CLEARED, 6, "2026-08-10T00:00:00Z", CYCLE,
                  reason="study_completed:period_study", dedupe="clear:S1", ref=2,
                  clear_kind="period_study"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_points"], 3)
        study = [c for c in result["crossings"] if c["measure"] == MEASURE_STUDY][0]
        self.assertFalse(study["active"])
        self.assertEqual(study["resolve"], "cleared")
        self.assertEqual(result["due_actions"], [])

    def test_cycle_reset_does_not_carry_points_forward(self) -> None:
        old_cycle = "2025-06-10"
        events = [
            event(1, EVENT_PENALTY_ADDED, 3, "2025-08-01T00:00:00Z", old_cycle,
                  penalty="P1", violation="V1", dedupe="add:P1"),
            event(2, EVENT_CYCLE_RESET, 3, "2026-06-09T16:00:00Z", old_cycle,
                  rule=None, reason="cycle_boundary_reset", dedupe="reset:D1:2025-06-10"),
        ]
        result = recompute(events, DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(result["current_cycle_start"], "2026-06-10")
        self.assertEqual(result["current_points"], 0)
        old = next(c for c in result["cycles"] if c["cycle_start"] == old_cycle)
        self.assertEqual(old["carried_reset"], 3)
        self.assertEqual(old["closing"], 0)

    def test_recompute_is_deterministic_and_order_independent_for_seq(self) -> None:
        events = [
            event(2, EVENT_PENALTY_ADDED, 3, "2026-08-01T00:00:00Z", CYCLE,
                  penalty="P2", violation="V2", dedupe="add:P2"),
            event(1, EVENT_PENALTY_ADDED, 3, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
        ]
        a = recompute(list(events), DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        b = recompute(list(reversed(events)), DRIVER, as_of=at("2026-09-01T00:00:00Z"))
        self.assertEqual(a["current_points"], b["current_points"])
        self.assertEqual([p["balance_after"] for p in a["postings"]], [3, 6])

    def test_due_actions_respect_business_clock(self) -> None:
        events = [
            event(1, EVENT_PENALTY_ADDED, 12, "2026-07-01T00:00:00Z", CYCLE,
                  penalty="P1", violation="V1", dedupe="add:P1"),
        ]
        before = recompute(events, DRIVER, as_of=at("2026-06-30T00:00:00Z"))
        after = recompute(events, DRIVER, as_of=at("2026-07-02T00:00:00Z"))
        self.assertEqual(before["due_actions"], [])
        self.assertEqual(len(after["due_actions"]), 3)


if __name__ == "__main__":
    unittest.main()
