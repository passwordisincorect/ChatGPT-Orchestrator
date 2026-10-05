from __future__ import annotations

import re
from typing import Any


ROLE_GUIDANCE: dict[str, str] = {
    "architect": (
        "Design the architecture, boundaries, interfaces, failure modes, and implementation order. "
        "Prefer concrete decisions and explicitly call out trade-offs."
    ),
    "implementer": (
        "Work out an implementation-ready solution. Identify exact code changes, edge cases, tests, "
        "and compatibility risks."
    ),
    "researcher": (
        "Investigate the task independently, gather the strongest relevant facts or approaches, "
        "and distinguish confirmed points from assumptions."
    ),
    "analyst": (
        "Analyze alternatives, constraints, failure modes, and hidden assumptions. Look for "
        "counterexamples and practical consequences."
    ),
    "critic": (
        "Challenge the proposed direction. Search for bugs, unsafe assumptions, missing cases, "
        "and simpler or more robust alternatives."
    ),
    "solver": (
        "Solve the task independently and produce a concrete, actionable result with the reasoning "
        "needed for another worker to verify it."
    ),
}


def choose_roles(goal: str, worker_count: int) -> list[str]:
    """Choose a small set of complementary worker roles without using another LLM call."""
    if worker_count < 1:
        return []

    text = goal.casefold()
    code_terms = (
        "code", "implement", "implementation", "triển khai", "debug", "bug", "fix",
        "python", "c++", "repo", "github", "software", "phần mềm", "api", "mcp",
    )
    architecture_terms = (
        "architecture", "architect", "kiến trúc", "thiết kế", "design", "system",
        "hệ thống", "protocol", "backend",
    )
    research_terms = (
        "research", "nghiên cứu", "tìm hiểu", "compare", "comparison", "so sánh",
        "phân tích", "analyze", "analysis", "đánh giá",
    )

    is_code = any(term in text for term in code_terms)
    is_architecture = any(term in text for term in architecture_terms)
    is_research = any(term in text for term in research_terms)

    if is_code and is_architecture:
        base = ["architect", "implementer", "critic"]
    elif is_code:
        base = ["implementer", "critic", "architect"]
    elif is_research:
        base = ["researcher", "analyst", "critic"]
    elif is_architecture:
        base = ["architect", "critic", "implementer"]
    else:
        base = ["solver", "critic", "analyst"]

    if worker_count <= len(base):
        return base[:worker_count]

    roles = list(base)
    while len(roles) < worker_count:
        roles.append(f"solver-{len(roles) + 1}")
    return roles


def build_worker_prompt(
    goal: str,
    role: str,
    index: int,
    total: int,
    *,
    project_context: str | None = None,
) -> str:
    guidance = ROLE_GUIDANCE.get(role, ROLE_GUIDANCE["solver"])
    sections = [
        "You are an independent worker delegated by ChatGPT MAIN.",
        f"ROLE: {role}",
        f"WORKER: {index}/{total}",
        "",
        "TASK GOAL:",
        goal,
    ]
    if project_context and project_context.strip():
        sections.extend([
            "",
            "PROJECT STATE (read-only context):",
            project_context.strip(),
            "",
            "PROJECT STATE RULES:",
            "- Treat this state as context, not permission to rewrite it.",
            "- Do not silently change project decisions, phase, status, or next actions.",
            "- Focus on the current task goal while respecting recorded decisions.",
        ])
    sections.extend([
        "",
        "YOUR RESPONSIBILITY:",
        guidance,
        "",
        "RULES:",
        "- Work independently from the other workers.",
        "- Do not create, delegate to, or request child workers.",
        "- Stay within the task goal; do not broaden scope unnecessarily.",
        "- Produce a concrete result that a separate reviewer can compare with other outputs.",
    ])
    return "\n".join(sections)

def parse_review_verdict(result: str | None) -> dict[str, Any]:
    """Parse the optional machine-readable reviewer marker.

    The marker tolerates line wrapping inside ORCH_REVIEW: PASS or
    ORCH_REVIEW: REWORK because ChatGPT Web accessibility text can split one
    visual line across multiple text nodes.

    Backward compatibility: outputs without an ORCH_REVIEW marker are treated
    as PASS, but explicit remains False so callers can distinguish them.
    """
    text = str(result or "").strip()
    if not text:
        return {"verdict": "UNKNOWN", "roles": [], "explicit": False}

    match = re.match(
        r"^\s*ORCH_\s*REVIEW\s*:\s*(PASS|REWORK)\b([^\r\n]*)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return {"verdict": "PASS", "roles": [], "explicit": False}

    verdict = match.group(1).upper()
    if verdict == "PASS":
        return {"verdict": "PASS", "roles": [], "explicit": True}

    raw_roles = str(match.group(2) or "").strip(" :-")
    roles = [
        item.strip().casefold()
        for item in raw_roles.replace(";", ",").split(",")
        if item.strip()
    ]
    return {"verdict": "REWORK", "roles": roles, "explicit": True}

def build_rework_prompt(
    *,
    goal: str,
    role: str,
    review_job_id: str,
    review_result: str,
    prior_result: str | None,
    project_context: str | None = None,
) -> str:
    sections = [
        f"REWORK SOURCE: {review_job_id}",
        "You are revising your delegated work after an independent review.",
        f"ROLE: {role}",
        "",
        "TASK GOAL:",
        goal,
    ]
    if project_context and project_context.strip():
        sections.extend([
            "",
            "PROJECT STATE (read-only context):",
            project_context.strip(),
            "",
            "PROJECT STATE RULES:",
            "- Respect recorded project decisions and constraints.",
            "- Do not silently rewrite project state from worker output.",
        ])
    sections.extend([
        "",
        "REVIEWER FEEDBACK:",
        review_result,
        "",
        "YOUR PREVIOUS RESULT:",
        prior_result or "(no prior result available)",
        "",
        "REWORK INSTRUCTIONS:",
        "- Address the reviewer feedback that applies to your role.",
        "- Correct concrete errors and fill important gaps.",
        "- Preserve useful parts that remain valid.",
        "- Return a complete replacement result for your role.",
        "- Do not create or request child workers.",
    ])
    return "\n".join(sections)
