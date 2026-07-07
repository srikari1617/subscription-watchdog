"""
agents_cli.py — Standalone CLI Skill for Subscription Watchdog

Purpose:
    Packages the Decision Agent's policy check as a standalone, CLI-invokable
    skill independent of the full pipeline. This satisfies the "Agent skills"
    evaluation criterion and PRD Section 10, Requirement 10.

Usage:
    python agents_cli.py check-subscriptions           # Show all flags + decisions
    python agents_cli.py check-subscriptions --verbose  # Include reasoning traces
    python agents_cli.py list-subscriptions             # Show all tracked subscriptions
    python agents_cli.py show-policy                    # Display current policy config
    python agents_cli.py audit-log                      # Show recent audit trail entries

Design decisions:
    - This CLI operates on the existing database state. It does NOT trigger
      a new email scan or create new drafts — it only reads and evaluates.
    - This allows the user to inspect the system's state and decisions
      at any time without running the full pipeline.
    - Demonstrates the "Agent skills (Agents CLI)" evaluation concept.

PRD References:
    - Section 10, Requirement 10 (standalone CLI skill — "agents-cli check-subscriptions")
    - Section 12 (Agent skills evaluation concept)
    - Section 8 (policy configuration — displayed by show-policy)
    - Section 9 (audit trail — displayed by audit-log)
"""

import argparse
import json
import sys

from dotenv import load_dotenv
load_dotenv()

from core.database import init_db, get_all_subscriptions, get_connection
from agents.decision import check_subscriptions_cli
from config.policy import policy


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

class _Colors:
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
# Commands
# ---------------------------------------------------------------------------

def cmd_check_subscriptions(args: argparse.Namespace) -> None:
    """
    Runs the Decision Agent's policy check against all pending flags
    in the database and displays the results.

    This is the core "agent skill" — it can be invoked independently
    of the full pipeline to inspect what the policy engine would decide.
    """
    init_db()

    print()
    print(_colored("  🔍 Subscription Watchdog — Policy Check", _Colors.BOLD + _Colors.HEADER))
    print(_colored("─" * 55, _Colors.BLUE))
    print()

    results = check_subscriptions_cli(verbose=args.verbose)

    if not results:
        print(_colored("  No pending flags to evaluate.", _Colors.DIM))
        print("  Run the full pipeline first: python main.py")
        return

    for i, r in enumerate(results, 1):
        # Color-code by decision
        if r["decision"] == "escalate":
            icon = "🔴"
            color = _Colors.RED
        elif r["decision"] == "log_only":
            icon = "🟡"
            color = _Colors.YELLOW
        else:
            icon = "⚪"
            color = _Colors.DIM

        print(f"  {icon} [{i}] {_colored(r['merchant'], _Colors.BOLD)}")
        print(f"       Flag:     {r['flag_reason']} ({r['severity']})")
        print(f"       Decision: {_colored(r['decision'].upper(), color)}")
        print(f"       Reason:   {r['decision_reason']}")

        if args.verbose and r.get("reasoning_trace"):
            print(_colored(f"       Trace:    {r['reasoning_trace']}", _Colors.DIM))

        print()

    # Summary
    escalated = sum(1 for r in results if r["decision"] == "escalate")
    logged = sum(1 for r in results if r["decision"] == "log_only")
    ignored = sum(1 for r in results if r["decision"] == "ignore")

    print(_colored("─" * 55, _Colors.BLUE))
    print(f"  Total: {len(results)} flag(s)")
    print(f"    🔴 Escalate:  {escalated}")
    print(f"    🟡 Log only:  {logged}")
    print(f"    ⚪ Ignore:    {ignored}")
    print()


def cmd_list_subscriptions(args: argparse.Namespace) -> None:
    """
    Lists all subscriptions currently tracked in the database.
    """
    init_db()

    print()
    print(_colored("  📦 Tracked Subscriptions", _Colors.BOLD + _Colors.HEADER))
    print(_colored("─" * 55, _Colors.BLUE))
    print()

    subs = get_all_subscriptions()

    if not subs:
        print(_colored("  No subscriptions tracked yet.", _Colors.DIM))
        print("  Run the full pipeline first: python main.py")
        return

    for i, sub in enumerate(subs, 1):
        status = "✅ In use" if sub.still_used else (
            "❌ Not used" if sub.still_used is False else "❓ Unknown"
        )
        print(f"  [{i}] {_colored(sub.merchant, _Colors.BOLD)}")
        print(f"      Amount:  {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}")
        if sub.last_known_amount > 0 and sub.last_known_amount != sub.amount:
            print(f"      Previous: {sub.currency} {sub.last_known_amount:.2f}")
        if sub.next_billing_date:
            print(f"      Next bill: {sub.next_billing_date.isoformat()}")
        print(f"      Status:  {status}")
        print()

    print(_colored(f"  Total: {len(subs)} subscription(s)", _Colors.DIM))
    print()


def cmd_show_policy(args: argparse.Namespace) -> None:
    """
    Displays the current policy configuration.

    This makes the policy fully transparent to the user — they can
    see exactly what thresholds are being applied (PRD Section 8).
    """
    print()
    print(_colored("  ⚙️  Current Policy Configuration", _Colors.BOLD + _Colors.HEADER))
    print(_colored("─" * 55, _Colors.BLUE))
    print()

    print(f"  Price increase threshold:  {policy['auto_flag_threshold_pct']}%")
    print(f"  Ignore below amount:       ${policy['auto_ignore_below_amount']:.2f}")
    print(f"  Unused days threshold:     {policy['unused_days_threshold']} days")
    print(f"  Draft action on:           {', '.join(policy['draft_action_on'])}")
    print()
    print(_colored("  Edit config/policy.py to change these values.", _Colors.DIM))
    print()


def cmd_audit_log(args: argparse.Namespace) -> None:
    """
    Displays recent entries from the audit log.

    Demonstrates the audit trail requirement (PRD Section 9).
    """
    init_db()

    print()
    print(_colored("  📋 Audit Log (Recent Entries)", _Colors.BOLD + _Colors.HEADER))
    print(_colored("─" * 55, _Colors.BLUE))
    print()

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (args.limit,)
        ).fetchall()

        if not rows:
            print(_colored("  No audit entries yet.", _Colors.DIM))
            return

        for row in rows:
            ts = row["timestamp"]
            entity = row["entity_type"]
            action = row["action"]
            details = row["details"]

            print(f"  [{ts}] {_colored(entity.upper(), _Colors.CYAN)}: {action}")
            if args.verbose and details:
                try:
                    parsed = json.loads(details)
                    for k, v in parsed.items():
                        print(f"    {k}: {v}")
                except json.JSONDecodeError:
                    print(f"    {details}")
            print()

    finally:
        conn.close()

    print(_colored(f"  Showing {len(rows)} most recent entries.", _Colors.DIM))
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Subscription Watchdog CLI — standalone agent skills interface.

    This CLI provides direct access to individual agent capabilities
    independent of the full pipeline, satisfying PRD Section 10,
    Requirement 10.
    """
    parser = argparse.ArgumentParser(
        prog="agents_cli",
        description="Subscription Watchdog — Agent Skills CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # check-subscriptions
    check_parser = subparsers.add_parser(
        "check-subscriptions",
        help="Run the Decision Agent policy check on pending flags",
    )
    check_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include reasoning traces in output",
    )

    # list-subscriptions
    subparsers.add_parser(
        "list-subscriptions",
        help="List all tracked subscriptions",
    )

    # show-policy
    subparsers.add_parser(
        "show-policy",
        help="Display the current policy configuration",
    )

    # audit-log
    audit_parser = subparsers.add_parser(
        "audit-log",
        help="Show recent audit trail entries",
    )
    audit_parser.add_argument(
        "--limit", "-n", type=int, default=20,
        help="Number of entries to show (default: 20)",
    )
    audit_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include full details in output",
    )

    args = parser.parse_args()

    if args.command == "check-subscriptions":
        cmd_check_subscriptions(args)
    elif args.command == "list-subscriptions":
        cmd_list_subscriptions(args)
    elif args.command == "show-policy":
        cmd_show_policy(args)
    elif args.command == "audit-log":
        cmd_audit_log(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
