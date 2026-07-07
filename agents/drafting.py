"""
agents/drafting.py — Drafting Agent for Subscription Watchdog

Purpose:
    Fourth agent in the pipeline. For each escalated Flag, generates either
    a cancellation email or a price-match negotiation email. Every draft
    includes a reasoning trace surfaced to the human reviewer.

Responsibilities (PRD Section 6.2 — Drafting Agent):
    - Generates a cancellation email for "unused" flags.
    - Generates a price-match negotiation email for "price_increase" flags.
    - Every draft includes a reasoning trace ("why was this generated")
      that is surfaced to the human reviewer.
    - Structurally CANNOT call any send function directly — its only
      output type is a Draft object.

Security design (PRD Section 9):
    - The Drafting Agent has NO access to Gmail send or Calendar tools.
    - It produces Draft objects only. The Draft.approved field defaults
      to False and can only be set by the Human Approval Gate.
    - This structural separation is enforced by the fact that this module
      does not import any MCP server code.

PRD References:
    - Section 6.2 (Drafting Agent)
    - Section 9 (structural separation — cannot call send)
    - Section 10, Requirement 6 (human-readable draft + reasoning trace)
    - Section 10, Requirement 7 (drafts presented to human approval gate)
"""

import json
import os
from typing import List

from google import genai
from google.genai import types
import groq

from core.models import Flag, Draft
from core.database import log_audit


# ---------------------------------------------------------------------------
# LLM Prompts for draft generation
# ---------------------------------------------------------------------------

CANCELLATION_SYSTEM_PROMPT = """You are a polite, professional email writer.
You will be given details about a subscription the user wants to cancel.

Write a clear, concise cancellation request email. The email should:
1. State the intent to cancel the subscription immediately.
2. Request confirmation of the cancellation.
3. Ask that no further charges be applied.
4. Be professional and courteous but firm.

Return ONLY the email body text. No subject line, no greetings preamble,
no markdown formatting. Start directly with the salutation (e.g., "Dear...")."""

NEGOTIATION_SYSTEM_PROMPT = """You are a polite, professional email writer.
You will be given details about a subscription whose price has increased.

Write a clear, concise price negotiation email. The email should:
1. Acknowledge the price change.
2. State that the user has been a loyal customer.
3. Ask whether the previous rate can be maintained or a discount offered.
4. Mention willingness to consider alternatives if the price cannot be adjusted.
5. Be professional and courteous.

Return ONLY the email body text. No subject line, no greetings preamble,
no markdown formatting. Start directly with the salutation (e.g., "Dear...")."""


async def generate_drafts(flags: List[Flag]) -> List[Draft]:
    """
    Generates Draft objects for each escalated Flag.

    This is the main entry point for the Drafting Agent.

    Args:
        flags: List of escalated Flag objects from the Decision Agent.

    Returns:
        List of Draft objects. Each draft has approved=False by default.
        The Drafting Agent structurally cannot set approved=True or
        call any send function.
    """
    drafts: List[Draft] = []

    for flag in flags:
        if flag.reason == "unused":
            draft = await _generate_cancellation_draft(flag)
        elif flag.reason == "price_increase":
            draft = await _generate_negotiation_draft(flag)
        else:
            # For any other reason, default to a cancellation draft
            draft = await _generate_cancellation_draft(flag)

        drafts.append(draft)

        # Log draft creation to audit trail (PRD Section 9)
        log_audit(
            entity_type="draft",
            entity_id=draft.id,
            action=f"Draft created: {draft.draft_type}",
            details={
                "merchant": flag.subscription.merchant,
                "draft_type": draft.draft_type,
                "flag_reason": flag.reason,
                "reasoning_trace": flag.reasoning_trace,
            },
        )

    return drafts


async def _generate_cancellation_draft(flag: Flag) -> Draft:
    """
    Generates a cancellation email draft for an unused subscription.

    The LLM generates the email body, but the Draft object is the ONLY
    output — no send functions are called or available.
    """
    sub = flag.subscription

    user_prompt = (
        f"Write a cancellation email for the following subscription:\n"
        f"- Service: {sub.merchant}\n"
        f"- Current price: {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}\n"
        f"- Reason for cancellation: {flag.reasoning_trace}\n"
    )

    body = await _call_llm(CANCELLATION_SYSTEM_PROMPT, user_prompt)

    return Draft(
        flag=flag,
        draft_type="cancellation",
        subject=f"Cancellation Request — {sub.merchant} Subscription",
        to_address="",  # To be filled by user during approval
        body=body,
        approved=False,  # SECURITY: only Human Approval Gate sets True
    )


async def _generate_negotiation_draft(flag: Flag) -> Draft:
    """
    Generates a price negotiation email draft for a price-increased subscription.

    The LLM generates the email body, but the Draft object is the ONLY
    output — no send functions are called or available.
    """
    sub = flag.subscription

    user_prompt = (
        f"Write a price negotiation email for the following subscription:\n"
        f"- Service: {sub.merchant}\n"
        f"- Previous price: {sub.currency} {sub.last_known_amount:.2f}/{sub.billing_cycle}\n"
        f"- New price: {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}\n"
        f"- Price increase: {sub.currency} {sub.amount - sub.last_known_amount:.2f}\n"
        f"- Details: {flag.reasoning_trace}\n"
    )

    body = await _call_llm(NEGOTIATION_SYSTEM_PROMPT, user_prompt)

    return Draft(
        flag=flag,
        draft_type="negotiation",
        subject=f"Regarding Recent Price Change — {sub.merchant} Subscription",
        to_address="",  # To be filled by user during approval
        body=body,
        approved=False,  # SECURITY: only Human Approval Gate sets True
    )


async def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """
    Calls the LLM (Gemini or Groq Llama-3) to generate email content.

    This is a focused, single-purpose LLM call with no tool-calling
    capability — it can only generate text, reinforcing the structural
    separation between drafting and action.
    """
    try:
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            # Use Groq client
            client = groq.Groq(api_key=groq_key)
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
            )
            return (response.choices[0].message.content or "").strip()
        else:
            # Fall back to Gemini client
            client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7,  # Slight creativity for natural email tone
                ),
            )
            return (response.text or "").strip()
    except Exception as e:
        # Fallback: return a template if LLM is unavailable
        return (
            f"[Draft generation failed: {str(e)}]\n\n"
            f"Please write your email manually based on the reasoning trace "
            f"provided in the flag details."
        )
