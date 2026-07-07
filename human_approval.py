"""
human_approval.py — Human Approval Gate for Subscription Watchdog

Purpose:
    The security boundary between draft generation and action execution.
    Presents each generated Draft to the user in an interactive CLI with
    approve/reject/edit choices. No draft proceeds to the Action Agent
    without an explicit approved=True flag set by this gate.

This is a FIRST-CLASS SECURITY REQUIREMENT, not a convenience feature
(PRD Section 9).

Interface (PRD Section 6.2 — Human Approval Gate):
    - Minimal CLI presenting each Draft with:
        * The subscription details
        * The reasoning trace (why was this flagged)
        * The draft email content
        * An approve / reject / edit choice
    - No draft proceeds to Action Agent without explicit approval.

PRD References:
    - Section 3.2 (Non-Goal: no automatic sending without human sign-off, ever)
    - Section 5, User Story 4 (review drafted email before it's sent)
    - Section 5, User Story 5 (see why the agent flagged something)
    - Section 6.2 (Human Approval Gate)
    - Section 9 (human approval before any external action)
    - Section 10, Requirement 7 (present drafts, no send without approval)
"""

import asyncio
from typing import List

from core.models import Draft
from core.database import log_audit
from agents.orchestrator import execute_approved


# ---------------------------------------------------------------------------
# ANSI color helpers for CLI presentation
# ---------------------------------------------------------------------------

class _Colors:
    """Simple ANSI color codes for terminal output."""
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _colored(text: str, color: str) -> str:
    return f"{color}{text}{_Colors.RESET}"


# ---------------------------------------------------------------------------
# Draft presentation
# ---------------------------------------------------------------------------

def _display_draft(draft: Draft, index: int, total: int) -> None:
    """
    Displays a single draft in a clear, readable format showing:
    - Subscription details
    - Why it was flagged (reasoning trace)
    - The draft email content
    """
    sub = draft.flag.subscription
    flag = draft.flag

    print()
    print(_colored("=" * 70, _Colors.BLUE))
    print(_colored(
        f"  DRAFT {index}/{total} — {draft.draft_type.upper()}",
        _Colors.BOLD + _Colors.HEADER,
    ))
    print(_colored("=" * 70, _Colors.BLUE))

    # Subscription details
    print()
    print(_colored("  📦 Subscription Details:", _Colors.BOLD))
    print(f"     Merchant:      {sub.merchant}")
    print(f"     Amount:        {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}")
    if sub.last_known_amount > 0 and sub.last_known_amount != sub.amount:
        print(f"     Previous:      {sub.currency} {sub.last_known_amount:.2f}/{sub.billing_cycle}")
    if sub.next_billing_date:
        print(f"     Next billing:  {sub.next_billing_date.isoformat()}")

    # Reasoning trace — why was this flagged (User Story 5)
    print()
    print(_colored("  🔍 Why this was flagged:", _Colors.BOLD))
    print(f"     Reason:   {flag.reason}")
    print(f"     Severity: {flag.severity}")
    print()
    print(_colored("     Reasoning trace:", _Colors.DIM))
    for line in flag.reasoning_trace.split(". "):
        print(f"       • {line.strip()}")

    # Draft email content
    print()
    print(_colored("  ✉️  Draft Email:", _Colors.BOLD))
    print(_colored(f"     Subject: {draft.subject}", _Colors.CYAN))
    if draft.to_address:
        print(f"     To:      {draft.to_address}")
    else:
        print(_colored("     To:      [not set — enter during approval]", _Colors.YELLOW))
    print()
    print(_colored("     --- Email Body ---", _Colors.DIM))
    for line in draft.body.split("\n"):
        print(f"     {line}")
    print(_colored("     --- End Body ---", _Colors.DIM))
    print()


# ---------------------------------------------------------------------------
# Approval interaction
# ---------------------------------------------------------------------------

def _prompt_approval(draft: Draft) -> Draft:
    """
    Prompts the user to approve, reject, or edit a draft.

    Returns the draft with approved field set accordingly.
    This is the ONLY place in the entire codebase where
    Draft.approved can be set to True.
    """
    while True:
        print(_colored("  Choose an action:", _Colors.BOLD + _Colors.YELLOW))
        print("     [a] Approve — send this email")
        print("     [r] Reject  — discard this draft")
        print("     [e] Edit    — modify the recipient address")
        print("     [v] View    — show the draft again")
        print()

        choice = input(_colored("  Your choice (a/r/e/v): ", _Colors.GREEN)).strip().lower()

        if choice == "a":
            # Require recipient address before approving
            if not draft.to_address:
                addr = input(_colored(
                    "  Enter recipient email address: ", _Colors.CYAN
                )).strip()
                if not addr or "@" not in addr:
                    print(_colored("  ⚠ Invalid email address. Try again.", _Colors.RED))
                    continue
                draft.to_address = addr

            # === SECURITY: This is the ONLY line that sets approved=True ===
            draft.approved = True

            # Log approval to audit trail
            log_audit(
                entity_type="approval",
                entity_id=draft.id,
                action="Draft APPROVED by human",
                details={
                    "merchant": draft.flag.subscription.merchant,
                    "draft_type": draft.draft_type,
                    "to_address": draft.to_address,
                },
            )

            print(_colored("  ✅ Draft APPROVED.", _Colors.GREEN + _Colors.BOLD))
            return draft

        elif choice == "r":
            draft.approved = False

            # Log rejection to audit trail
            log_audit(
                entity_type="approval",
                entity_id=draft.id,
                action="Draft REJECTED by human",
                details={
                    "merchant": draft.flag.subscription.merchant,
                    "draft_type": draft.draft_type,
                },
            )

            print(_colored("  ❌ Draft REJECTED.", _Colors.RED + _Colors.BOLD))
            return draft

        elif choice == "e":
            addr = input(_colored(
                "  Enter new recipient email address: ", _Colors.CYAN
            )).strip()
            if addr and "@" in addr:
                draft.to_address = addr
                print(_colored(f"  📝 Recipient updated to: {addr}", _Colors.CYAN))
            else:
                print(_colored("  ⚠ Invalid email address.", _Colors.RED))

        elif choice == "v":
            _display_draft(draft, 0, 0)

        else:
            print(_colored("  ⚠ Invalid choice. Enter a, r, e, or v.", _Colors.RED))


# ---------------------------------------------------------------------------
# Main approval flow
# ---------------------------------------------------------------------------

def review_drafts(drafts: List[Draft]) -> List[Draft]:
    """
    Presents all drafts to the user for review and returns the list
    with approval status set on each.

    This is the main entry point for the Human Approval Gate.

    Args:
        drafts: List of Draft objects from the Drafting Agent.

    Returns:
        The same list of Draft objects with approved flags set
        based on human decisions.
    """
    if not drafts:
        print()
        print(_colored("  No drafts to review. Pipeline complete.", _Colors.GREEN))
        return drafts

    print()
    print(_colored("=" * 70, _Colors.BOLD + _Colors.HEADER))
    print(_colored(
        "  🛡️  HUMAN APPROVAL GATE — Subscription Watchdog",
        _Colors.BOLD + _Colors.HEADER,
    ))
    print(_colored("=" * 70, _Colors.BOLD + _Colors.HEADER))
    print()
    print(f"  {len(drafts)} draft(s) require your review.")
    print("  No email will be sent without your explicit approval.")
    print()

    for i, draft in enumerate(drafts, 1):
        _display_draft(draft, i, len(drafts))
        _prompt_approval(draft)

    # Summary
    approved_count = sum(1 for d in drafts if d.approved)
    rejected_count = sum(1 for d in drafts if not d.approved)

    print()
    print(_colored("=" * 70, _Colors.BLUE))
    print(_colored("  Review Summary:", _Colors.BOLD))
    print(f"     ✅ Approved: {approved_count}")
    print(f"     ❌ Rejected: {rejected_count}")
    print(_colored("=" * 70, _Colors.BLUE))
    print()

    return drafts


def run_approval_and_execute(drafts: List[Draft]) -> List[dict]:
    """
    Full approval flow: review drafts, then execute approved ones.

    This is the convenience entry point that chains the Human Approval
    Gate with the Action Agent.
    """
    reviewed = review_drafts(drafts)
    approved = [d for d in reviewed if d.approved]

    if not approved:
        print(_colored("  No drafts were approved. No actions taken.", _Colors.YELLOW))
        return []

    print(_colored(
        f"  Executing {len(approved)} approved draft(s)...",
        _Colors.GREEN + _Colors.BOLD,
    ))

    results = asyncio.run(execute_approved(approved))

    for result in results:
        merchant = result.get("merchant", "Unknown")
        if result.get("email_sent"):
            print(_colored(f"  ✅ Email sent for {merchant}", _Colors.GREEN))
        if result.get("reminder_created"):
            print(_colored(f"  📅 Reminder created for {merchant}", _Colors.CYAN))
        if result.get("errors"):
            for err in result["errors"]:
                print(_colored(f"  ⚠ {merchant}: {err}", _Colors.RED))

    return results


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # When run directly, load pending drafts from the most recent pipeline run.
    # This allows the approval gate to be run independently of main.py.
    print(_colored(
        "\n  Subscription Watchdog — Human Approval Gate\n",
        _Colors.BOLD + _Colors.HEADER,
    ))
    print("  This gate reviews drafts generated by the pipeline.")
    print("  Run main.py first to generate drafts, then run this file.")
    print()
