"""
agents/action.py — Action Agent for Subscription Watchdog

Purpose:
    Final agent in the pipeline. Executes ONLY on approved drafts —
    sends the email via Gmail MCP and creates a Calendar MCP reminder
    ahead of the next renewal date.

Responsibilities (PRD Section 6.2 — Action Agent):
    - Executes only on drafts where approved == True.
    - Sends the approved email via Gmail MCP send_message.
    - Creates a Calendar MCP event as a reminder ahead of next renewal date.

Security design (PRD Section 9):
    - HARD-CODED BOUNDARY: The Action Agent MUST assert Draft.approved == True
      before executing ANY external action. This assertion is the final
      security gate and cannot be bypassed.
    - approved can ONLY be set to True by the Human Approval Gate.
    - Uses Gmail send scope (not read) and Calendar events scope.
    - Every action is logged to the audit trail.

PRD References:
    - Section 6.1 (high-level flow — Action Agent)
    - Section 6.2 (Action Agent responsibilities)
    - Section 9 (human approval before any external action)
    - Section 10, Requirement 7 (shall not send without explicit approval)
    - Section 10, Requirement 8 (send via Gmail MCP, create Calendar reminder)
    - Section 10, Requirement 9 (log every action to audit trail)
"""

import json
from typing import List

from core.models import Draft
from core.database import log_audit


class ApprovalViolationError(Exception):
    """
    Raised when the Action Agent is asked to execute a draft that
    has NOT been approved by the Human Approval Gate.

    This is a hard security boundary — it should never be caught
    and silenced. If this fires, something is seriously wrong with
    the pipeline.
    """
    pass


async def execute_approved_drafts(
    drafts: List[Draft],
    gmail_send_fn=None,
    calendar_create_fn=None,
) -> List[dict]:
    """
    Executes all approved drafts by sending emails and creating
    calendar reminders.

    This is the main entry point for the Action Agent.

    SECURITY: Every draft is checked for approval BEFORE any action.
    An ApprovalViolationError is raised if any unapproved draft is
    encountered — this is a hard stop, not a soft warning.

    Args:
        drafts:             List of Draft objects (must all be approved).
        gmail_send_fn:      Callable that sends an email. If None, uses
                            the Gmail MCP send_message tool.
        calendar_create_fn: Callable that creates a calendar reminder.
                            If None, uses the Calendar MCP create_reminder tool.

    Returns:
        List of result dicts summarizing each action taken.
    """
    results = []

    for draft in drafts:
        result = await _execute_single_draft(
            draft, gmail_send_fn, calendar_create_fn
        )
        results.append(result)

    return results


async def _execute_single_draft(
    draft: Draft,
    gmail_send_fn=None,
    calendar_create_fn=None,
) -> dict:
    """
    Executes a single approved draft.

    Steps:
        1. ASSERT approval (hard security boundary)
        2. Send the email via Gmail MCP
        3. Create a calendar reminder via Calendar MCP
        4. Log everything to the audit trail
    """
    # ===================================================================
    # SECURITY BOUNDARY: Hard-coded approval check (PRD Section 9)
    # This assertion MUST NOT be removed, weakened, or caught upstream.
    # ===================================================================
    if not draft.approved:
        raise ApprovalViolationError(
            f"SECURITY VIOLATION: Attempted to execute unapproved draft "
            f"for {draft.flag.subscription.merchant}. "
            f"Draft.approved is {draft.approved}. "
            f"The Action Agent refuses to proceed."
        )

    sub = draft.flag.subscription
    result = {
        "merchant": sub.merchant,
        "draft_type": draft.draft_type,
        "email_sent": False,
        "reminder_created": False,
        "errors": [],
    }

    # --- Step 1: Send the email ---
    if draft.to_address and draft.body:
        try:
            if gmail_send_fn:
                send_result = gmail_send_fn(
                    to=draft.to_address,
                    subject=draft.subject,
                    body=draft.body,
                )
            else:
                # Use Gmail MCP tool directly
                from mcp_servers.gmail_mcp import send_message
                send_result = send_message(
                    to=draft.to_address,
                    subject=draft.subject,
                    body=draft.body,
                )

            send_data = json.loads(send_result) if isinstance(send_result, str) else send_result
            if send_data.get("status") == "sent":
                result["email_sent"] = True
                result["message_id"] = send_data.get("message_id", "")
            else:
                result["errors"].append(send_data.get("error", "Unknown send error"))

        except Exception as e:
            result["errors"].append(f"Email send failed: {str(e)}")
    else:
        result["errors"].append("No recipient address or body provided")

    # --- Step 2: Create calendar reminder ---
    if sub.next_billing_date:
        try:
            if calendar_create_fn:
                cal_result = calendar_create_fn(
                    merchant=sub.merchant,
                    renewal_date=sub.next_billing_date.isoformat(),
                    amount=sub.amount,
                    currency=sub.currency,
                )
            else:
                # Use Calendar MCP tool directly
                from mcp_servers.calendar_mcp import create_reminder
                cal_result = create_reminder(
                    merchant=sub.merchant,
                    renewal_date=sub.next_billing_date.isoformat(),
                    amount=sub.amount,
                    currency=sub.currency,
                )

            cal_data = json.loads(cal_result) if isinstance(cal_result, str) else cal_result
            if cal_data.get("status") == "created":
                result["reminder_created"] = True
                result["event_id"] = cal_data.get("event_id", "")
            else:
                result["errors"].append(cal_data.get("error", "Unknown calendar error"))

        except Exception as e:
            result["errors"].append(f"Calendar reminder failed: {str(e)}")

    # --- Step 3: Audit trail ---
    log_audit(
        entity_type="action",
        entity_id=draft.id,
        action=f"Action executed: {draft.draft_type}",
        details={
            "merchant": sub.merchant,
            "email_sent": result["email_sent"],
            "reminder_created": result["reminder_created"],
            "errors": result["errors"],
        },
    )

    return result
