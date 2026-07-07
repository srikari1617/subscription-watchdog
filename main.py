"""
main.py — Main Entry Point for Subscription Watchdog

Purpose:
    The primary entry point for the Subscription Watchdog pipeline.
    Designed to run as a scheduled job (e.g., daily cron trigger or
    Cloud Function) rather than requiring manual invocation.

Execution flow:
    1. Load environment variables from .env
    2. Initialize the database
    3. Optionally fetch emails via Gmail MCP
    4. Run the orchestrator pipeline (Scanner → Comparator → Decision → Drafting)
    5. Present drafts to the Human Approval Gate
    6. Execute approved drafts via the Action Agent

Design decisions:
    - The pipeline and approval gate are intentionally separate steps.
      In a scheduled/cron deployment, step 5 (approval) would be
      handled asynchronously (e.g., via a web hook or queued notification).
      For the hackathon demo, it runs interactively in the CLI.
    - Supports --no-interactive flag for unattended pipeline runs that
      generate drafts without prompting for approval (drafts are stored
      for later review).

PRD References:
    - Section 6.1 (high-level flow)
    - Section 11 (deployability — designed for scheduled execution)
    - Section 13 (milestones — build order)
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Load environment variables from .env file before any other imports
# that might need API keys
from dotenv import load_dotenv
load_dotenv()

from core.database import init_db
from agents.orchestrator import run_pipeline_sync, PipelineState
from human_approval import review_drafts, run_approval_and_execute


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
# Pipeline display
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    """Prints the Subscription Watchdog startup banner."""
    print()
    print(_colored("=" * 60, _Colors.BLUE))
    print(_colored("  🐕 Subscription Watchdog", _Colors.BOLD + _Colors.HEADER))
    print(_colored("  Multi-Agent Personal Finance Assistant", _Colors.DIM))
    print(_colored("=" * 60, _Colors.BLUE))
    print()


def _print_pipeline_summary(state: PipelineState) -> None:
    """Prints a summary of the pipeline run results."""
    print()
    print(_colored("─" * 60, _Colors.BLUE))
    print(_colored("  Pipeline Summary", _Colors.BOLD))
    print(_colored("─" * 60, _Colors.BLUE))

    print(f"  📬 Subscriptions found:  {len(state.subscriptions)}")
    for sub in state.subscriptions:
        print(f"     • {sub.merchant}: {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}")

    print(f"  🚩 Flags raised:         {len(state.flags)}")
    for flag in state.flags:
        print(f"     • {flag.subscription.merchant}: {flag.reason} ({flag.severity})")

    print(f"  ⬆️  Escalated to draft:   {len(state.escalated_flags)}")
    print(f"  ✉️  Drafts generated:     {len(state.drafts)}")

    if state.errors:
        print(_colored(f"  ⚠ Errors: {len(state.errors)}", _Colors.RED))
        for err in state.errors:
            print(_colored(f"     • {err}", _Colors.RED))

    print(f"  📍 Final stage:          {state.current_stage}")
    print(_colored("─" * 60, _Colors.BLUE))
    print()


# ---------------------------------------------------------------------------
# Gmail MCP integration
# ---------------------------------------------------------------------------

def _fetch_gmail_data(lookback_days: int) -> str | None:
    """
    Attempts to fetch email data from the Gmail MCP server.

    Returns the JSON search results, or None if Gmail MCP is
    not available (falls back to sample data).
    """
    try:
        from mcp_servers.gmail_mcp import search_messages
        result = search_messages(lookback_days=lookback_days)
        data = json.loads(result)

        if data.get("error"):
            print(_colored(
                f"  ⚠ Gmail MCP error: {data['error']}", _Colors.YELLOW
            ))
            print(_colored(
                "  ℹ Falling back to sample email data.", _Colors.DIM
            ))
            return None

        print(_colored(
            f"  ✅ Gmail MCP: {data.get('count', 0)} emails fetched.",
            _Colors.GREEN,
        ))
        return result

    except Exception as e:
        print(_colored(
            f"  ⚠ Gmail MCP unavailable: {str(e)}", _Colors.YELLOW
        ))
        print(_colored(
            "  ℹ Falling back to sample email data.", _Colors.DIM
        ))
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point for Subscription Watchdog.

    Usage:
        python main.py                    # Interactive mode (default)
        python main.py --no-interactive   # Generate drafts only, no approval
        python main.py --lookback 30      # Custom lookback window
        python main.py --use-sample-data  # Force sample data (skip Gmail)
    """
    parser = argparse.ArgumentParser(
        description="Subscription Watchdog — Multi-Agent Personal Finance Assistant",
    )
    parser.add_argument(
        "--lookback", type=int, default=90,
        help="Number of days to look back for subscription emails (default: 90)",
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help="Run pipeline only, skip the interactive approval gate",
    )
    parser.add_argument(
        "--use-sample-data", action="store_true",
        help="Force use of sample email data instead of Gmail MCP",
    )
    args = parser.parse_args()

    _print_banner()

    # --- Step 1: Initialize ---
    print(_colored("  Initializing database...", _Colors.DIM))
    init_db()
    print(_colored("  ✅ Database ready.", _Colors.GREEN))

    # --- Step 2: Fetch Gmail data (or fall back to samples) ---
    gmail_data = None
    if not args.use_sample_data:
        print(_colored("  Connecting to Gmail MCP...", _Colors.DIM))
        gmail_data = _fetch_gmail_data(args.lookback)
    else:
        print(_colored("  ℹ Using sample email data (--use-sample-data).", _Colors.YELLOW))

    # --- Step 3: Run the pipeline ---
    print()
    print(_colored("  Running agent pipeline...", _Colors.BOLD))
    print(_colored("  Scanner → Comparator → Decision → Drafting", _Colors.DIM))
    print()

    state = run_pipeline_sync(
        lookback_days=args.lookback,
        gmail_search_results=gmail_data,
    )

    _print_pipeline_summary(state)

    # --- Step 4: Human Approval Gate ---
    if state.drafts and not args.no_interactive:
        results = run_approval_and_execute(state.drafts)
    elif state.drafts and args.no_interactive:
        print(_colored(
            f"  ℹ {len(state.drafts)} draft(s) generated. "
            "Run without --no-interactive to review and approve.",
            _Colors.YELLOW,
        ))
    else:
        print(_colored("  ✅ No action required. All subscriptions look good!", _Colors.GREEN))

    print()
    print(_colored("  🐕 Subscription Watchdog complete.", _Colors.BOLD + _Colors.GREEN))
    print()


if __name__ == "__main__":
    main()
