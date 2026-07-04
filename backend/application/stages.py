from __future__ import annotations

KNOWN_PROJECT_STAGES = frozenset(
    {
        "INTAKE",
        "DIRECTIONS",
        "LOGO",
        "VI",
        "IP",
        "MATERIALS",
        "REVIEW",
        "PROPOSAL",
    }
)


def normalize_stage_key(stage_key: str) -> str:
    return stage_key.strip().upper().replace("-", "_")
