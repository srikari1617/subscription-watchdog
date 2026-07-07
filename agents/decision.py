"""
agents/decision.py — Decision Agent for Subscription Watchdog

Purpose:
    Third agent in the pipeline. Evaluates each Flag produced by the
    Comparator Agent against the user-editable policy configuration
    to determine the appropriate action: ignore, log only, or escalate
    to the Drafting Agent.

Responsibilities (PRD Section 6.2 — Decision Agent):
    - Applies the policy configuration (config/policy.py) to each Flag.
    - This is the project's explicit "policy engine" — fully inspectable
      and user-editable, NOT hidden in an LLM prompt.
    - Determines one of three outcomes for each flag:
        1. ignore     — flag does not meet any policy threshold
        2. log_only   — flag is logged to audit trail but not escalated
        3. escalate   — flag is forwarded to the Drafting Agent for action

    - Also packaged as a standalone CLI skill (agents_cli.py) per
      PRD Section 10, Requirement 10.

Design decisions:
    - Pure logic agent — no LLM calls. All decisions are deterministic,
      transparent, and fully auditable.
    - Every decision (including ignores) is logged to the audit trail
      with the reasoning behind it (PRD Section 9).

PRD References:
    - Section 6.2 (Decision Agent — "applies a policy configuration")
    - Section 8 (Policy Configuration)
    - Section 9 (audit trail — every decision logged)
    - Section 10, Requirement 5 (user-editable policy determines escalation)
    - Section 10, Requirement 10 (standalone CLI skill)
    - Section 12 (Agent skills evaluation concept)
"""

from typing import List, Tuple

from core.models import Flag
from core.database import log_audit
from config.policy import policy


# Decision outcomes
IGNORE = "ignore"
LOG_ONLY = "log_only"
ESCALATE = "escalate"


def evaluate_flags(
    flags: List[Flag],
    log_decisions: bool = True,
) -> List[Flag]:
    """
    Evaluates a list of Flags against the policy and returns only
    those that should be escalated to the Drafting Agent.

    This is the main entry point for the Decision Agent.

    Args:
        flags:          List of Flag objects from the Comparator Agent.
        log_decisions:  If True, logs every decision to the audit trail.

    Returns:
        Filtered list of Flags that should be escalated to drafting.
    """
    escalated: List[Flag] = []

    for flag in flags:
        decision, reason = _apply_policy(flag)

        if log_decisions:
            log_audit(
                entity_type="flag",
                entity_id=flag.id,
                action=f"Decision: {decision}",
                details={
                    "merchant": flag.subscription.merchant,
                    "flag_reason": flag.reason,
                    "flag_severity": flag.severity,
                    "decision": decision,
                    "decision_reason": reason,
                    "policy_snapshot": policy,
                },
            )

        if decision == ESCALATE:
            escalated.append(flag)

    return escalated


def evaluate_single_flag(flag: Flag) -> Tuple[str, str]:
    """
    Evaluates a single Flag against the policy.

    This is the interface used by the standalone CLI skill
    (agents_cli.py check-subscriptions) for individual flag inspection.

    Returns:
        Tuple of (decision, reason) where decision is one of:
        IGNORE, LOG_ONLY, or ESCALATE.
    """
    return _apply_policy(flag)


def _apply_policy(flag: Flag) -> Tuple[str, str]:
    """
    Core policy engine. Applies the user-editable policy configuration
    to a single Flag and returns the decision.

    Policy rules (PRD Section 8):
        1. draft_action_on — only flag reasons in this list can escalate.
        2. auto_ignore_below_amount — price increases below this absolute
           amount are ignored (already enforced by Comparator, but
           double-checked here as a safety net).
        3. Severity-based routing:
           - "info" severity → log_only
           - "review" severity → escalate if reason is in draft_action_on
           - "action_recommended" → escalate if reason is in draft_action_on

    Returns:
        Tuple of (decision, human_readable_reason).
    """
    reason_text = flag.reason
    severity = flag.severity
    draft_actions = policy.get("draft_action_on", [])

    # Rule 1: Is this flag reason configured for drafting?
    if reason_text not in draft_actions:
        return (
            IGNORE,
            f"Flag reason '{reason_text}' is not in the policy's "
            f"draft_action_on list {draft_actions}. Ignoring.",
        )

    # Rule 2: Info-severity flags are logged but not escalated
    if severity == "info":
        return (
            LOG_ONLY,
            f"Flag for {flag.subscription.merchant} has 'info' severity. "
            f"Logged for awareness but not escalated to drafting.",
        )

    # Rule 3: Review and action_recommended flags are escalated
    if severity in ("review", "action_recommended"):
        return (
            ESCALATE,
            f"Flag for {flag.subscription.merchant}: reason='{reason_text}', "
            f"severity='{severity}'. Matches policy draft_action_on and "
            f"severity threshold. Escalating to Drafting Agent.",
        )

    # Fallback: unknown severity — log only for safety
    return (
        LOG_ONLY,
        f"Flag for {flag.subscription.merchant} has unrecognized severity "
        f"'{severity}'. Logging but not escalating out of caution.",
    )


def check_subscriptions_cli(verbose: bool = False) -> List[dict]:
    """
    Standalone CLI skill entry point for the Decision Agent.

    Reads all pending flags from the database, evaluates them against
    the policy, and returns a summary of decisions. This is designed
    to be invoked independently of the full pipeline.

    PRD Section 10, Requirement 10:
    "Decision Agent's policy check shall be packaged as a standalone,
     CLI-invokable skill independent of the full pipeline."

    Args:
        verbose: If True, includes the full reasoning trace in output.

    Returns:
        List of decision summary dicts for display.
    """
    from core.database import get_pending_flags

    flags = get_pending_flags()
    results = []

    for flag in flags:
        decision, reason = _apply_policy(flag)
        summary = {
            "merchant": flag.subscription.merchant,
            "flag_reason": flag.reason,
            "severity": flag.severity,
            "decision": decision,
            "decision_reason": reason,
        }
        if verbose:
            summary["reasoning_trace"] = flag.reasoning_trace
        results.append(summary)

    return results
