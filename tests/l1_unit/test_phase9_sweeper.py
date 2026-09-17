"""Procrastinate schedules retention classes as independent maintenance jobs."""

from __future__ import annotations

from typing import Any, cast

import pytest

from bluelab.work import maintenance


@pytest.mark.l1_unit
async def test_failed_retention_class_does_not_skip_other_scheduled_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    tasks: dict[str, Any] = {}
    schedules: list[tuple[str, str]] = []

    class FakeApp:
        def task(self, *, name: str, queue: str):
            assert queue == "maintenance"

            def register(fn):
                tasks[name] = fn
                return fn

            return register

        def periodic(self, *, cron: str):
            def schedule(fn):
                schedules.append((cron, fn.__name__))
                return fn

            return schedule

    async def run_class(retention_class: str, _store: object) -> None:
        called.append(retention_class)
        if retention_class == "RC-3":
            raise RuntimeError("retention dependency unavailable")

    monkeypatch.setattr(maintenance, "RETENTION_CLASSES", ("RC-3", "RC-4"))
    monkeypatch.setattr(maintenance, "create_object_store", lambda _settings: object())
    monkeypatch.setattr(maintenance, "run_retention_class", run_class)
    maintenance.register_maintenance(cast(Any, FakeApp()), cast(Any, object()))
    assert len(schedules) == 3
    with pytest.raises(RuntimeError):
        await tasks["retention_rc_3"](timestamp=0)
    await tasks["retention_rc_4"](timestamp=0)
    assert called == ["RC-3", "RC-4"]
