from datetime import UTC, datetime

from fake_calendar import FakeCalendarApi

from gcalsync.state import (
    VALUE_LIMIT,
    SyncState,
    next_state,
    save_state,
    split_state,
    state_body,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def managed(key, start, end):
    return {
        "id": key,
        "summary": "x",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "extendedProperties": {"private": {"gcalsync_managed": "1", "gcalsync_key": key}},
    }


def test_roundtrip_through_calendar_event():
    api = FakeCalendarApi()
    cal = api.create_calendar("Plan WAT", "Europe/Warsaw", "")["id"]
    state = SyncState(seen={"a" * 16, "b" * 16}, deleted={"c" * 16: "20261201"})
    assert save_state(api, cal, SyncState(), state)
    loaded, others = split_state(api.list_events(cal))
    assert others == []
    assert loaded.seen == state.seen and loaded.deleted == state.deleted
    assert not save_state(api, cal, loaded, SyncState(loaded.event, state.seen, state.deleted))


def test_many_keys_fit_in_property_limits():
    state = SyncState(
        seen={f"{i:016x}" for i in range(500)},
        deleted={f"{i:016x}": "20261201" for i in range(300)},
    )
    props = state_body(state)["extendedProperties"]["private"]
    assert all(len(v) <= VALUE_LIMIT for v in props.values())
    assert all(len(k) <= 44 for k in props)
    assert sum(len(k) + len(v) for k, v in props.items()) < 32_000
    loaded, _ = split_state([{"id": "s", **state_body(state)}])
    assert loaded.seen == state.seen and loaded.deleted == state.deleted


def test_next_state_tracks_present_classes_and_prunes_past_deletions():
    events = [
        managed("1" * 32, "2026-10-20T08:00:00+02:00", "2026-10-20T09:35:00+02:00"),
        managed("2" * 32, "2026-10-01T08:00:00+02:00", "2026-10-01T09:35:00+02:00"),  # minione
        {"id": "own", "start": {"dateTime": "2026-10-20T08:00:00+02:00"}},  # niezarządzane
    ]
    old = SyncState(deleted={"9" * 16: "20261001", "8" * 16: "20261220"})
    new_deleted_end = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    new = next_state(old, events, [("7" * 32, new_deleted_end)], NOW)
    assert new.seen == {"1" * 16}
    assert new.deleted == {"8" * 16: "20261220", "7" * 16: "20261105"}
    assert next_state(old, events, [], NOW, restore=True).deleted == {}
