"""记分规则、滚动周期与措施阈值的纯领域逻辑。

本模块不接触数据库与墙上时钟：对同一组规则版本与事件输入，
任何时刻重放都得到完全相同的结果，这是资格账本可以确定性重算、
措施不会被重复执行的基础。

记分制度（交管所 139/162 号令体系）要点：
- 一个记分周期为 12 个月，满分 12 分，周期自驾驶证初次领取日的周年起算；
- 周期内累计记分达到 9 分的，应当参加学习消分；
- 达到 12 分的，应当参加满分学习和考试，并扣留驾驶证；
- 一个周期内未达到 12 分的，周期届满后余分清除，不结转下一周期。
具体阈值与扣留时长通过规则版本调整，新版本只在明确的生效区间内适用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

# 默认规则版本（162 号令阈值）
DEFAULT_RULE_VERSION = "v2022"

# 一个记分周期的满分与月数
MAX_POINTS = 12
CYCLE_MONTHS = 12

# 措施类型
MEASURE_STUDY = "study"              # 学习消分通知
MEASURE_FULL_STUDY = "full_study"    # 满分学习与考试通知
MEASURE_SUSPEND = "suspend"          # 扣留驾驶证
MEASURE_RESTORE = "restore"          # 限制期届满、考试合格后恢复资格

# 阈值从低到高的判定顺序
MEASURE_ORDER = (MEASURE_STUDY, MEASURE_FULL_STUDY, MEASURE_SUSPEND)

THRESHOLD_STUDY = 9
THRESHOLD_FULL = 12
THRESHOLD_SUSPEND = 12

# 满分后扣留的默认时长（自然日）
DEFAULT_SUSPEND_DAYS = 7

# 普通学习一次最多消减的分值
PERIOD_STUDY_CLEAR_POINTS = 6

# 事件类型
EVENT_PENALTY_ADDED = "penalty_added"
EVENT_PENALTY_REMOVED = "penalty_removed"
EVENT_POINTS_CLEARED = "points_cleared"
EVENT_CYCLE_RESET = "cycle_reset"

REMOVE_REASON_REVOKED = "revoked"   # 处罚决定被撤销
REMOVE_REASON_APPEAL = "appeal"     # 申诉变更

CLEAR_KIND_PERIOD = "period_study"  # 普通学习消分
CLEAR_KIND_FULL = "full_study"      # 满分学习考试合格清零


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def integer_points(value: object, field_name: str = "points") -> int:
    """记分必须是 0 到 MAX_POINTS 的整数。"""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是整数")
    if isinstance(value, int):
        points = value
    else:
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{field_name} 必须是整数") from exc
        if decimal != decimal.to_integral_value():
            raise ValueError(f"{field_name} 必须是整数")
        points = int(decimal)
    if not 0 <= points <= MAX_POINTS:
        raise ValueError(f"{field_name} 必须在 0 到 {MAX_POINTS} 之间")
    return points


def parse_event_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("时间必须是 ISO 8601 格式") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def add_months(day: date, months: int) -> date:
    """day 加 months 个月后的同日（月末自动回落，如 2 月 29 日）。"""
    month_index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def cycle_start_for(moment: datetime, license_issued_on: date) -> date:
    """moment 所在记分周期的起始日（领证日的周年日）。"""
    incident_date = moment.astimezone(timezone.utc).date()
    years = incident_date.year - license_issued_on.year
    start = license_issued_on.replace(year=license_issued_on.year + years)
    if start > incident_date:
        start = start.replace(year=start.year - 1)
    return start


def next_cycle_start(start_on: date) -> date:
    return add_months(start_on, CYCLE_MONTHS)


@dataclass(frozen=True, slots=True)
class Rule:
    """一版记分规则，只在 [effective_from, effective_to) 内适用。"""

    version: str
    effective_from: str          # ISO 日期，含当日
    effective_to: str | None     # ISO 日期，不含当日；None 表示至今
    thresholds: Mapping[str, int]
    suspend_days: int | None

    def applies_on(self, day: date) -> bool:
        start = date.fromisoformat(self.effective_from)
        if day < start:
            return False
        if self.effective_to is not None and day >= date.fromisoformat(self.effective_to):
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "thresholds": dict(self.thresholds),
            "suspend_days": self.suspend_days,
        }


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        version=DEFAULT_RULE_VERSION,
        effective_from="2022-04-01",
        effective_to=None,
        thresholds={
            MEASURE_STUDY: THRESHOLD_STUDY,
            MEASURE_FULL_STUDY: THRESHOLD_FULL,
            MEASURE_SUSPEND: THRESHOLD_SUSPEND,
        },
        suspend_days=DEFAULT_SUSPEND_DAYS,
    ),
)


class RuleBook:
    """按违法发生日期选择适用规则；规则调整只影响明确的生效范围。"""

    def __init__(self, rules: Sequence[Rule] = DEFAULT_RULES) -> None:
        ordered = sorted(rules, key=lambda item: (item.effective_from, item.version))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None:
                raise ValueError("只有最后一版规则可以不设结束日期")
            if previous.effective_to > current.effective_from:
                raise ValueError("规则生效区间不能重叠")
        self._rules = tuple(ordered)

    def for_day(self, day: date) -> Rule:
        for rule in self._rules:
            if rule.applies_on(day):
                return rule
        raise LookupError(f"{day.isoformat()} 没有适用的记分规则版本")

    def for_incident(self, incident_at: datetime) -> Rule:
        return self.for_day(incident_at.astimezone(timezone.utc).date())

    def versions(self) -> tuple[Rule, ...]:
        return self._rules


@dataclass(frozen=True, slots=True)
class DriverProfile:
    driver_id: str
    license_issued_on: date

    @classmethod
    def create(cls, driver_id: str, license_issued_on: str | date) -> "DriverProfile":
        if isinstance(license_issued_on, date):
            issued = license_issued_on
        else:
            issued = date.fromisoformat(license_issued_on)
        return cls(driver_id=driver_id, license_issued_on=issued)


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """不可变的记账事实。旧余额永不被改写，撤销与结转只追加新事件。"""

    seq: int
    driver_id: str
    kind: str
    points: int
    occurred_at: str            # 业务事实时间（违法发生/撤销生效/学习完成/周期边界）
    recorded_at: str            # 登记时间（业务时钟）
    cycle_start: str
    rule_version: str | None
    penalty_id: str | None
    violation_id: str | None
    clear_kind: str | None
    reason: str
    dedupe_key: str
    ref_event_seq: int | None   # 撤销/消分引用的原事件

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "driver_id": self.driver_id,
            "kind": self.kind,
            "points": self.points,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "cycle_start": self.cycle_start,
            "rule_version": self.rule_version,
            "penalty_id": self.penalty_id,
            "violation_id": self.violation_id,
            "clear_kind": self.clear_kind,
            "reason": self.reason,
            "dedupe_key": self.dedupe_key,
            "ref_event_seq": self.ref_event_seq,
        }


@dataclass(slots=True)
class _Cycle:
    opening: int = 0
    added: int = 0
    removed: int = 0
    cleared: int = 0
    carried_reset: int = 0
    closing: int = 0


@dataclass(frozen=True, slots=True)
class Posting:
    """事件流中的一笔账：对余额的实际影响与余额后值（形成过程）。"""

    seq: int
    kind: str
    delta: int
    effective: bool
    balance_after: int
    cycle_start: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "delta": self.delta,
            "effective": self.effective,
            "balance_after": self.balance_after,
            "cycle_start": self.cycle_start,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Crossing:
    """一次阈值跨越事实。status 表示它在重放结束时是否仍然成立。"""

    measure: str
    rule_version: str
    cycle_start: str
    trigger_event_seq: int
    trigger_occurred_at: str
    threshold: int
    total: int
    dedupe_key: str
    active: bool
    resolve: str | None        # None=仍成立；reversed=撤销/变更后回落；cleared=学习消分；reset=周期清零

    def as_dict(self) -> dict[str, Any]:
        return {
            "measure": self.measure,
            "rule_version": self.rule_version,
            "cycle_start": self.cycle_start,
            "trigger_event_seq": self.trigger_event_seq,
            "trigger_occurred_at": self.trigger_occurred_at,
            "threshold": self.threshold,
            "total": self.total,
            "dedupe_key": self.dedupe_key,
            "active": self.active,
            "resolve": self.resolve,
        }


@dataclass(frozen=True, slots=True)
class DueAction:
    """截至某业务时钟时刻已经成立、等待执行（幂等）的措施。"""

    dedupe_key: str
    measure: str
    cycle_start: str
    rule_version: str
    trigger_event_seq: int
    threshold: int
    total: int
    due_at: str
    suspend_days: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dedupe_key": self.dedupe_key,
            "measure": self.measure,
            "cycle_start": self.cycle_start,
            "rule_version": self.rule_version,
            "trigger_event_seq": self.trigger_event_seq,
            "threshold": self.threshold,
            "total": self.total,
            "due_at": self.due_at,
            "suspend_days": self.suspend_days,
        }


def _dedupe_for_crossing(cycle_start: str, measure: str, rule_version: str,
                         trigger_event_seq: int) -> str:
    return f"{cycle_start}:{measure}:{rule_version}:{trigger_event_seq}"


def recompute(
    events: Iterable[LedgerEvent],
    driver: DriverProfile,
    *,
    as_of: datetime,
    rulebook: RuleBook | None = None,
) -> dict[str, Any]:
    """对追加式事件流做确定性重算。

    重算只读事件、不写库、不执行措施：任意回溯登记或撤销之后再次调用，
    得到的跨越事实与待办措施完全一致；是否真正下发措施由物化层依据
    稳定的 dedupe_key 去重决定。
    """
    rulebook = rulebook or RuleBook()
    ordered = sorted(events, key=lambda item: item.seq)
    as_of = as_of.astimezone(timezone.utc)

    cycles: dict[str, _Cycle] = {}
    postings: list[Posting] = []
    crossings: list[Crossing] = []
    crossing_by_key: dict[tuple[str, str], Crossing] = {}

    # 仍在余额内的违法事实：撤销后重新处罚可以再次记分，
    # 而同一违法的重复决定在原决定有效期间被抑制。
    active_violations: set[str] = set()
    added_by_seq: dict[int, LedgerEvent] = {}

    def bucket(name: str) -> _Cycle:
        return cycles.setdefault(name, _Cycle())

    def resolve_crossings(name: str, how: str) -> None:
        balance = cycles[name].closing
        for key, crossing in list(crossing_by_key.items()):
            cycle_name, _measure = key
            if cycle_name != name or not crossing.active:
                continue
            if balance < crossing.threshold:
                updated = Crossing(
                    crossing.measure, crossing.rule_version, crossing.cycle_start,
                    crossing.trigger_event_seq, crossing.trigger_occurred_at,
                    crossing.threshold, crossing.total, crossing.dedupe_key,
                    False, how,
                )
                # 当前档位置为不活跃并让出键：余额再次达到阈值时开启新的一轮，
                # 旧跨越仍保留在 crossings 中作为形成过程，措施也不会重复执行。
                crossing_by_key.pop(key)
                for index, item in enumerate(crossings):
                    if item.dedupe_key == updated.dedupe_key:
                        crossings[index] = updated

    for event in ordered:
        c = bucket(event.cycle_start)
        if event.kind == EVENT_PENALTY_ADDED:
            added_by_seq[event.seq] = event
            duplicate = bool(event.violation_id and event.violation_id in active_violations)
            if duplicate:
                # 重复决定：不进入余额，事件本身仍保留作为痕迹
                postings.append(Posting(event.seq, event.kind, 0, False, c.closing,
                                        event.cycle_start, "duplicate_decision"))
                continue
            if event.violation_id:
                active_violations.add(event.violation_id)
            c.added += event.points
            c.closing += event.points
            postings.append(Posting(event.seq, event.kind, event.points, True, c.closing,
                                    event.cycle_start, event.reason))
            rule = rulebook.for_incident(parse_event_time(event.occurred_at))
            for measure in MEASURE_ORDER:
                threshold = rule.thresholds.get(measure)
                if threshold is None or c.closing < threshold:
                    continue
                key = (event.cycle_start, measure)
                if key in crossing_by_key:
                    # 同一轮跨越期间（余额始终高于阈值）不重复触发
                    continue
                dedupe = _dedupe_for_crossing(event.cycle_start, measure,
                                              rule.version, event.seq)
                crossing = Crossing(
                    measure=measure,
                    rule_version=rule.version,
                    cycle_start=event.cycle_start,
                    trigger_event_seq=event.seq,
                    trigger_occurred_at=event.occurred_at,
                    threshold=threshold,
                    total=c.closing,
                    dedupe_key=dedupe,
                    active=True,
                    resolve=None,
                )
                crossings.append(crossing)
                crossing_by_key[key] = crossing
        elif event.kind == EVENT_PENALTY_REMOVED:
            target = added_by_seq.get(event.ref_event_seq) if event.ref_event_seq else None
            if target is None and event.penalty_id:
                target = next((item for item in ordered
                               if item.kind == EVENT_PENALTY_ADDED and item.penalty_id == event.penalty_id), None)
            effective = target is not None and _posting_effective(target, postings)
            points = target.points if target is not None else event.points
            if effective:
                if target.violation_id:
                    active_violations.discard(target.violation_id)
                c.removed += points
                c.closing -= points
            postings.append(Posting(event.seq, event.kind, -points if effective else 0, effective,
                                    c.closing, event.cycle_start, event.reason))
            if effective:
                resolve_crossings(event.cycle_start, "reversed")
        elif event.kind == EVENT_POINTS_CLEARED:
            clear_points = min(event.points, c.closing)
            c.cleared += clear_points
            c.closing -= clear_points
            postings.append(Posting(event.seq, event.kind, -clear_points, clear_points > 0,
                                    c.closing, event.cycle_start, event.reason))
            resolve_crossings(event.cycle_start, "cleared")
        elif event.kind == EVENT_CYCLE_RESET:
            reset_total = c.closing
            c.carried_reset += reset_total
            c.closing = 0
            postings.append(Posting(event.seq, event.kind, -reset_total, reset_total > 0,
                                    c.closing, event.cycle_start, "cycle_boundary_reset"))
            resolve_crossings(event.cycle_start, "reset")
        else:  # pragma: no cover - 未知事件类型直接报错而不是静默
            raise ValueError(f"未知记分事件类型: {event.kind}")

    current_cycle = cycle_start_for(as_of, driver.license_issued_on).isoformat()
    current_points = cycles[current_cycle].closing if current_cycle in cycles else 0

    due = _due_actions(crossings, as_of, rulebook)
    upcoming = _upcoming(cycles.get(current_cycle), current_cycle, as_of, rulebook, ordered)

    return {
        "driver_id": driver.driver_id,
        "as_of": utc_text(as_of),
        "current_cycle_start": current_cycle,
        "current_points": current_points,
        "cycles": [
            {
                "cycle_start": name,
                "opening": cycles[name].opening,
                "added": cycles[name].added,
                "removed": cycles[name].removed,
                "cleared": cycles[name].cleared,
                "carried_reset": cycles[name].carried_reset,
                "closing": cycles[name].closing,
            }
            for name in sorted(cycles)
        ],
        "postings": [item.as_dict() for item in postings],
        "crossings": [item.as_dict() for item in sorted(crossings, key=lambda item: item.trigger_event_seq)],
        "due_actions": [item.as_dict() for item in due],
        "upcoming": upcoming,
    }


def _posting_effective(added: LedgerEvent, postings: Sequence[Posting]) -> bool:
    return any(item.seq == added.seq and item.effective for item in postings)


def _due_actions(
    crossings: Sequence[Crossing],
    as_of: datetime,
    rulebook: RuleBook,
) -> list[DueAction]:
    due: list[DueAction] = []
    rules_by_version = {rule.version: rule for rule in rulebook.versions()}
    for crossing in crossings:
        if not crossing.active:
            continue
        trigger_at = parse_event_time(crossing.trigger_occurred_at)
        if trigger_at > as_of:
            continue
        rule = rules_by_version.get(crossing.rule_version)
        due.append(DueAction(
            dedupe_key=crossing.dedupe_key,
            measure=crossing.measure,
            cycle_start=crossing.cycle_start,
            rule_version=crossing.rule_version,
            trigger_event_seq=crossing.trigger_event_seq,
            threshold=crossing.threshold,
            total=crossing.total,
            due_at=crossing.trigger_occurred_at,
            suspend_days=rule.suspend_days if rule and crossing.measure == MEASURE_SUSPEND else None,
        ))
    due.sort(key=lambda item: (item.due_at, item.measure))
    return due


def _upcoming(
    current: _Cycle | None,
    current_cycle: str,
    as_of: datetime,
    rulebook: RuleBook,
    ordered: Sequence[LedgerEvent],
) -> list[dict[str, Any]]:
    """按当前余额给出距下一措施的差距（预警，不产生任何后果）。"""
    balance = current.closing if current is not None else 0
    try:
        rule = rulebook.for_day(as_of.date())
    except LookupError:
        return []
    result: list[dict[str, Any]] = []
    for measure in MEASURE_ORDER:
        threshold = rule.thresholds.get(measure)
        if threshold is None or balance >= threshold:
            continue
        result.append({
            "measure": measure,
            "threshold": threshold,
            "points_needed": threshold - balance,
            "cycle_start": current_cycle,
        })
    # 同一阈值只预警一次（full_study 与 suspend 同为 12 分时合并为暂扣档）
    merged: dict[int, dict[str, Any]] = {}
    for item in result:
        merged.setdefault(item["threshold"], item)
    return [merged[key] for key in sorted(merged)]
