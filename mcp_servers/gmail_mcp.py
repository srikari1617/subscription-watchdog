"""
mcp_servers/gmail_mcp.py — Gmail MCP Server for Subscription Watchdog

Purpose:
    A Model Context Protocol (MCP) server that exposes Gmail operations as
    tools consumable by the ADK agent pipeline. This is the real-world
    integration point for email scanning and sending.

Tools exposed:
    1. search_messages — Searches Gmail for subscription-related emails within
       a configurable lookback window. Uses READ-ONLY scope.
    2. send_message    — Sends an approved email on behalf of the user.
       Uses SEND scope. Only invoked by the Action Agent post-approval.

Security design (PRD Section 9):
    - Least-privilege scopes: search_messages uses gmail.readonly,
      send_message uses gmail.send. These are the narrowest scopes possible.
    - The Scanner Agent only has access to search_messages (read-only).
    - The Action Agent is the ONLY consumer of send_message, and it
      requires Draft.approved == True before invoking it.
    - Raw email bodies are returned for extraction but MUST be discarded
      by the Scanner Agent after structured field extraction.

OAuth setup:
    - Expects a credentials.json (OAuth client) in the project root.
    - Generates a token.json on first run via browser-based consent flow.
    - Credentials path is configurable via GMAIL_CREDENTIALS_PATH env var.

PRD References:
    - Section 6.1 (high-level flow — Gmail MCP read + send)
    - Section 6.2 (Scanner Agent — search_messages; Action Agent — send_message)
    - Section 9 (least-privilege MCP scopes)
    - Section 10, Requirements 1, 8
    - Section 12 (MCP Server evaluation concept)
"""

import os
import json
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Optional

from mcp.server.fastmcp import FastMCP  # pyrefly: ignore [missing-import]

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build  # pyrefly: ignore [missing-import]

# ---------------------------------------------------------------------------
# OAuth Scopes — deliberately separated for least-privilege (PRD Section 9)
# ---------------------------------------------------------------------------

# Read-only scope: used by search_messages (Scanner Agent)
READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Send scope: used by send_message (Action Agent only, post-approval)
SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Combined scopes for the OAuth consent flow (requested once at setup)
ALL_SCOPES = READONLY_SCOPES + SEND_SCOPES

# ---------------------------------------------------------------------------
# Credential paths (configurable via environment)
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "token.json")

# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "gmail-mcp",
    instructions="Gmail MCP Server — provides email search and send tools for Subscription Watchdog",
)


def _get_gmail_service():
    """
    Builds and returns an authenticated Gmail API service client.

    Handles the OAuth2 flow:
      1. Loads existing token from token.json if available.
      2. Refreshes expired tokens automatically.
      3. Falls back to browser-based consent flow on first run.

    The token file is stored locally and excluded via .gitignore
    to prevent accidental credential leakage (PRD Section 9).
    """
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, ALL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"OAuth credentials file not found at '{CREDENTIALS_PATH}'. "
                    "Download it from the Google Cloud Console and place it in "
                    "the project root, or set GMAIL_CREDENTIALS_PATH env var."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, ALL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist the token for subsequent runs
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_messages(
    query: str = "category:purchases OR subject:(receipt OR renewal OR subscription)",
    lookback_days: int = 90,
    max_results: int = 50,
) -> str:
    """
    Searches the user's Gmail inbox for subscription-related emails.

    This tool is used by the Scanner Agent to find receipts, renewal notices,
    and subscription confirmations. It returns structured metadata — the
    Scanner Agent is responsible for extracting fields and discarding the
    raw body (data minimization, PRD Section 9).

    Args:
        query:         Gmail search query string. Defaults to the PRD-specified
                       query targeting purchases, receipts, and renewals.
        lookback_days: Number of days to look back. Default 90 (PRD Section 6.2).
        max_results:   Maximum number of messages to return.

    Returns:
        JSON string containing a list of message objects with id, subject,
        sender, date, and body snippet.
    """
    try:
        service = _get_gmail_service()

        # Add date filter to enforce the lookback window
        after_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
        full_query = f"({query}) after:{after_date}"

        # Search for matching messages
        results = service.users().messages().list(  # type: ignore[attr-defined]
            userId="me", q=full_query, maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return json.dumps({"messages": [], "count": 0})

        # Fetch details for each message
        extracted = []
        for msg_ref in messages:
            msg = service.users().messages().get(  # type: ignore[attr-defined]
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

            extracted.append({
                "id": msg["id"],
                "subject": headers.get("Subject", ""),
                "sender": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })

        return json.dumps({"messages": extracted, "count": len(extracted)})

    except FileNotFoundError as e:
        return json.dumps({"error": str(e), "messages": [], "count": 0})
    except Exception as e:
        return json.dumps({"error": f"Gmail API error: {str(e)}", "messages": [], "count": 0})


@mcp.tool()
def send_message(
    to: str,
    subject: str,
    body: str,
) -> str:
    """
    Sends an email via the user's Gmail account.

    SECURITY: This tool is ONLY to be called by the Action Agent, and ONLY
    after the Human Approval Gate has set Draft.approved = True. The Action
    Agent must assert this condition before invoking this tool.

    This tool uses the gmail.send scope — the narrowest scope that allows
    sending (PRD Section 9, least-privilege).

    Args:
        to:      Recipient email address.
        subject: Email subject line.
        body:    Plain-text email body.

    Returns:
        JSON string with the sent message ID on success, or an error message.
    """
    try:
        service = _get_gmail_service()

        # Construct the email
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        # Send via Gmail API
        sent = service.users().messages().send(  # type: ignore[attr-defined]
            userId="me", body={"raw": raw}
        ).execute()

        return json.dumps({
            "status": "sent",
            "message_id": sent.get("id", ""),
        })

    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Failed to send email: {str(e)}",
        })


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run the MCP server over stdio transport (standard for MCP)
    mcp.run(transport="stdio")
