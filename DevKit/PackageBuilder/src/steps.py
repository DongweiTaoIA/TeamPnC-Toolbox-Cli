from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from .ado import WorkItem
from .business_days import add_business_days


@dataclass(frozen=True)
class MigrationStep:
    name: str
    offset_days: int  # relative to PROD migration date, business days
    checklist: tuple[str, ...] = ()
    action: str = ""


MIGRATION_STEPS = (
    MigrationStep("10 Days before - book resources", -10, action="book_resources"),
    MigrationStep("9 Days before - DPACCP Refresh", -9, action="dpaccp_refresh"),
    MigrationStep("3 Days before", -3),
    MigrationStep("2 Days before", -2),
    MigrationStep("Migration Day", 0),
    MigrationStep("Day After Migration", 1),
)
DAYS_AFTER_PROD = max(step.offset_days for step in MIGRATION_STEPS)
DONE_STATES = {"done", "closed", "resolved", "completed"}


@dataclass
class PlannedStep:
    step: MigrationStep
    due_date: date
    task: WorkItem | None


def _match_key(title: str) -> str:
    return title.split(" - ")[0].strip().lower()


def build_step_plan(
    prod_date: date, children: list[WorkItem], holidays: Collection[date] = ()
) -> list[PlannedStep]:
    plan: list[PlannedStep] = []
    for step in MIGRATION_STEPS:
        key = _match_key(step.name)
        matches = [c for c in children if c.title.strip().lower().startswith(key)]
        # 'Migration day' must win over 'Migration Day - Run Automated tests in Prod'.
        task = next((c for c in matches if c.title.strip().lower() == key), matches[0] if matches else None)
        plan.append(PlannedStep(step, add_business_days(prod_date, step.offset_days, holidays), task))
    return plan
