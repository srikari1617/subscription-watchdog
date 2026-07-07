"""
core/models.py — Data Models for Subscription Watchdog

Purpose:
    Defines the three core data structures used throughout the entire pipeline:
    - Subscription: structured fields extracted from Gmail emails
    - Flag: a detected issue (price increase, unused, new subscription)
    - Draft: a generated cancellation/negotiation email awaiting human approval

Design decisions:
    - Uses Python dataclasses as specified in PRD Section 7.
    - Raw email body content is NEVER stored — only structured fields are persisted.
      This enforces the "data minimization" security requirement (PRD Section 9).
    - source_email_id stores only the Gmail message ID, never the full body.
    - Draft.approved defaults to False and can ONLY be set to True by the
      Human Approval Gate — this is the hard-coded security boundary (PRD Section 9).

PRD References:
    - Section 7 (Data Model)
    - Section 9 (Security & Privacy — data minimization)
    - Section 10, Requirement 3 (persist only structured data)
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class Subscription:
    """
    Represents a single recurring subscription detected from a user's inbox.

    Fields map 1:1 to the PRD Section 7 specification:
        merchant          — name of the service provider (e.g. "Netflix")
        amount            — current billing amount
        currency          — ISO currency code (e.g. "USD")
        billing_cycle     — "monthly" or "annual"
        next_billing_date — the next expected charge date
        last_known_amount — the previously recorded amount (for price-change detection)
        still_used        — user-declared usage status; None means unknown
        source_email_id   — Gmail message ID only, never the full email body
    """
    merchant: str
    amount: float
    currency: str
    billing_cycle: str               # "monthly" | "annual"
    next_billing_date: Optional[date] = None
    last_known_amount: float = 0.0
    still_used: Optional[bool] = None  # user-declared; None = unknown
    source_email_id: str = ""          # Gmail message id only, never full body
    id: Optional[int] = None           # database primary key, None until persisted


@dataclass
class Flag:
    """
    Represents a detected issue with a subscription that may require action.

    The Comparator Agent creates these by diffing new scan results against
    stored history. The Decision Agent then evaluates them against the
    user-editable policy to decide whether to escalate.

    Fields:
        subscription      — the Subscription this flag pertains to
        reason            — one of: "price_increase", "unused", "new_subscription"
        severity          — one of: "info", "review", "action_recommended"
        reasoning_trace   — human-readable explanation of why this was flagged
        created_at        — timestamp for the audit trail
    """
    subscription: Subscription
    reason: str                        # "price_increase" | "unused" | "new_subscription"
    severity: str                      # "info" | "review" | "action_recommended"
    reasoning_trace: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: Optional[int] = None           # database primary key


@dataclass
class Draft:
    """
    Represents a generated email (cancellation or negotiation) awaiting human review.

    Security boundary:
        - approved defaults to False
        - ONLY the Human Approval Gate may set approved = True
        - The Action Agent MUST assert approved == True before executing
        - The Drafting Agent structurally cannot call any send function —
          its only output type is this Draft object (PRD Section 6.2)

    Fields:
        flag              — the Flag that triggered this draft
        draft_type        — "cancellation" or "negotiation"
        subject           — email subject line
        to_address        — recipient email address
        body              — the draft email body text
        approved          — hard-coded to False; set only by Human Approval Gate
        created_at        — timestamp for the audit trail
    """
    flag: Flag
    draft_type: str                    # "cancellation" | "negotiation"
    subject: str = ""
    to_address: str = ""
    body: str = ""
    approved: bool = False             # SECURITY: only Human Approval Gate sets True
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: Optional[int] = None           # database primary key
