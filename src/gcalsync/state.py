"""Stan synchronizacji przechowywany w samym kalendarzu (zajęcia usunięte ręcznie).

Google nie gwarantuje dostępu do usuniętych zdarzeń (ani ich znaczników), więc gcalsync
pamięta sam:
- `seen` — zajęcia obecne w kalendarzu po ostatnim zapisie; jeśli któregoś z nich teraz
  brakuje, a plan nadal je zawiera, usunął je człowiek;
- `deleted` — zajęcia usunięte ręcznie; nie są dodawane ponownie, dopóki nie minie ich
  termin (albo do przywrócenia opcją --restore-deleted).

Stan leży w jednym technicznym zdarzeniu całodniowym z 1 stycznia 2000 r. w kalendarzu
„Plan WAT” (znaczniki w extendedProperties.private: do 300 właściwości, klucz ≤ 44 znaki,
wartość ≤ 1024 znaki, łącznie ≤ 32 kB — zweryfikowane w dokumentacji Calendar API).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from gcalsync.core.normalize import WARSAW
from gcalsync.gcal.client import CalendarApi
from gcalsync.gcal.mapping import event_times, is_managed, managed_key, private_props

PROP_STATE = "gcalsync_state"
KEY_LENGTH = 16  # skrócony klucz zajęć (prefiks sha256) — kolizje pomijalne
VALUE_LIMIT = 1000  # zapas poniżej limitu 1024 znaków wartości
STATE_DATE = "2000-01-01"
STATE_SUMMARY = "gcalsync — dane techniczne (nie usuwaj)"
STATE_DESCRIPTION = (
    "To zdarzenie przechowuje listę zajęć usuniętych ręcznie z kalendarza „Plan WAT”, "
    "żeby gcalsync nie dodawał ich ponownie. Usunięcie go przywróci wszystkie usunięte "
    "zajęcia przy następnej synchronizacji."
)


def short_key(key: str) -> str:
    return key[:KEY_LENGTH]


def is_state_event(google_event: dict[str, Any]) -> bool:
    return private_props(google_event).get(PROP_STATE) == "1"


@dataclass
class SyncState:
    event: dict[str, Any] | None = None  # zdarzenie ze stanem, jeśli już istnieje
    seen: set[str] = field(default_factory=set)
    deleted: dict[str, str] = field(default_factory=dict)  # klucz -> data zajęć (RRRRMMDD)
    duplicates: list[dict[str, Any]] = field(default_factory=list)  # nadmiarowe zdarzenia stanu

    def properties(self) -> dict[str, str]:
        props = {PROP_STATE: "1"}
        props |= _pack("seen", sorted(self.seen))
        props |= _pack("del", [f"{k}:{d}" for k, d in sorted(self.deleted.items())])
        return props


def _pack(prefix: str, items: list[str]) -> dict[str, str]:
    """Rozkłada listę na właściwości prefix0, prefix1, … o wartościach ≤ VALUE_LIMIT znaków."""
    chunks: list[list[str]] = [[]]
    size = 0
    for item in items:
        if chunks[-1] and size + len(item) + 1 > VALUE_LIMIT:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item) + 1
    return {f"{prefix}{i}": ",".join(chunk) for i, chunk in enumerate(chunks) if chunk}


def _unpack(props: dict[str, str], prefix: str) -> list[str]:
    items: list[str] = []
    for name, value in props.items():
        if name.startswith(prefix) and name[len(prefix) :].isdigit() and value:
            items += value.split(",")
    return items


def split_state(events: list[dict[str, Any]]) -> tuple[SyncState, list[dict[str, Any]]]:
    """Wyodrębnia stan z listy zdarzeń kalendarza; zwraca stan i pozostałe zdarzenia."""
    state_events = [e for e in events if is_state_event(e)]
    others = [e for e in events if not is_state_event(e)]
    state = SyncState()
    if state_events:
        # Więcej niż jedno zdarzenie stanu nie powinno się zdarzyć; łączymy, by nic nie zgubić.
        state.event, state.duplicates = state_events[0], state_events[1:]
        for e in state_events:
            props = private_props(e)
            state.seen |= set(_unpack(props, "seen"))
            for item in _unpack(props, "del"):
                key, _, day = item.partition(":")
                state.deleted[key] = day
    return state, others


def present_keys(events: Iterable[dict[str, Any]], now: datetime) -> set[str]:
    """Skrócone klucze zarządzanych zajęć, które jeszcze się nie zakończyły."""
    keys = set()
    for e in events:
        times = event_times(e)
        key = managed_key(e)
        if is_managed(e) and key and times and times[1] > now:
            keys.add(short_key(key))
    return keys


def next_state(
    state: SyncState,
    events_after: list[dict[str, Any]],
    newly_deleted: Iterable[tuple[str, datetime]],
    now: datetime,
    *,
    restore: bool = False,
) -> SyncState:
    """Stan po zapisie: obecne zajęcia + dotychczasowe i nowo wykryte usunięcia (bez minionych)."""
    today = now.astimezone(WARSAW).strftime("%Y%m%d")
    deleted = {} if restore else dict(state.deleted)
    for key, end in newly_deleted:
        deleted[short_key(key)] = end.astimezone(WARSAW).strftime("%Y%m%d")
    deleted = {k: d for k, d in deleted.items() if d >= today}
    return SyncState(state.event, present_keys(events_after, now), deleted, state.duplicates)


def state_body(state: SyncState) -> dict[str, Any]:
    return {
        "summary": STATE_SUMMARY,
        "description": STATE_DESCRIPTION,
        "start": {"date": STATE_DATE},
        "end": {"date": date.fromisoformat(STATE_DATE).replace(day=2).isoformat()},
        "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": state.properties()},
    }


def save_state(api: CalendarApi, calendar_id: str, old: SyncState, new: SyncState) -> bool:
    """Zapisuje stan, jeśli się zmienił. Zwraca True, gdy był zapis."""
    unchanged = old.event is not None and private_props(old.event) == new.properties()
    if unchanged and not old.duplicates:
        return False
    body = state_body(new)
    if old.event is None:
        api.insert_event(calendar_id, body)
    else:
        api.update_event(calendar_id, old.event["id"], body)
    for extra in old.duplicates:
        api.delete_event(calendar_id, extra["id"])
    return True
