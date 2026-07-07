"""
mcp_servers/calendar_mcp.py — Google Calendar MCP Server for Subscription Watchdog

Purpose:
    A Model Context Protocol (MCP) server that exposes Google Calendar
    operations as tools consumable by the ADK agent pipeline. Specifically,
    it allows the Action Agent to create renewal reminders so the user has
    time to act before the next charge.

Tools exposed:
    1. create_reminder — Creates a calendar event as a reminder ahead of a
       subscription's next renewal date. Only invoked by the Action Agent
       after human approval.

Security design (PRD Section 9):
    - Uses the narrowest possible scope: calendar.events (create events only).
    - Only the Action Agent invokes this tool, and only after
      Draft.approved == True has been set by the Human Approval Gate.

OAuth setup:
    - Shares the same credentials.json as the Gmail MCP server.
    - Generates a separate calendar_token.json on first run.
    - Credentials path configurable via CALENDAR_CREDENTIALS_PATH env var.

PRD References:
    - Section 5, User Story 6 (calendar reminder ahead of renewal date)
    - Section 6.1 (high-level flow — Calendar MCP reminder)
    - Section 6.2 (Action Agent — Calendar MCP event)
    - Section 9 (least-privilege scopes)
    - Section 10, Requirement 8 (create Calendar MCP reminder)
    - Section 12 (MCP Server evaluation concept)
"""

import os
import json
from datetime import datetime, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP  # pyrefly: ignore [missing-import]

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build  # pyrefly: ignore [missing-import]

# ---------------------------------------------------------------------------
# OAuth Scopes — narrowest scope for event creation (PRD Section 9)
# ---------------------------------------------------------------------------

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# ---------------------------------------------------------------------------
# Credential paths (configurable via environment)
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = os.getenv("CALENDAR_CREDENTIALS_PATH", "credentials.json")
CALENDAR_TOKEN_PATH = os.getenv("CALENDAR_TOKEN_PATH", "calendar_token.json")

# Default number of days before the renewal date to set the reminder.
# E.g., 7 means the reminder fires 7 days before the charge.
DEFAULT_REMINDER_DAYS_BEFORE = 7

# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "calendar-mcp",
    instructions="Google Calendar MCP Server — provides reminder creation for Subscription Watchdog",
)


def _get_calendar_service():
    """
    Builds and returns an authenticated Google Calendar API service client.

    Handles the OAuth2 flow:
      1. Loads existing token from calendar_token.json if available.
      2. Refreshes expired tokens automatically.
      3. Falls back to browser-based consent flow on first run.

    Uses a separate token file from Gmail to maintain clean scope separation.
    """
    creds = None

    if os.path.exists(CALENDAR_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(CALENDAR_TOKEN_PATH, CALENDAR_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"OAuth credentials file not found at '{CREDENTIALS_PATH}'. "
                    "Download it from the Google Cloud Console and place it in "
                    "the project root, or set CALENDAR_CREDENTIALS_PATH env var."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist the token for subsequent runs
        with open(CALENDAR_TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_reminder(
    merchant: str,
    renewal_date: str,
    amount: float,
    currency: str = "USD",
    days_before: int = DEFAULT_REMINDER_DAYS_BEFORE,
) -> str:
    """
    Creates a Google Calendar event as a reminder ahead of a subscription renewal.

    This fulfills PRD User Story 6: "Get a calendar reminder ahead of a renewal
    date so I have time to act before I'm charged again."

    SECURITY: This tool is ONLY to be called by the Action Agent, and ONLY
    after the Human Approval Gate has set Draft.approved = True.

    Args:
        merchant:     Name of the subscription service (e.g., "Netflix").
        renewal_date: The next billing date in ISO format (YYYY-MM-DD).
        amount:       The expected charge amount.
        currency:     Currency code (default: "USD").
        days_before:  How many days before the renewal to set the reminder.
                      Default is 7 days.

    Returns:
        JSON string with the created event ID on success, or an error message.
    """
    try:
        service = _get_calendar_service()

        # Parse the renewal date and calculate the reminder date
        renewal = datetime.strptime(renewal_date, "%Y-%m-%d")
        reminder_date = renewal - timedelta(days=days_before)

        # If the reminder date is in the past, set it to today
        if reminder_date < datetime.now():
            reminder_date = datetime.now()

        # Build the calendar event
        event = {
            "summary": f"⚠️ Subscription Renewal: {merchant}",
            "description": (
                f"Your {merchant} subscription renews on {renewal_date} "
                f"for {currency} {amount:.2f}.\n\n"
                f"This reminder was created by Subscription Watchdog to give "
                f"you time to review or cancel before the charge."
            ),
            "start": {
                "date": reminder_date.strftime("%Y-%m-%d"),
            },
            "end": {
                "date": reminder_date.strftime("%Y-%m-%d"),
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    # Pop-up reminder on the morning of the reminder day
                    {"method": "popup", "minutes": 0},
                    # Email reminder 1 day before the reminder day
                    {"method": "email", "minutes": 1440},
                ],
            },
        }

        # Insert the event into the user's primary calendar
        created = service.events().insert(  # type: ignore[attr-defined]
            calendarId="primary", body=event
        ).execute()

        return json.dumps({
            "status": "created",
            "event_id": created.get("id", ""),
            "reminder_date": reminder_date.strftime("%Y-%m-%d"),
            "renewal_date": renewal_date,
            "merchant": merchant,
        })

    except FileNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Calendar API error: {str(e)}",
        })


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run the MCP server over stdio transport (standard for MCP)
    mcp.run(transport="stdio")
