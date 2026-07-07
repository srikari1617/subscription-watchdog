"""
config/policy.py — User-Editable Policy Configuration for the Decision Agent

Purpose:
    Defines the transparent, auditable policy that the Decision Agent uses
    to evaluate flags. This configuration is deliberately kept OUT of any
    LLM prompt so that behavior is fully inspectable and user-editable
    (PRD Section 8).

Design decisions:
    - A single Python dictionary — the simplest possible representation
      that satisfies the PRD's requirement for a "user-editable config object."
    - The Decision Agent reads this at runtime to make deterministic,
      explainable decisions (no hidden prompt logic).
    - Users can edit thresholds directly in this file without touching
      any agent code.

Fields (all from PRD Section 8):
    auto_flag_threshold_pct  — flag if a subscription's price rises by more
                               than this percentage (default: 10%)
    auto_ignore_below_amount — ignore trivial price increases below this
                               absolute dollar amount (default: $5.00)
    unused_days_threshold    — flag a subscription as "unused" if the user
                               hasn't interacted with it for this many days
                               (default: 60 days)
    draft_action_on          — list of flag reasons that should escalate to
                               the Drafting Agent (default: price_increase, unused)

PRD References:
    - Section 8 (Policy Configuration)
    - Section 6.2 (Decision Agent — "applies a policy configuration")
    - Section 10, Requirement 5 (user-editable policy determines escalation)
"""

policy = {
    # Flag if a subscription's price increases by more than this percentage.
    # Example: a 10% threshold means $10 -> $11 is flagged, $10 -> $10.50 is not.
    "auto_flag_threshold_pct": 10,

    # Ignore price increases where the absolute increase is below this amount.
    # This prevents noise from trivial adjustments (e.g., $0.30 tax changes).
    "auto_ignore_below_amount": 5.00,

    # Flag a subscription as "unused" after this many days of inactivity.
    # Cross-referenced against the user-maintained "still_used" field.
    "unused_days_threshold": 60,

    # Which flag reasons should escalate to the Drafting Agent.
    # Only flags with a reason in this list will generate draft emails.
    # Valid values: "price_increase", "unused", "new_subscription"
    "draft_action_on": ["price_increase", "unused"],
}
