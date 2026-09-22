"""Wykonanie planu synchronizacji z dziennikiem operacji.

Dziennik (runs/<czas>.jsonl) powstaje przed pierwszą operacją i dostaje wpis po każdej
operacji. Brak wpisu końcowego oznacza przerwaną synchronizację — aplikacja ostrzega o tym
przy następnym uruchomieniu. Kalendarz nie wymaga „naprawiania”: kolejny `sync` liczy plan
od nowa na podstawie faktycznego stanu w Google i dokańcza brakujące operacje.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gcalsync.core.diff import SyncPlan
from gcalsync.gcal.client import CalendarApi, GoogleApiError
from gcalsync.gcal.mapping import event_times

MIN_INTERVAL_SECONDS = 0.2  # ok. 5 zapytań/s — daleko poniżej 600/min na użytkownika
MAX_CONSECUTIVE_FAILURES = 3

# Pola wysyłane przy zmianie; przypomnień nie ruszamy (mogłeś je zmienić ręcznie).
PATCH_FIELDS = ("summary", "location", "description", "start", "end", "extendedProperties")


@dataclass
class Operation:
    kind: str  # "add" | "update" | "delete"
    label: str
    body: dict[str, Any] | None = None
    event_id: str | None = None


@dataclass
class OperationFailure:
    operation: Operation
    error: str


@dataclass
class ExecutionResult:
    total: int
    done: int = 0
    failures: list[OperationFailure] = field(default_factory=list)
    aborted: str | None = None  # powód przerwania wszystkich pozostałych operacji

    @property
    def not_attempted(self) -> int:
        return self.total - self.done - len(self.failures)


def _label(resource: dict[str, Any]) -> str:
    times = event_times(resource)
    when = f"{times[0]:%Y-%m-%d %H:%M}" if times else "?"
    return f"{when} {resource.get('summary', '')}".strip()


def operations_for(plan: SyncPlan) -> list[Operation]:
    """Kolejność: dodania, zmiany, usunięcia — przerwanie zostawia co najwyżej nadmiar."""
    ops = [Operation("add", _label(a.body), body=a.body) for a in plan.adds]
    for u in plan.updates:
        patch = {f: u.body[f] for f in PATCH_FIELDS if f in u.body}
        patch.setdefault("location", "")  # wyczyszczenie sali, jeśli zniknęła z planu
        ops.append(Operation("update", _label(u.body), body=patch, event_id=u.existing["id"]))
    ops += [
        Operation("delete", _label(d.existing), event_id=d.existing["id"]) for d in plan.deletes
    ]
    return ops


# --- dziennik -----------------------------------------------------------------------------


class Journal:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, runs_dir: Path) -> Journal:
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return cls(runs_dir / f"{stamp}.jsonl")

    def write(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


@dataclass
class RunSummary:
    path: Path
    started: str
    calendar_id: str
    planned: int
    done: int
    failed: int
    finished: bool
    aborted: str | None


def read_run(path: Path) -> RunSummary | None:
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return None
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            break  # urwany ostatni wiersz po twardym przerwaniu
    if not records or records[0].get("type") != "start":
        return None
    start = records[0]
    ops = [r for r in records if r.get("type") == "op"]
    end = next((r for r in records if r.get("type") == "end"), None)
    return RunSummary(
        path=path,
        started=start.get("time", ""),
        calendar_id=start.get("calendar_id", ""),
        planned=len(start.get("operations", [])),
        done=sum(r.get("status") == "ok" for r in ops),
        failed=sum(r.get("status") == "error" for r in ops),
        finished=end is not None,
        aborted=end.get("aborted") if end else None,
    )


def last_run(runs_dir: Path) -> RunSummary | None:
    if not runs_dir.is_dir():
        return None
    runs = sorted(runs_dir.glob("*.jsonl"))
    return read_run(runs[-1]) if runs else None


def run_warning(summary: RunSummary | None) -> str | None:
    """Ostrzeżenie o ostatniej synchronizacji, jeśli nie zakończyła się w pełni."""
    if summary is None:
        return None
    when = datetime.fromisoformat(summary.started).astimezone().strftime("%Y-%m-%d %H:%M")
    if not summary.finished:
        return (
            f"Poprzednia synchronizacja ({when}) została przerwana: wykonano {summary.done} "
            f"z {summary.planned} operacji. Kalendarz może być w stanie pośrednim — "
            "uruchom `gcalsync sync`, aby zobaczyć, co zostało, i dokończ przez --apply."
        )
    if summary.failed or summary.aborted:
        return (
            f"Poprzednia synchronizacja ({when}) zakończyła się z błędami: "
            f"{summary.failed} nieudanych operacji"
            + (f", przerwano: {summary.aborted}" if summary.aborted else "")
            + f". Szczegóły: {summary.path}. Uruchom `gcalsync sync`, aby zobaczyć, co zostało."
        )
    return None


# --- wykonanie ----------------------------------------------------------------------------


def execute_plan(
    api: CalendarApi,
    calendar_id: str,
    plan: SyncPlan,
    journal: Journal,
    progress: Callable[[int, int, Operation, str | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> ExecutionResult:
    ops = operations_for(plan)
    result = ExecutionResult(total=len(ops))
    journal.write(
        {
            "type": "start",
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "calendar_id": calendar_id,
            "operations": [{"kind": o.kind, "label": o.label, "event_id": o.event_id} for o in ops],
        }
    )

    consecutive_failures = 0
    for index, op in enumerate(ops):
        if index:
            sleep(min_interval)
        error: str | None = None
        record: dict[str, Any] = {"type": "op", "index": index, "kind": op.kind}
        try:
            if op.kind == "add":
                created = api.insert_event(calendar_id, op.body)
                record["event_id"] = created.get("id")
            elif op.kind == "update":
                api.patch_event(calendar_id, op.event_id, op.body)
                record["event_id"] = op.event_id
            else:
                api.delete_event(calendar_id, op.event_id)
                record["event_id"] = op.event_id
        except GoogleApiError as exc:
            error = str(exc)
            if exc.status == 401:
                result.aborted = "sesja Google wygasła"
        record["status"] = "error" if error else "ok"
        if error:
            record["error"] = error
            result.failures.append(OperationFailure(op, error))
            consecutive_failures += 1
        else:
            result.done += 1
            consecutive_failures = 0
        journal.write(record)
        if progress:
            progress(index + 1, len(ops), op, error)

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not result.aborted:
            result.aborted = f"{MAX_CONSECUTIVE_FAILURES} kolejne operacje nieudane"
        if result.aborted:
            break

    journal.write(
        {
            "type": "end",
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "done": result.done,
            "failed": len(result.failures),
            "aborted": result.aborted,
        }
    )
    return result


def verify(plan_after: SyncPlan) -> list[str]:
    """Opis rozbieżności, które zostały po synchronizacji (pusta lista = kalendarz zgodny)."""
    issues = [f"nadal do dodania: {_label(a.body)}" for a in plan_after.adds]
    for u in plan_after.updates:
        fields = ", ".join(c.field for c in u.changes)
        issues.append(f"nadal różni się ({fields}): {_label(u.body)}")
        # Diagnoza na wypadek, gdyby Google przerabiał któreś pole (np. opis).
        for c in u.changes:
            if c.field == "description":
                issues.append(f"    Google: {c.old!r}\n    oczekiwano: {c.new!r}")
    issues += [f"nadal do usunięcia: {_label(d.existing)}" for d in plan_after.deletes]
    return issues
