"""AccessFix score: a quick indicator, not a compliance certificate."""

WEIGHTS = {"critical": 15, "serious": 10, "moderate": 5, "minor": 2}
IMPACT_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
TOOLTIP = "AccessFix score: a quick indicator, not a compliance certificate."


def weight(impact: str | None) -> int:
    return WEIGHTS.get(impact or "minor", WEIGHTS["minor"])


def score(violations: list[dict]) -> int:
    """100 minus the weight of every violated rule (each rule counts once), floored at 0."""
    return max(0, 100 - sum(weight(v.get("impact")) for v in violations if v.get("nodes")))


def issue_count(violations: list[dict]) -> int:
    return sum(len(v.get("nodes", [])) for v in violations)
