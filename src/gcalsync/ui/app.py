"""GUI w przeglądarce (NiceGUI), dostępne tylko lokalnie pod http://127.0.0.1:<port>.

Uruchomienie: `uv run gcalsync ui` (albo start.bat). Ekrany odpowiadają krokom z planu:
Źródła → Reguły → Podgląd → Kalendarz → Synchronizacja. Cała logika jest w Controller;
tutaj tylko widoki i obsługa zdarzeń.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from nicegui import run, ui

from gcalsync.app import PlanChangedError
from gcalsync.core.merge import ConflictPolicy
from gcalsync.core.normalize import WARSAW
from gcalsync.core.rules import FIELD_LABELS, OPERATOR_LABELS
from gcalsync.gcal.mapping import FIELD_LABELS as GOOGLE_FIELD_LABELS
from gcalsync.gcal.mapping import display_time, event_times, render_title
from gcalsync.model import Event
from gcalsync.report import format_issue
from gcalsync.storage import DEFAULT_CALENDAR_NAME, ConfigError, Paths
from gcalsync.ui.controller import ENCODINGS, USER_ERRORS, Controller

WEEKDAYS = ("pn", "wt", "śr", "cz", "pt", "so", "nd")
POLICY_LABELS = {
    ConflictPolicy.PRIORITY.value: "ważniejsze źródło wygrywa (zalecane)",
    ConflictPolicy.KEEP_ALL.value: "zostaw wszystko, tylko ostrzegaj",
}
DEFAULT_PORT = 8765
AUTO = "auto"  # wartość w polu wyboru kodowania oznaczająca wykrywanie automatyczne


# --- formatowanie -------------------------------------------------------------------------


def _day(moment: datetime) -> str:
    moment = moment.astimezone(WARSAW)
    return f"{moment:%Y-%m-%d} ({WEEKDAYS[moment.weekday()]})"


def _hours(start: datetime, end: datetime) -> str:
    start, end = start.astimezone(WARSAW), end.astimezone(WARSAW)
    offset = start.strftime("%z")
    return f"{start:%H:%M}–{end:%H:%M} {offset[:3]}:{offset[3:]}"


def _event_row(event: Event, ctrl: Controller, **extra: Any) -> dict[str, Any]:
    preview = ctrl.preview
    return {
        "id": f"{event.source_id}:{event.row}",
        "sort": event.start.isoformat(),
        "day": _day(event.start),
        "hours": _hours(event.start, event.end),
        "title": render_title(event, ctrl.config.title_template),
        "subject": event.subject_raw,
        "location": event.location,
        "source": preview.source_name(event.source_id) if preview else event.source_id,
        **extra,
    }


def _google_row(resource: dict[str, Any], row_id: str, **extra: Any) -> dict[str, Any]:
    times = event_times(resource)
    return {
        "id": row_id,
        "sort": times[0].isoformat() if times else "",
        "day": _day(times[0]) if times else "?",
        "hours": _hours(*times) if times else "?",
        "title": resource.get("summary", ""),
        "location": resource.get("location", ""),
        **extra,
    }


def _col(name: str, label: str, **kwargs: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "field": name, "align": "left", **kwargs}


EVENT_COLUMNS = [
    _col("day", "Dzień", sortable=True, sort_field="sort"),
    _col("hours", "Godziny"),
    _col("title", "Tytuł w kalendarzu", sortable=True),
    _col("subject", "Temat w planie"),
    _col("location", "Sala"),
    _col("source", "Źródło", sortable=True),
]


def _table(columns: list[dict], rows: list[dict], *, filterable: bool = True) -> None:
    if not rows:
        ui.label("(brak)").classes("text-grey-7 q-pa-sm")
        return
    if filterable and len(rows) > 10:
        search = ui.input(placeholder="Szukaj…").props("dense clearable").classes("w-64")
    table = ui.table(
        columns=columns, rows=rows, row_key="id", pagination={"rowsPerPage": 25}
    ).classes("w-full")
    table.props("dense flat bordered wrap-cells")
    if filterable and len(rows) > 10:
        search.bind_value(table, "filter")


def _stat(label: str, value: int | str, color: str = "primary") -> None:
    with ui.card().tight().classes("q-pa-sm min-w-[110px] items-center"):
        ui.label(str(value)).classes(f"text-h5 text-{color}")
        ui.label(label).classes("text-caption text-grey-8")


# --- aplikacja ----------------------------------------------------------------------------


def build_root(ctrl: Controller) -> Callable[[], None]:
    """Zwraca funkcję strony głównej (każda karta przeglądarki dostaje własne widoki)."""

    def root() -> None:
        ui.query("body").classes("bg-grey-1")
        views: list[Any] = []

        def refresh_all() -> None:
            for view in views:
                view.refresh()

        # Stały element strony: komunikaty z obsługi zdarzeń trafiają tu, nawet jeśli przycisk,
        # który je wywołał, zniknął po odświeżeniu widoku.
        anchor = ui.element()

        def notify(*args: Any, **kwargs: Any) -> None:
            with anchor:
                ui.notify(*args, **kwargs)

        def error(exc: BaseException) -> None:
            notify(str(exc), type="negative", multi_line=True, close_button="OK", timeout=0)

        def action(fn: Callable[..., Any], success: str | None = None) -> Callable[..., None]:
            """Opakowanie szybkiej akcji: błąd użytkownika -> komunikat, sukces -> odświeżenie."""

            def handler(*args: Any) -> None:
                try:
                    fn(*args)
                except USER_ERRORS as exc:
                    error(exc)
                    return
                if success:
                    notify(success, type="positive")
                refresh_all()

            return handler

        async def background(fn: Callable[..., Any], *args: Any, busy: str) -> Any:
            """Operacja sieciowa w osobnym wątku, z komunikatem „w toku”."""
            with anchor:
                note = ui.notification(busy, type="ongoing", spinner=True, timeout=None)
            try:
                return await run.io_bound(fn, *args)
            except USER_ERRORS as exc:
                error(exc)
                return None
            finally:
                note.dismiss()

        # --- nagłówek i ostrzeżenia ---

        with ui.header(elevated=True).classes("items-center bg-primary"):
            ui.icon("event", size="md")
            ui.label("gcalsync — plan zajęć WAT → Google Calendar").classes("text-h6")
            ui.space()
            ui.label(f"Dane: {ctrl.paths.root}").classes("text-caption opacity-80")

        @ui.refreshable
        def banner() -> None:
            warning = ctrl.run_warning()
            if warning:
                with (
                    ui.card().classes("w-full bg-orange-1 q-mb-sm"),
                    ui.row().classes("items-center no-wrap"),
                ):
                    ui.icon("warning", color="orange-9", size="md")
                    ui.label(warning.replace("`gcalsync sync`", "zakładkę Synchronizacja"))
            if ctrl.preview_error:
                with ui.card().classes("w-full bg-red-1 q-mb-sm"):
                    ui.label(ctrl.preview_error).classes("text-negative")

        views.append(banner)
        banner()

        with ui.tabs().classes("w-full bg-white text-primary shadow-1") as tabs:
            t_sources = ui.tab("sources", label="1. Źródła", icon="upload_file")
            t_rules = ui.tab("rules", label="2. Reguły", icon="filter_alt")
            t_preview = ui.tab("preview", label="3. Podgląd", icon="visibility")
            t_calendar = ui.tab("calendar", label="4. Kalendarz", icon="calendar_month")
            t_sync = ui.tab("sync", label="5. Synchronizacja", icon="sync")

        # --- 1. Źródła ---

        async def ask_source_name(filename: str) -> dict[str, str] | None:
            existing = [s.name for s in ctrl.config.sources]
            with ui.dialog() as dialog, ui.card().classes("min-w-[420px]"):
                ui.label(f"Plik: {filename}").classes("text-subtitle1")
                mode = ui.toggle(
                    {"new": "Nowe źródło", "replace": "Podmień plik istniejącego"},
                    value="new",
                ).props("no-caps")
                name = ui.input("Nazwa źródła", value=ctrl.suggest_source_name()).classes("w-full")
                target = ui.select(existing, label="Które źródło podmienić?").classes("w-full")
                name.bind_visibility_from(mode, "value", value="new")
                target.bind_visibility_from(mode, "value", value="replace")
                if not existing:
                    mode.set_visibility(False)
                with ui.row().classes("w-full justify-end"):
                    ui.button("Anuluj", on_click=lambda: dialog.submit(None)).props("flat")
                    ui.button(
                        "Zapisz",
                        on_click=lambda: dialog.submit(
                            {"mode": mode.value, "name": name.value, "target": target.value}
                        ),
                    )
            return await dialog

        async def handle_upload(e: Any) -> None:
            files = getattr(e, "files", None) or [e.file]
            for file in files:
                data = await file.read()
                answer = await ask_source_name(file.name)
                if not answer:
                    continue
                try:
                    if answer["mode"] == "replace":
                        if not answer["target"]:
                            raise ConfigError("Wybierz źródło do podmiany.")
                        ctrl.replace_source(answer["target"], data, file.name)
                        notify(f"Podmieniono plik źródła „{answer['target']}”.", type="positive")
                    else:
                        source = ctrl.add_source(data, file.name, answer["name"])
                        notify(f"Dodano źródło „{source.name}”.", type="positive")
                except USER_ERRORS as exc:
                    error(exc)
            uploader.reset()
            refresh_all()

        async def rename(name: str) -> None:
            with ui.dialog() as dialog, ui.card():
                new = ui.input("Nowa nazwa", value=name)
                with ui.row():
                    ui.button("Anuluj", on_click=lambda: dialog.submit(None)).props("flat")
                    ui.button("Zmień", on_click=lambda: dialog.submit(new.value))
            new_name = await dialog
            if new_name and new_name != name:
                action(ctrl.rename_source)(name, new_name)

        async def remove(name: str) -> None:
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Usunąć źródło „{name}” i kopię jego pliku?")
                ui.label(
                    "Zdarzenia w Google zostaną usunięte dopiero przy synchronizacji, "
                    "po Twoim potwierdzeniu."
                ).classes("text-caption")
                with ui.row():
                    ui.button("Anuluj", on_click=lambda: dialog.submit(False)).props("flat")
                    ui.button("Usuń", color="negative", on_click=lambda: dialog.submit(True))
            if await dialog:
                try:
                    dropped = ctrl.remove_source(name)
                except USER_ERRORS as exc:
                    error(exc)
                    return
                for rule in dropped:
                    notify(f"Usunięto regułę tylko dla tego źródła: {rule.describe()}")
                refresh_all()

        @ui.refreshable
        def sources_view() -> None:
            ui.label(
                "Kolejność = priorytet: pierwsze źródło jest najważniejsze i wygrywa w konfliktach."
            ).classes("text-body2 text-grey-8")
            summaries = {s.id: s for s in ctrl.preview.sources} if ctrl.preview else {}
            if not ctrl.config.sources:
                ui.label("Nie ma jeszcze źródeł — wgraj pliki CSV z ewig poniżej.").classes(
                    "text-grey-7"
                )
            for index, source in enumerate(ctrl.config.sources):
                summary = summaries.get(source.id)
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center"):
                        ui.badge(str(index + 1)).classes("text-subtitle2")
                        ui.label(source.name).classes("text-h6")
                        ui.space()
                        ui.button(
                            icon="arrow_upward",
                            on_click=lambda n=source.name: action(ctrl.move_source)(n, -1),
                        ).props("flat round dense").tooltip("Wyżej (ważniejsze)").set_enabled(
                            index > 0
                        )
                        ui.button(
                            icon="arrow_downward",
                            on_click=lambda n=source.name: action(ctrl.move_source)(n, 1),
                        ).props("flat round dense").tooltip("Niżej").set_enabled(
                            index < len(ctrl.config.sources) - 1
                        )
                        ui.button(icon="edit", on_click=lambda n=source.name: rename(n)).props(
                            "flat round dense"
                        ).tooltip("Zmień nazwę")
                        ui.button(
                            icon="delete",
                            color="negative",
                            on_click=lambda n=source.name: remove(n),
                        ).props("flat round dense").tooltip("Usuń źródło")
                    added = datetime.fromisoformat(source.added).astimezone()
                    ui.label(
                        f"Plik: {source.original_filename} · wgrany {added:%Y-%m-%d %H:%M}"
                    ).classes("text-caption text-grey-8")
                    with ui.row().classes("items-center"):
                        ui.select(
                            {k or AUTO: v for k, v in ENCODINGS.items()},
                            value=source.encoding or AUTO,
                            label="Kodowanie",
                            on_change=lambda e, n=source.name: action(ctrl.set_source_encoding)(
                                n, None if e.value == AUTO else e.value
                            ),
                        ).classes("w-56")
                        if summary:
                            detected = f"użyte: {summary.encoding}" if summary.encoding else ""
                            ui.label(detected).classes("text-caption")
                    if summary:
                        info = f"Zdarzeń: {summary.event_count}"
                        if summary.first_start and summary.last_end:
                            info += (
                                f" · od {summary.first_start:%Y-%m-%d} "
                                f"do {summary.last_end:%Y-%m-%d}"
                            )
                        ui.label(info)
                        with ui.row().classes("gap-1"):
                            for label, count in sorted(summary.courses.items()):
                                ui.chip(f"{label}: {count}", color="blue-1", text_color="black")
                        for issue in summary.issues:
                            color = "text-negative" if issue.level == "error" else "text-orange-9"
                            ui.label(format_issue(issue, ctrl.preview)).classes(color)

        with ui.tab_panels(tabs, value=t_sources).classes("w-full bg-transparent"):
            with ui.tab_panel(t_sources).classes("gap-3"):
                views.append(sources_view)
                sources_view()
                uploader = (
                    ui.upload(
                        multiple=True,
                        auto_upload=True,
                        on_multi_upload=handle_upload,
                        label="Wgraj pliki CSV z ewig (przeciągnij tutaj lub kliknij +)",
                    )
                    .props('accept=".csv,.txt"')
                    .classes("w-full max-w-xl")
                )

            # --- 2. Reguły ---

            with ui.tab_panel(t_rules).classes("gap-3"):

                @ui.refreshable
                def rules_view() -> None:
                    names = ctrl.source_names()
                    hits = ctrl.rule_hits()
                    with ui.card().classes("w-full"):
                        ui.label("Reguły wykluczeń").classes("text-h6")
                        if not ctrl.config.rules:
                            ui.label("Brak reguł.").classes("text-grey-7")
                        for i, rule in enumerate(ctrl.config.rules):
                            with ui.row().classes("w-full items-center"):
                                ui.switch(
                                    value=rule.enabled,
                                    on_change=lambda e, i=i: action(ctrl.set_rule_enabled)(
                                        i, e.value
                                    ),
                                ).tooltip("Włączona / wyłączona")
                                ui.label(rule.describe(names)).classes("text-body1")
                                ui.badge(
                                    f"wyklucza: {hits[i] if i < len(hits) else 0}",
                                    color="grey-7",
                                )
                                ui.space()
                                ui.button(
                                    icon="delete",
                                    color="negative",
                                    on_click=lambda i=i: action(ctrl.remove_rule)(i),
                                ).props("flat round dense").tooltip("Usuń regułę")

                    with ui.card().classes("w-full"):
                        ui.label("Szybko: wyklucz przedmiot").classes("text-subtitle1")
                        with ui.row().classes("items-center"):
                            course = ui.select(
                                ctrl.courses(), label="Przedmiot z wgranych plików", with_input=True
                            ).classes("w-96")
                            ui.button(
                                "Wyklucz",
                                icon="block",
                                on_click=lambda: (
                                    action(ctrl.add_rule, "Dodano regułę.")(
                                        "course", "equals", course.value
                                    )
                                    if course.value
                                    else notify("Wybierz przedmiot.", type="warning")
                                ),
                            )

                    with ui.expansion("Reguła zaawansowana", icon="tune").classes(
                        "w-full bg-white"
                    ):
                        with ui.row().classes("items-end"):
                            field = ui.select(FIELD_LABELS, value="subject", label="Pole")
                            op = ui.select(OPERATOR_LABELS, value="contains", label="Warunek")
                            value = ui.input("Wartość").classes("w-72")
                            case = ui.checkbox("wielkość liter ma znaczenie")
                        with ui.row().classes("items-end"):
                            only = ui.select(
                                [s.name for s in ctrl.config.sources],
                                multiple=True,
                                label="Tylko źródła (puste = wszystkie)",
                            ).classes("w-80")
                            d_from = ui.input("Od dnia").props("type=date")
                            d_to = ui.input("Do dnia").props("type=date")

                        def add_advanced() -> None:
                            action(ctrl.add_rule, "Dodano regułę.")(
                                field.value,
                                op.value,
                                value.value or "",
                                case.value,
                                list(only.value or []),
                                date.fromisoformat(d_from.value) if d_from.value else None,
                                date.fromisoformat(d_to.value) if d_to.value else None,
                            )

                        ui.button("Dodaj regułę", icon="add", on_click=add_advanced)

                views.append(rules_view)
                rules_view()

            # --- 3. Podgląd ---

            with ui.tab_panel(t_preview).classes("gap-3"):

                @ui.refreshable
                def preview_view() -> None:
                    p = ctrl.preview
                    if p is None:
                        ui.label("Wgraj pliki w zakładce Źródła, aby zobaczyć podgląd.")
                        return
                    dropped = sum(len(d.dropped) for d in p.duplicates)
                    with ui.row():
                        _stat("zdarzeń w plikach", len(p.parsed))
                        _stat("wykluczone", len(p.excluded), "grey-8")
                        _stat("duplikaty", dropped, "grey-8")
                        _stat("odrzucone w konfliktach", len(p.conflicts), "orange-9")
                        _stat("do kalendarza", len(p.events), "positive")
                        if p.errors:
                            _stat("błędy", len(p.errors), "negative")
                    if p.coverage:
                        start, end = p.coverage
                        ui.label(
                            f"Okno pokrycia plików: {start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} "
                            "(synchronizacja nie zmienia zdarzeń poza nim)"
                        ).classes("text-caption")

                    with ui.tabs().props("dense align=left no-caps") as sub:
                        s_events = ui.tab("Do kalendarza")
                        s_excl = ui.tab("Wykluczone")
                        s_conf = ui.tab("Konflikty")
                        s_dup = ui.tab("Duplikaty")
                        s_over = ui.tab("Kolizje (ostrzeżenia)")
                        s_issues = ui.tab(f"Błędy i ostrzeżenia ({len(p.issues)})")
                    with ui.tab_panels(sub, value=s_events).classes("w-full"):
                        with ui.tab_panel(s_events):
                            _table(EVENT_COLUMNS, [_event_row(e, ctrl) for e in p.events])
                        with ui.tab_panel(s_excl):
                            names = p.source_names
                            _table(
                                [*EVENT_COLUMNS, _col("rule", "Reguła")],
                                [
                                    _event_row(x.event, ctrl, rule=x.rule.describe(names))
                                    for x in sorted(p.excluded, key=lambda x: x.event.start)
                                ],
                            )
                        with ui.tab_panel(s_conf):
                            _table(
                                [*EVENT_COLUMNS, _col("winner", "Przegrywa z")],
                                [
                                    _event_row(
                                        c.loser,
                                        ctrl,
                                        winner="; ".join(
                                            f"{w.subject_raw} [{p.source_name(w.source_id)}]"
                                            for w in c.winners
                                        ),
                                    )
                                    for c in p.conflicts
                                ],
                            )
                        with ui.tab_panel(s_dup):
                            _table(
                                [
                                    *EVENT_COLUMNS,
                                    _col("also", "Występuje też w"),
                                    _col("diff", "Rozbieżności"),
                                ],
                                [
                                    _event_row(
                                        d.kept,
                                        ctrl,
                                        also=", ".join(
                                            f"{p.source_name(e.source_id)} (wiersz {e.row})"
                                            for e in d.dropped
                                        ),
                                        diff="; ".join(x.describe(names) for x in d.differences),
                                    )
                                    for d in p.duplicates
                                ],
                            )
                        with ui.tab_panel(s_over):
                            _table(
                                [*EVENT_COLUMNS, _col("other", "Nakłada się na")],
                                [
                                    _event_row(
                                        o.first,
                                        ctrl,
                                        other=f"{o.second.subject_raw} "
                                        f"{_hours(o.second.start, o.second.end)}",
                                    )
                                    for o in p.overlaps
                                ],
                            )
                        with ui.tab_panel(s_issues):
                            if not p.issues:
                                ui.label("(brak)").classes("text-grey-7")
                            for issue in p.issues:
                                color = (
                                    "text-negative" if issue.level == "error" else "text-orange-9"
                                )
                                ui.label(format_issue(issue, p)).classes(color)

                views.append(preview_view)
                preview_view()

            # --- 4. Kalendarz i ustawienia ---

            with ui.tab_panel(t_calendar).classes("gap-3"):

                @ui.refreshable
                def calendar_view() -> None:
                    with ui.card().classes("w-full"):
                        ui.label("Konto Google").classes("text-h6")
                        status_label = ui.label("Sprawdź stan logowania przyciskiem obok.")
                        if not ctrl.paths.client_secret.exists():
                            status_label.set_text(
                                f"Brak pliku client_secret.json — skopiuj go do: "
                                f"{ctrl.paths.client_secret}"
                            )
                            status_label.classes("text-negative")

                        async def check() -> None:
                            status = await background(ctrl.google_status, busy="Sprawdzam token…")
                            if status:
                                status_label.set_text(status.message)

                        async def do_login() -> None:
                            notify(
                                "Otwieram kartę z ekranem zgody Google — wróć tu po zalogowaniu.",
                                type="info",
                            )
                            await background(ctrl.login, busy="Czekam na logowanie w przeglądarce…")
                            await check()

                        async def do_logout() -> None:
                            ctrl.logout()
                            await check()

                        with ui.row():
                            ui.button("Sprawdź", icon="refresh", on_click=check).props("outline")
                            ui.button("Zaloguj", icon="login", on_click=do_login)
                            ui.button("Wyloguj", icon="logout", on_click=do_logout).props("flat")

                    with ui.card().classes("w-full"):
                        ui.label("Kalendarz docelowy").classes("text-h6")
                        cal = ctrl.config.calendar
                        cal_label = ui.label(
                            f"„{cal.summary}” ({cal.id})" if cal else "Nie jest ustawiony."
                        )

                        async def check_calendar() -> None:
                            text = await background(ctrl.calendar_status, busy="Sprawdzam…")
                            if text:
                                cal_label.set_text(text)

                        async def create() -> None:
                            with ui.dialog() as dialog, ui.card():
                                ui.label("Utworzyć nowy, pusty kalendarz dodatkowy w Google?")
                                name = ui.input("Nazwa", value=DEFAULT_CALENDAR_NAME)
                                with ui.row():
                                    ui.button("Anuluj", on_click=lambda: dialog.submit(None)).props(
                                        "flat"
                                    )
                                    ui.button("Utwórz", on_click=lambda: dialog.submit(name.value))
                            chosen = await dialog
                            if chosen:
                                await background(ctrl.create_calendar, chosen, busy="Tworzę…")
                                refresh_all()

                        async def forget() -> None:
                            with ui.dialog() as dialog, ui.card():
                                ui.label(
                                    "Zapomnieć kalendarz docelowy? W Google nic nie zostanie "
                                    "usunięte."
                                )
                                with ui.row():
                                    ui.button(
                                        "Anuluj", on_click=lambda: dialog.submit(False)
                                    ).props("flat")
                                    ui.button("Zapomnij", on_click=lambda: dialog.submit(True))
                            if await dialog:
                                action(ctrl.forget_calendar)()

                        with ui.row():
                            if cal:
                                ui.button("Sprawdź", icon="refresh", on_click=check_calendar).props(
                                    "outline"
                                )
                                ui.button("Zapomnij", icon="link_off", on_click=forget).props(
                                    "flat"
                                )
                            else:
                                ui.button("Utwórz kalendarz", icon="add", on_click=create)

                    with ui.card().classes("w-full"):
                        ui.label("Ustawienia").classes("text-h6")
                        template = ui.input(
                            "Szablon tytułu", value=ctrl.config.title_template
                        ).classes("w-96")
                        ui.label(
                            "Pola: {course} przedmiot, {kind} typ, {seq} numer, {subject} temat, "
                            "{location} sala"
                        ).classes("text-caption")
                        example = ui.label().classes("text-body2")

                        def update_example() -> None:
                            try:
                                example.set_text(f"Przykład: {ctrl.title_example(template.value)}")
                                example.classes(remove="text-negative")
                            except USER_ERRORS as exc:
                                example.set_text(str(exc))
                                example.classes(add="text-negative")

                        template.on_value_change(update_example)
                        update_example()
                        policy = ui.select(
                            POLICY_LABELS, value=ctrl.config.policy.value, label="Konflikty"
                        ).classes("w-96")

                        def save_settings() -> None:
                            def apply() -> None:
                                ctrl.set_title_template(template.value)
                                ctrl.set_policy(policy.value)

                            action(apply, "Zapisano ustawienia.")()

                        ui.button("Zapisz ustawienia", icon="save", on_click=save_settings)

                views.append(calendar_view)
                calendar_view()

            # --- 5. Synchronizacja ---

            with ui.tab_panel(t_sync).classes("gap-3"):
                progress_state: dict[str, Any] = {"lines": [], "done": 0, "total": 0}
                lock = threading.Lock()

                async def dry_run() -> None:
                    await background(ctrl.dry_run, busy="Czytam kalendarz Google…")
                    refresh_all()

                async def confirm_and_apply() -> None:
                    sync = ctrl.sync
                    plan = sync.plan
                    with ui.dialog() as dialog, ui.card().classes("min-w-[440px]"):
                        ui.label(f"Zapisać w kalendarzu „{sync.calendar.summary}”?").classes(
                            "text-h6"
                        )
                        ui.label(
                            f"Dodanie: {len(plan.adds)} · zmiana: {len(plan.updates)} · "
                            f"usunięcie: {len(plan.deletes)}"
                        )
                        count = None
                        if plan.mass_delete:
                            ui.label(
                                f"Plan usuwa dużo zdarzeń ({len(plan.deletes)}). Aby potwierdzić, "
                                "wpisz ich liczbę:"
                            ).classes("text-negative")
                            count = ui.input("Liczba usuwanych zdarzeń")
                        with ui.row().classes("w-full justify-end"):
                            ui.button("Anuluj", on_click=lambda: dialog.submit(False)).props("flat")
                            ui.button(
                                "Zapisz",
                                color="positive",
                                on_click=lambda: dialog.submit(
                                    count is None
                                    or (count.value or "").strip() == str(len(plan.deletes))
                                ),
                            )
                    if not await dialog:
                        notify("Anulowano — nic nie zapisano.")
                        return

                    with lock:
                        progress_state.update(lines=[], done=0, total=plan.operation_count)
                    symbols = {"add": "+", "update": "~", "delete": "−"}

                    def progress(done: int, total: int, op, err: str | None) -> None:
                        line = f"[{done}/{total}] {symbols[op.kind]} {op.label}"
                        if err:
                            line += f" — BŁĄD: {err}"
                        with lock:
                            progress_state["done"] = done
                            progress_state["lines"].append(line)

                    sync_view.refresh()
                    try:
                        outcome = await run.io_bound(ctrl.apply, progress)
                    except PlanChangedError as exc:
                        error(exc)
                        refresh_all()
                        return
                    except USER_ERRORS as exc:
                        error(exc)
                        refresh_all()
                        return
                    finally:
                        flush_progress()
                    result = outcome.result
                    summary = f"Wykonano {result.done} z {result.total} operacji"
                    if result.failures:
                        summary += f", nieudanych: {len(result.failures)}"
                    if result.aborted:
                        summary += f", przerwano: {result.aborted}"
                    if outcome.remaining:
                        notify(
                            f"{summary}. Kalendarz nie jest jeszcze zgodny "
                            f"({len(outcome.remaining)}) — sprawdź zmiany ponownie.",
                            type="warning",
                            multi_line=True,
                            close_button="OK",
                            timeout=0,
                        )
                    else:
                        notify(
                            f"{summary}. Weryfikacja: kalendarz jest zgodny z planem.",
                            type="positive" if not result.failures else "warning",
                            multi_line=True,
                            close_button="OK",
                            timeout=0,
                        )
                    with lock:
                        progress_state["lines"].append(f"Dziennik: {outcome.journal_path}")
                        progress_state["lines"].extend(
                            f"Nadal niezgodne: {line}" for line in outcome.remaining
                        )
                    await dry_run()

                @ui.refreshable
                def sync_view() -> None:
                    with ui.row().classes("items-center"):
                        ui.button("Sprawdź zmiany (dry-run)", icon="search", on_click=dry_run)
                        sync = ctrl.sync
                        can_apply = (
                            sync is not None
                            and not sync.blocked_reason
                            and sync.plan.operation_count > 0
                        )
                        ui.button(
                            "Zapisz w kalendarzu",
                            icon="cloud_upload",
                            color="positive",
                            on_click=confirm_and_apply,
                        ).set_enabled(can_apply)
                    ui.label(
                        "Dry-run tylko czyta kalendarz. Zapis wymaga potwierdzenia; tuż przed nim "
                        "plan jest liczony ponownie i jeśli coś się zmieniło, nic nie zostanie "
                        "zapisane."
                    ).classes("text-caption text-grey-8")

                    if progress_state["total"]:
                        with ui.card().classes("w-full"):
                            ui.label("Postęp zapisu").classes("text-subtitle1")
                            bar = ui.linear_progress(value=0, show_value=False).classes("w-full")
                            log = ui.log(max_lines=500).classes("w-full h-48")
                            shown = {"n": 0}

                            def flush() -> None:
                                with lock:
                                    lines = progress_state["lines"][shown["n"] :]
                                    shown["n"] += len(lines)
                                    total = progress_state["total"] or 1
                                    bar.set_value(progress_state["done"] / total)
                                for line in lines:
                                    log.push(line)

                            ui.timer(0.3, flush)
                            flush_holder["fn"] = flush

                    sync = ctrl.sync
                    if sync is None:
                        ui.label("Kliknij „Sprawdź zmiany”, aby porównać plan z kalendarzem.")
                        return
                    plan = sync.plan
                    with ui.card().classes("w-full"):
                        if sync.calendar is None:
                            ui.label(
                                "Kalendarz docelowy nie jest utworzony — plan zakłada pusty "
                                "kalendarz (zakładka Kalendarz)."
                            ).classes("text-orange-9")
                        else:
                            ui.label(f"Kalendarz: „{sync.calendar.summary}”").classes("text-h6")
                            ui.label(
                                f"Zdarzenia zarządzane przez gcalsync: {plan.managed_total}, "
                                f"inne: {len(plan.unmanaged)} (nigdy nie są ruszane)"
                            )
                        if plan.window:
                            start, end = plan.window
                            ui.label(
                                f"Okno synchronizacji: {start:%Y-%m-%d %H:%M} – "
                                f"{end:%Y-%m-%d %H:%M} · "
                                f"teraz: {plan.now.astimezone():%Y-%m-%d %H:%M}"
                            ).classes("text-caption")
                        with ui.row():
                            _stat("do dodania", len(plan.adds), "positive")
                            _stat("do zmiany", len(plan.updates), "orange-9")
                            _stat("do usunięcia", len(plan.deletes), "negative")
                            _stat("bez zmian", plan.unchanged, "grey-8")
                            _stat("pominięte zakończone", len(plan.skipped_past), "grey-8")
                            _stat("poza oknem", len(plan.kept_outside), "grey-8")
                        if plan.mass_delete:
                            ui.label(
                                "Plan usuwa dużo zdarzeń — sprawdź, czy wgrałeś wszystkie pliki. "
                                "Zapis będzie wymagał wpisania liczby usuwanych zdarzeń."
                            ).classes("text-negative")
                        if sync.blocked_reason:
                            ui.label(f"Zapis zablokowany: {sync.blocked_reason}.").classes(
                                "text-negative text-weight-bold"
                            )
                        if plan.broken:
                            ui.label(
                                f"Zdarzenia z uszkodzonym znacznikiem (nieruszane): "
                                f"{len(plan.broken)}"
                            ).classes("text-orange-9")

                    columns = [
                        _col("day", "Dzień", sortable=True, sort_field="sort"),
                        _col("hours", "Godziny"),
                        _col("title", "Tytuł"),
                        _col("location", "Sala"),
                    ]
                    with ui.expansion(
                        f"Do dodania ({len(plan.adds)})", icon="add_circle", value=bool(plan.adds)
                    ).classes("w-full bg-white"):
                        _table(
                            columns, [_google_row(a.body, f"a{i}") for i, a in enumerate(plan.adds)]
                        )
                    with ui.expansion(
                        f"Do zmiany ({len(plan.updates)})", icon="edit", value=bool(plan.updates)
                    ).classes("w-full bg-white"):
                        _table(
                            [*columns, _col("changes", "Zmiany")],
                            [
                                _google_row(
                                    u.body,
                                    f"u{i}",
                                    changes="; ".join(_change_text(c) for c in u.changes),
                                )
                                for i, u in enumerate(plan.updates)
                            ],
                        )
                    with ui.expansion(
                        f"Do usunięcia ({len(plan.deletes)})",
                        icon="remove_circle",
                        value=bool(plan.deletes),
                    ).classes("w-full bg-white"):
                        _table(
                            [*columns, _col("reason", "Powód")],
                            [
                                _google_row(d.existing, f"d{i}", reason=d.reason)
                                for i, d in enumerate(plan.deletes)
                            ],
                        )

                flush_holder: dict[str, Callable[[], None]] = {"fn": lambda: None}

                def flush_progress() -> None:
                    flush_holder["fn"]()

                views.append(sync_view)
                sync_view()

    return root


def _change_text(change: Any) -> str:
    old, new = _short(change.field, change.old), _short(change.field, change.new)
    return f"{GOOGLE_FIELD_LABELS[change.field]}: „{old}” → „{new}”"


def _short(field: str, value: str) -> str:
    if field in ("start", "end"):
        return display_time(value)
    value = value.replace("\n", " / ")
    return value if len(value) <= 80 else value[:77] + "…"


def run_ui(paths: Paths, port: int = DEFAULT_PORT, show: bool = True) -> None:
    ctrl = Controller(paths)
    ui.run(
        build_root(ctrl),
        host="127.0.0.1",  # tylko ten komputer — aplikacja ma dostęp do tokenu Google
        port=port,
        title="gcalsync",
        favicon="📅",
        language="pl",
        reload=False,
        show=show,
        show_welcome_message=True,
    )
