"""
Per-plan resource quotas for sandboxed worker processes.

Each subscription tier maps to a tuple of cgroup v2 limits that get
applied at worker spawn time:

- ``memory_max`` (bytes): hard ceiling on RSS+cache. The OOM killer
  fires at this limit; we tune it conservatively so paying tiers
  rarely hit it under normal load.
- ``cpu_pct`` (% of one core): cpu.max quota — scheduling fairness,
  not a hard cap. Pro users get 2× the throughput of personal users.
- ``pids_max``: defence against fork-bombs inside a sandbox.

Unknown / missing plans degrade to the ``free`` defaults, which are
the tightest. That way a misconfigured token doesn't accidentally
hand out Pro-tier resources.
"""

from __future__ import annotations

from typing import TypedDict

_MB = 1024 * 1024


class PlanQuotas(TypedDict):
    memory_max: int
    cpu_pct: int
    pids_max: int


# Quota table. The previous hardcoded defaults in ``cgroups.setup_cgroup``
# (512 MiB / 25% / 100 PIDs) become the trial / personal tier — every
# tester / pro user gets a 2× upgrade and free users get a hard
# squeeze so the public-tier worker pool stays cheap to host.
PLAN_QUOTAS: dict[str, PlanQuotas] = {
    "free":     {"memory_max": 256 * _MB, "cpu_pct": 10, "pids_max":  50},
    "trial":    {"memory_max": 512 * _MB, "cpu_pct": 25, "pids_max": 100},
    "personal": {"memory_max": 512 * _MB, "cpu_pct": 25, "pids_max": 100},
    "pro":      {"memory_max": 1024 * _MB, "cpu_pct": 50, "pids_max": 200},
    "tester":   {"memory_max": 1024 * _MB, "cpu_pct": 50, "pids_max": 200},
}

# Default when the plan name is unknown / missing — match free, the
# tightest tier. This is intentionally conservative: any auth-token
# entry without a plan field shouldn't accidentally get pro-tier
# resources.
DEFAULT_PLAN = "free"


def quotas_for_plan(plan: str | None) -> PlanQuotas:
    """Return the quota tuple for a plan name, falling back to free."""
    if plan is None:
        return PLAN_QUOTAS[DEFAULT_PLAN]
    return PLAN_QUOTAS.get(plan, PLAN_QUOTAS[DEFAULT_PLAN])
