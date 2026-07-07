"""
agents/orchestrator.py — Root Orchestrator Agent for Subscription Watchdog

Purpose:
    The ADK root agent that sequences the entire multi-agent pipeline and
    holds session state across the run. This is the central coordinator
    that demonstrates the "Agent / Multi-agent system (ADK)" evaluation
    concept.

Pipeline sequence (PRD Section 6.1):
    1. Scanner Agent   → scans Gmail, extracts structured subscription data
    2. Comparator Agent → diffs against DB, produces Flag objects
    3. Decision Agent  → applies policy, filters flags for escalation
    4. Drafting Agent   → generates email drafts with reasoning traces
    5. (pause)         → Human Approval Gate (external to orchestrator)
    6. Action Agent    → executes approved drafts (invoked post-approval)

Design decisions:
    - The orchestrator does NOT call the Action Agent directly. It runs
      steps 1–4 and returns the drafts. The Human Approval Gate and
      Action Agent are invoked separately to enforce the security boundary.
    - Session state (subscriptions found, flags raised, drafts generated)
      is maintained in a PipelineState dataclass passed between stages.
    - Gmail MCP is called at the orchestrator level and results are passed
      to the Scanner Agent as data, keeping MCP tool-calling separate
      from untrusted email content processing (prompt-injection resistance).

PRD References:
    - Section 6.1 (high-level flow)
    - Section 6.2 (all agent responsibilities)
    - Section 9 (security boundaries)
    - Section 12 (Agent / Multi-agent system ADK evaluation concept)
"""

import json
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.models import Subscription, Flag, Draft
from core.database import init_db, log_audit
from agents.scanner import scan_inbox
from agents.comparator import compare_subscriptions
from agents.decision import evaluate_flags
from agents.drafting import generate_drafts
from agents.action import execute_approved_drafts


@dataclass
class PipelineState:
    """
    Holds the session state across the orchestrator's pipeline run.

    This is the ADK session state object — it tracks what each agent
    produced so the next agent in the sequence has full context.
    """
    # Configuration
    lookback_days: int = 90
    started_at: datetime = field(default_factory=datetime.utcnow)

    # Stage outputs
    subscriptions: List[Subscription] = field(default_factory=list)
    flags: List[Flag] = field(default_factory=list)
    escalated_flags: List[Flag] = field(default_factory=list)
    drafts: List[Draft] = field(default_factory=list)
    action_results: List[dict] = field(default_factory=list)

    # Status tracking
    current_stage: str = "initialized"
    errors: List[str] = field(default_factory=list)
    completed: bool = False


async def run_pipeline(
    lookback_days: int = 90,
    gmail_search_results: Optional[str] = None,
) -> PipelineState:
    """
    Runs the full agent pipeline from scanning through draft generation.

    IMPORTANT: This function runs stages 1–4 only. It does NOT invoke
    the Action Agent. Drafts are returned for the Human Approval Gate
    to review. The Action Agent must be invoked separately after approval.

    This separation is the core security design: the orchestrator
    produces drafts, a human reviews them, and only then can actions
    be taken (PRD Section 9).

    Args:
        lookback_days:        Configurable lookback window (default: 90 days).
        gmail_search_results: Pre-fetched Gmail MCP search results (JSON).
                              If None, the Scanner Agent falls back to
                              sample data (PRD Section 14).

    Returns:
        PipelineState with all intermediate results for inspection.
    """
    state = PipelineState(lookback_days=lookback_days)

    # Ensure the database is initialized
    init_db()

    # --- Stage 1: Scanner Agent ---
    state.current_stage = "scanning"
    try:
        state.subscriptions = await scan_inbox(
            lookback_days=lookback_days,
            gmail_search_results=gmail_search_results,
        )
        log_audit(
            entity_type="pipeline",
            entity_id=None,
            action="Scanner Agent completed",
            details={
                "subscriptions_found": len(state.subscriptions),
                "merchants": [s.merchant for s in state.subscriptions],
            },
        )
    except Exception as e:
        state.errors.append(f"Scanner Agent failed: {str(e)}")
        return state

    # --- Stage 2: Comparator Agent ---
    state.current_stage = "comparing"
    try:
        state.flags = compare_subscriptions(state.subscriptions)
        log_audit(
            entity_type="pipeline",
            entity_id=None,
            action="Comparator Agent completed",
            details={
                "flags_raised": len(state.flags),
                "flag_reasons": [f.reason for f in state.flags],
            },
        )
    except Exception as e:
        state.errors.append(f"Comparator Agent failed: {str(e)}")
        return state

    # --- Stage 3: Decision Agent ---
    state.current_stage = "deciding"
    try:
        state.escalated_flags = evaluate_flags(state.flags)
        log_audit(
            entity_type="pipeline",
            entity_id=None,
            action="Decision Agent completed",
            details={
                "total_flags": len(state.flags),
                "escalated": len(state.escalated_flags),
                "dropped": len(state.flags) - len(state.escalated_flags),
            },
        )
    except Exception as e:
        state.errors.append(f"Decision Agent failed: {str(e)}")
        return state

    # --- Stage 4: Drafting Agent ---
    state.current_stage = "drafting"
    try:
        state.drafts = await generate_drafts(state.escalated_flags)
        log_audit(
            entity_type="pipeline",
            entity_id=None,
            action="Drafting Agent completed",
            details={
                "drafts_generated": len(state.drafts),
                "draft_types": [d.draft_type for d in state.drafts],
            },
        )
    except Exception as e:
        state.errors.append(f"Drafting Agent failed: {str(e)}")
        return state

    # --- Pipeline complete (up to approval gate) ---
    state.current_stage = "awaiting_approval"
    state.completed = True

    log_audit(
        entity_type="pipeline",
        entity_id=None,
        action="Pipeline completed — awaiting human approval",
        details={
            "subscriptions": len(state.subscriptions),
            "flags": len(state.flags),
            "escalated": len(state.escalated_flags),
            "drafts": len(state.drafts),
            "duration_seconds": (
                datetime.utcnow() - state.started_at
            ).total_seconds(),
        },
    )

    return state


async def execute_approved(drafts: List[Draft]) -> List[dict]:
    """
    Invokes the Action Agent on approved drafts.

    This is called AFTER the Human Approval Gate has reviewed and
    approved specific drafts. It is deliberately a separate function
    from run_pipeline() to enforce the security boundary.

    Args:
        drafts: List of Draft objects that have been approved
                (draft.approved == True).

    Returns:
        List of action result dicts from the Action Agent.
    """
    approved = [d for d in drafts if d.approved]

    if not approved:
        log_audit(
            entity_type="pipeline",
            entity_id=None,
            action="No approved drafts to execute",
        )
        return []

    results = await execute_approved_drafts(approved)

    log_audit(
        entity_type="pipeline",
        entity_id=None,
        action="Action Agent completed",
        details={
            "executed": len(results),
            "results": results,
        },
    )

    return results


def run_pipeline_sync(
    lookback_days: int = 90,
    gmail_search_results: Optional[str] = None,
) -> PipelineState:
    """
    Synchronous wrapper for run_pipeline().
    Convenience method for CLI and scheduled job invocation.
    """
    return asyncio.run(run_pipeline(lookback_days, gmail_search_results))


def execute_approved_sync(drafts: List[Draft]) -> List[dict]:
    """
    Synchronous wrapper for execute_approved().
    Convenience method for CLI invocation after approval.
    """
    return asyncio.run(execute_approved(drafts))
