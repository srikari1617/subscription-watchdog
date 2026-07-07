"""
app.py — Streamlit Visual Dashboard for Subscription Watchdog

Purpose:
    Provides a beautiful, interactive web application for Subscription Watchdog.
    Fulfills the PRD allowance for a "minimal console/Streamlit approval flow"
    by providing a visual alternative to the raw CLI interface.

Features:
    1. Run Scan      — Triggers the Orchestrator pipeline directly from the UI.
    2. Approval Gate — Interactive draft cards with email previews and
                       approve/reject buttons.
    3. Subscriptions — Displays active subscriptions with toggle buttons to
                       mark them as "Still Used" or "Not Used".
    4. Audit Log     — Renders a searchable table of all historical decisions.
    5. Policy View   — Displays current policy settings in the sidebar.

PRD References:
    - Section 3.2 (Streamlit approval flow is sufficient)
    - Section 5 (All user stories: list subscriptions, price alerts, review drafts)
    - Section 6.2 (Human Approval Gate, user-maintained "still using" list)
    - Section 9 (Security gate boundary)
"""

import streamlit as st
import pandas as pd
from datetime import date, datetime
import json
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from core.database import (
    init_db,
    get_all_subscriptions,
    get_connection,
    log_audit,
    upsert_subscription
)
from core.models import Subscription, Flag, Draft
from agents.orchestrator import run_pipeline
from agents.action import execute_approved_drafts
from config.policy import policy

# Page configuration
st.set_page_config(
    page_title="Subscription Watchdog",
    page_icon="🐕",
    layout="wide",
)

# Initialize database
init_db()

# ---------------------------------------------------------------------------
# Session State Initialization
# ---------------------------------------------------------------------------
if "drafts" not in st.session_state:
    st.session_state.drafts = []
if "pipeline_run" not in st.session_state:
    st.session_state.pipeline_run = False
if "messages" not in st.session_state:
    st.session_state.messages = []

# Helper to run async functions in Streamlit sync environment
def run_async(coro):
    return asyncio.run(coro)

# ---------------------------------------------------------------------------
# Sidebar UI (Policy & Actions)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.image("https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=300&q=80", use_container_width=True)
    st.title("🐕 Watchdog Control")
    st.markdown("---")
    
    st.subheader("⚙️ Policy Config")
    st.metric("Price Increase Threshold", f"{policy['auto_flag_threshold_pct']}%")
    st.metric("Ignore Below", f"${policy['auto_ignore_below_amount']:.2f}")
    st.metric("Dormancy Threshold", f"{policy['unused_days_threshold']} Days")
    st.caption("Policy is defined transparently in `config/policy.py`.")
    
    st.markdown("---")
    
    # Run Scanner Pipeline
    st.subheader("🏁 Run Analysis")
    use_sample = st.checkbox("Use Sample Email Data", value=True)
    
    if st.button("🔍 Scan Inbox Now", type="primary", use_container_width=True):
        with st.spinner("Pipeline running (Scanner ➜ Comparator ➜ Decision ➜ Drafting)..."):
            # Set search results
            gmail_data = None
            if not use_sample:
                try:
                    from mcp_servers.gmail_mcp import search_messages
                    gmail_data = search_messages(lookback_days=90)
                except Exception as e:
                    st.error(f"Gmail MCP unavailable: {e}")
            
            # Run orchestrator pipeline
            state = run_async(run_pipeline(lookback_days=90, gmail_search_results=gmail_data))
            
            if state.errors:
                for err in state.errors:
                    st.error(err)
            else:
                st.session_state.drafts = state.drafts
                st.session_state.pipeline_run = True
                st.session_state.messages.append((f"Scan complete at {datetime.now().strftime('%H:%M:%S')}. Found {len(state.subscriptions)} subscriptions, raised {len(state.flags)} flags.", "success"))
                st.rerun()

# ---------------------------------------------------------------------------
# Main Dashboard Tabs
# ---------------------------------------------------------------------------
st.title("🐕 Subscription Watchdog Dashboard")
st.markdown("Manage subscriptions, review auto-generated drafts, and check decisions audit logs.")

# Show messages/toasts
for msg, level in st.session_state.messages:
    if level == "success":
        st.success(msg)
    elif level == "info":
        st.info(msg)
st.session_state.messages = []

tab1, tab2, tab3 = st.tabs(["✉️ Approval Gate", "📦 Tracked Subscriptions", "📋 Audit Trail"])

# ---------------------------------------------------------------------------
# Tab 1: Human Approval Gate
# ---------------------------------------------------------------------------
with tab1:
    st.header("🛡️ Human Approval Gate")
    st.markdown("Review generated cancellation/negotiation drafts. No actions are taken until you approve them.")

    pending_drafts = [d for d in st.session_state.drafts if not d.approved]

    if not pending_drafts:
        st.info("No pending drafts. Run a new scan from the sidebar to check for subscription price increases or unused services.")
    else:
        for idx, draft in enumerate(pending_drafts):
            sub = draft.flag.subscription
            flag = draft.flag
            
            with st.container(border=True):
                col1, col2 = st.columns([1, 2])
                
                # Column 1: Details & Reasoning
                with col1:
                    st.subheader(f"{sub.merchant}")
                    st.markdown(f"**Draft Type:** `{draft.draft_type.upper()}`")
                    st.markdown(f"**Amount:** {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}")
                    if sub.last_known_amount > 0:
                        st.markdown(f"**Previous:** {sub.currency} {sub.last_known_amount:.2f}")
                    
                    st.markdown("##### 🔍 Flag Reasoning")
                    st.warning(flag.reasoning_trace)
                
                # Column 2: Email Draft Editor & Approval
                with col2:
                    st.markdown("##### ✉️ Edit & Approve Draft")
                    
                    # Pre-fill support email address if empty
                    default_to = draft.to_address if draft.to_address else f"support@{sub.merchant.lower().replace(' ', '')}.com"
                    to_addr = st.text_input("To Address", value=default_to, key=f"to_{idx}")
                    subject_line = st.text_input("Subject", value=draft.subject, key=f"subj_{idx}")
                    email_body = st.text_area("Email Body", value=draft.body, height=180, key=f"body_{idx}")
                    
                    col_b1, col_b2, _ = st.columns([1, 1, 2])
                    
                    # Approve & Send Action
                    if col_b1.button("✅ Approve & Send", key=f"app_{idx}", type="primary"):
                        draft.to_address = to_addr
                        draft.subject = subject_line
                        draft.body = email_body
                        
                        # === SECURITY BOUNDARY: Set Approved = True ===
                        draft.approved = True
                        
                        log_audit(
                            entity_type="approval",
                            entity_id=draft.flag.id,
                            action="Draft APPROVED by human via Web UI",
                            details={"merchant": sub.merchant, "to": to_addr}
                        )
                        
                        with st.spinner("Executing action via MCP..."):
                            results = run_async(execute_approved_drafts([draft]))
                            res = results[0] if results else {}
                            
                            if res.get("email_sent"):
                                st.session_state.messages.append((f"Success! Email sent to {to_addr} for {sub.merchant}.", "success"))
                            if res.get("reminder_created"):
                                st.session_state.messages.append((f"Renewal reminder calendar event created for {sub.merchant}.", "success"))
                            if res.get("errors"):
                                for err in res["errors"]:
                                    st.error(f"Action error: {err}")
                                    
                        st.session_state.drafts.remove(draft)
                        st.rerun()
                    
                    # Reject Action
                    if col_btn2 := col_b2.button("❌ Reject Draft", key=f"rej_{idx}"):
                        log_audit(
                            entity_type="approval",
                            entity_id=draft.flag.id,
                            action="Draft REJECTED by human via Web UI",
                            details={"merchant": sub.merchant}
                        )
                        st.session_state.drafts.remove(draft)
                        st.session_state.messages.append((f"Draft for {sub.merchant} rejected and discarded.", "info"))
                        st.rerun()

# ---------------------------------------------------------------------------
# Tab 2: Subscriptions list & user "still using" status configuration
# ---------------------------------------------------------------------------
with tab2:
    st.header("📦 Tracked Subscriptions")
    st.markdown("User-editable 'still using' list. Update status here to direct the Comparator Agent's checks.")
    
    subs = get_all_subscriptions()
    
    if not subs:
        st.info("No subscriptions tracked. Run a scan first to build the database.")
    else:
        # Table list
        for sub in subs:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                
                c1.markdown(f"### {sub.merchant}")
                c2.markdown(f"**Amount:** {sub.currency} {sub.amount:.2f}/{sub.billing_cycle}")
                
                # Usage status
                status_text = "✅ Still Using" if sub.still_used is True else (
                    "❌ Not Using" if sub.still_used is False else "❓ Unknown"
                )
                c3.markdown(f"**Usage Status:** `{status_text}`")
                
                # Actions to edit usage list
                col_btn1, col_btn2 = c4.columns(2)
                if col_btn1.button("Mark In Use", key=f"use_{sub.id}", use_container_width=True):
                    sub.still_used = True
                    upsert_subscription(sub)
                    log_audit("subscription", sub.id, f"Marked {sub.merchant} as STILL IN USE")
                    st.rerun()
                if col_btn2.button("Mark Unused", key=f"unuse_{sub.id}", use_container_width=True):
                    sub.still_used = False
                    upsert_subscription(sub)
                    log_audit("subscription", sub.id, f"Marked {sub.merchant} as NOT IN USE")
                    st.rerun()

# ---------------------------------------------------------------------------
# Tab 3: Audit Log Table
# ---------------------------------------------------------------------------
with tab3:
    st.header("📋 Audit Trail")
    st.markdown("Immutable record of decisions, flags, approvals, and actions taken by the agents.")
    
    conn = get_connection()
    try:
        df = pd.read_sql_query("SELECT timestamp, entity_type, action, details FROM audit_log ORDER BY timestamp DESC", conn)
        if df.empty:
            st.info("Audit log is currently empty.")
        else:
            # Simple clean columns
            df.columns = ["Timestamp", "Entity", "Action", "Details JSON"]
            st.dataframe(df, use_container_width=True)
    finally:
        conn.close()
