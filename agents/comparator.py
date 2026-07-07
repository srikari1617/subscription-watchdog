"""
agents/comparator.py — Comparator Agent for Subscription Watchdog

Purpose:
    Second agent in the pipeline. Compares newly scanned subscription data
    against the stored history in subscriptions.db to detect changes that
    warrant user attention.

Responsibilities (PRD Section 6.2 — Comparator Agent):
    - Reads current state from subscriptions.db.
    - Compares each new scan result against the last known price and
      billing history for that merchant.
    - Produces Flag objects for three categories:
        1. price_increase  — the amount went up compared to last known
        2. new_subscription — merchant not previously seen in the database
        3. unused           — subscription not declared as "still used" and
                              exceeds the configurable inactivity threshold
    - Cross-references against the user-maintained "still_used" field.

Design decisions:
    - Pure logic agent — no LLM calls needed. Comparison is deterministic
      and fully auditable.
    - All new/updated subscriptions are persisted to the database during
      comparison so the Comparator always has the latest state.
    - Each Flag includes a human-readable reasoning_trace explaining
      exactly why it was generated (PRD Section 6.2, Drafting Agent input).

PRD References:
    - Section 6.2 (Comparator Agent)
    - Section 7 (Data Model — Flag)
    - Section 10, Requirement 4 (detect price increases, new subs, unused)
"""

from datetime import date, timedelta
from typing import List

from core.models import Subscription, Flag
from core.database import (
    get_all_subscriptions,
    get_subscription_by_merchant,
    upsert_subscription,
    insert_flag,
)
from config.policy import policy


def compare_subscriptions(
    scanned: List[Subscription],
    persist: bool = True,
) -> List[Flag]:
    """
    Compares a list of newly scanned subscriptions against the database
    and generates Flag objects for detected issues.

    This is the main entry point for the Comparator Agent.

    Args:
        scanned:  List of Subscription objects from the Scanner Agent.
        persist:  If True, upserts subscriptions and persists flags to the DB.
                  Set to False for dry-run testing.

    Returns:
        List of Flag objects representing detected issues.
    """
    flags: List[Flag] = []

    for sub in scanned:
        existing = get_subscription_by_merchant(sub.merchant)

        if existing is None:
            # --- New subscription detected ---
            flag = _flag_new_subscription(sub)
            flags.append(flag)

            if persist:
                sub_id = upsert_subscription(sub)
                sub.id = sub_id
                flag.subscription = sub
                insert_flag(flag)

        else:
            # --- Existing subscription: check for price changes ---
            sub.id = existing.id
            sub.last_known_amount = existing.amount
            sub.still_used = existing.still_used

            # Check for price increase
            price_flag = _check_price_increase(sub, existing)
            if price_flag:
                flags.append(price_flag)

            # Check for unused subscription
            unused_flag = _check_unused(sub, existing)
            if unused_flag:
                flags.append(unused_flag)

            if persist:
                upsert_subscription(sub)
                for f in [price_flag, unused_flag]:
                    if f is not None:
                        insert_flag(f)

    # Also check existing DB subscriptions not in the current scan
    # (they might be unused if they haven't appeared recently)
    all_existing = get_all_subscriptions()
    scanned_merchants = {s.merchant.lower() for s in scanned}

    for existing_sub in all_existing:
        if existing_sub.merchant.lower() not in scanned_merchants:
            unused_flag = _check_unused_no_recent_activity(existing_sub)
            if unused_flag:
                flags.append(unused_flag)
                if persist:
                    insert_flag(unused_flag)

    return flags


# ---------------------------------------------------------------------------
# Flag generation helpers
# ---------------------------------------------------------------------------

def _flag_new_subscription(sub: Subscription) -> Flag:
    """
    Creates a Flag for a newly detected subscription that wasn't
    previously in the database.
    """
    return Flag(
        subscription=sub,
        reason="new_subscription",
        severity="review",
        reasoning_trace=(
            f"New subscription detected: {sub.merchant} at "
            f"{sub.currency} {sub.amount:.2f}/{sub.billing_cycle}. "
            f"This merchant was not found in the existing subscription "
            f"database. Review to confirm this is intentional."
        ),
    )


def _check_price_increase(
    current: Subscription, previous: Subscription
) -> Flag | None:
    """
    Checks if a subscription's price has increased compared to the
    last known amount. Returns a Flag if the increase exceeds the
    policy threshold, or None if no significant change.

    Uses two policy thresholds (PRD Section 8):
        - auto_flag_threshold_pct: percentage increase threshold
        - auto_ignore_below_amount: absolute increase floor
    """
    if previous.amount <= 0:
        return None

    price_diff = current.amount - previous.amount

    # No increase — nothing to flag
    if price_diff <= 0:
        return None

    # Check absolute floor: ignore trivial increases (PRD Section 8)
    if price_diff < policy["auto_ignore_below_amount"]:
        return None

    # Check percentage threshold
    pct_increase = (price_diff / previous.amount) * 100

    if pct_increase >= policy["auto_flag_threshold_pct"]:
        return Flag(
            subscription=current,
            reason="price_increase",
            severity="action_recommended",
            reasoning_trace=(
                f"Price increase detected for {current.merchant}: "
                f"{current.currency} {previous.amount:.2f} → "
                f"{current.currency} {current.amount:.2f} "
                f"(+{pct_increase:.1f}%, +{current.currency} {price_diff:.2f}). "
                f"This exceeds the policy threshold of "
                f"{policy['auto_flag_threshold_pct']}% / "
                f"{current.currency} {policy['auto_ignore_below_amount']:.2f} minimum."
            ),
        )

    return None


def _check_unused(
    current: Subscription, existing: Subscription
) -> Flag | None:
    """
    Checks if a subscription appears to be unused based on the user's
    "still_used" declaration.

    A subscription is flagged as unused if:
        - still_used is explicitly False, OR
        - still_used is None (unknown) and no recent activity detected

    PRD Section 6.2: "cross-referenced against a user-maintained
    'still using' list"
    """
    # If user explicitly marked as still in use, don't flag
    if existing.still_used is True:
        return None

    # If user explicitly marked as NOT in use, flag it
    if existing.still_used is False:
        return Flag(
            subscription=current,
            reason="unused",
            severity="action_recommended",
            reasoning_trace=(
                f"Subscription to {current.merchant} "
                f"({current.currency} {current.amount:.2f}/{current.billing_cycle}) "
                f"has been marked as no longer in use by the user. "
                f"Consider cancelling to avoid further charges."
            ),
        )

    return None


def _check_unused_no_recent_activity(sub: Subscription) -> Flag | None:
    """
    Flags a subscription that was NOT in the latest scan results and
    whose still_used status is unknown or False.

    This catches subscriptions that may have gone dormant — they're
    still charging but no recent email activity was detected.
    """
    # Don't flag if user explicitly marked as still in use
    if sub.still_used is True:
        return None

    # Check if next_billing_date is approaching or past
    if sub.next_billing_date and sub.next_billing_date <= date.today():
        return Flag(
            subscription=sub,
            reason="unused",
            severity="review",
            reasoning_trace=(
                f"Subscription to {sub.merchant} "
                f"({sub.currency} {sub.amount:.2f}/{sub.billing_cycle}) "
                f"was not found in recent email scan results and its billing "
                f"date ({sub.next_billing_date.isoformat()}) has passed. "
                f"This may indicate an unused subscription still being charged. "
                f"Usage status is currently unknown."
            ),
        )

    return None
