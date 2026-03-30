"""
fetchers.py — Provider-agnostic email fetching via OAuth2.

Each fetcher authenticates with its provider using OAuth2 and returns
newsletters in a normalised format that the rest of the agent can use
without knowing anything about the underlying email provider.

Normalised newsletter dict:
{
    "id":        str,   # unique message ID (used for dedup)
    "subject":   str,
    "sender":    str,   # email address of sender
    "received":  str,   # ISO 8601 datetime string
    "body_html": str,   # raw HTML body
}

Supported providers:
    - OutlookFetcher  (Hotmail, Outlook, Live) via Microsoft Graph API
    - GmailFetcher    (Gmail) via Gmail REST API

Adding a new provider:
    1. Subclass EmailFetcher
    2. Implement authenticate() and fetch_newsletters()
    3. Register it in FETCHER_REGISTRY at the bottom of this file
"""

import logging
import os
import json
import requests
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import msal
from msal import PublicClientApplication
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
import base64

log = logging.getLogger(__name__)

BASE_DIR       = Path(__file__).parent
TOKEN_DIR      = BASE_DIR / "tokens"
TOKEN_DIR.mkdir(exist_ok=True)


# ── Abstract base ──────────────────────────────────────────────────────────────
class EmailFetcher(ABC):
    """
    Base class for all email fetchers.
    Subclasses must implement authenticate() and fetch_newsletters().
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.newsletter_domains  = cfg.get("newsletter_domains", [])
        self.newsletter_keywords = cfg.get("newsletter_keywords", [])

    @abstractmethod
    def authenticate(self):
        """Authenticate with the email provider and store credentials."""
        pass

    @abstractmethod
    def fetch_newsletters(self) -> list[dict]:
        """
        Fetch unread newsletters from today and return them in
        the normalised format described at the top of this file.
        """
        pass

    def is_newsletter(self, sender: str, subject: str, body_html: str) -> bool:
        """
        Multi-signal scoring heuristic to decide if an email is a newsletter.
        Uses a point system — email must score >= 2 to be considered a newsletter.

        Positive signals (it IS a newsletter):
          +3 — sender domain matches a known newsletter platform
          +2 — subject matches a newsletter keyword
          +1 — body contains 'unsubscribe' link
          +1 — body contains 'view in browser' link
          +1 — body is long (> 1500 chars = likely editorial content)

        Negative signals (it is NOT a newsletter):
          -3 — subject contains calendar/event/webinar keywords
          -2 — sender is a known transactional/promotional domain
          -2 — subject contains promotional keywords
          -1 — body contains calendar invite markers
        """
        body_lower   = body_html.lower()
        subject_lower = subject.lower()
        score = 0

        # ── Positive signals ──────────────────────────────────────────────────
        # Known newsletter platform sender
        if any(domain in sender for domain in self.newsletter_domains):
            score += 3

        # Subject matches newsletter keywords
        if any(kw.lower() in subject_lower for kw in self.newsletter_keywords):
            score += 2

        # Body contains unsubscribe link
        if "unsubscribe" in body_lower:
            score += 1

        # Body contains view in browser link (common in newsletters)
        if "view in browser" in body_lower or "view online" in body_lower:
            score += 1

        # Long body = likely editorial content not a short transactional email
        if len(body_html) > 1500:
            score += 1

        # ── Negative signals ──────────────────────────────────────────────────
        # Calendar/event/webinar subject keywords
        event_keywords = [
            "webinar", "calendar", "meeting", "invite", "invitation",
            "zoom", "teams meeting", "google meet", "has been updated",
            "reminder", "rsvp", "register now", "join us", "event"
        ]
        if any(kw in subject_lower for kw in event_keywords):
            score -= 3

        # Promotional subject keywords
        promo_keywords = [
            "% off", "sale", "deal", "offer", "discount", "coupon",
            "limited time", "act now", "buy now", "shop now", "free shipping",
            "order confirmation", "your order", "receipt", "invoice",
            "payment", "subscription renewed", "your account"
        ]
        if any(kw in subject_lower for kw in promo_keywords):
            score -= 2

        # Known transactional/promotional sender domains
        transactional_domains = [
            "zoom.us", "zoomgov.com", "calendar.google.com",
            "noreply", "no-reply", "donotreply", "do-not-reply",
            "notifications@", "alerts@", "updates@", "billing@",
            "support@", "help@", "team@"
        ]
        if any(domain in sender for domain in transactional_domains):
            score -= 2

        # Calendar invite markers in body
        if any(marker in body_lower for marker in ["ical", "text/calendar", "begin:vcalendar"]):
            score -= 1

        log.debug(f"  Score {score:+d} | {subject[:60]}")
        return score >= 2


# ── Outlook / Hotmail fetcher ──────────────────────────────────────────────────
class OutlookFetcher(EmailFetcher):
    """
    Fetches newsletters from Hotmail/Outlook via Microsoft Graph API.
    Uses OAuth2 delegated auth flow (interactive login on first run,
    then silent token refresh on subsequent runs).

    Required config keys:
        ms_client_id, ms_tenant_id, email_address

    OAuth2 token is cached in tokens/outlook_token.json and refreshed
    automatically. Browser only opens on the very first run.
    """

    SCOPES     = ["https://graph.microsoft.com/Mail.Read"]
    TOKEN_FILE = TOKEN_DIR / "outlook_token.json"

    def _build_app(self) -> PublicClientApplication:
        """Build MSAL app with a persistent on-disk token cache."""
        cache = msal.SerializableTokenCache()

        # Load existing cache from disk if it exists
        if self.TOKEN_FILE.exists():
            cache.deserialize(self.TOKEN_FILE.read_text())

        app = PublicClientApplication(
            client_id=self.cfg["ms_client_id"],
            authority="https://login.microsoftonline.com/consumers",
            token_cache=cache,
        )

        # Save cache back to disk after every operation
        self._cache = cache
        return app

    def _save_cache(self):
        """Persist token cache to disk if it has changed."""
        if self._cache.has_state_changed:
            self.TOKEN_FILE.write_text(self._cache.serialize())
            log.info("  Token cache saved to disk.")

    def authenticate(self) -> str:
        log.info("Authenticating with Microsoft Graph (Outlook)...")

        app = self._build_app()

        # Try silent auth first using cached token
        accounts = app.get_accounts()
        result = None
        if accounts:
            log.info("  Found cached account, attempting silent auth...")
            result = app.acquire_token_silent(self.SCOPES, account=accounts[0])

        # If silent auth failed or no cached token, do interactive login
        if not result or "access_token" not in result:
            log.info("  Opening browser for Outlook authorisation (first time only)...")
            result = app.acquire_token_interactive(scopes=self.SCOPES)

        if "access_token" not in result:
            raise RuntimeError(
                f"Outlook auth failed: {result.get('error_description')}"
            )

        self._save_cache()
        self._token = result["access_token"]
        log.info("  Outlook authenticated successfully")
        return self._token

    def fetch_newsletters(self) -> list[dict]:
        token = self.authenticate()
        headers = {"Authorization": f"Bearer {token}"}

        since        = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        odata_filter = f"isRead eq false and receivedDateTime ge {since}"

        # Use /me endpoint — works with delegated auth for personal accounts
        url = (
            f"https://graph.microsoft.com/v1.0/me/messages"
            f"?$filter={requests.utils.quote(odata_filter)}"
            f"&$select=id,subject,from,receivedDateTime,body"
            f"&$top=50"
            f"&$orderby=receivedDateTime desc"
        )

        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 401:
            raise RuntimeError(
                f"401 Unauthorized. Response: {resp.text!r} Headers: {dict(resp.headers)}"
            )
        resp.raise_for_status()
        messages = resp.json().get("value", [])

        results = []
        for msg in messages:
            sender    = msg.get("from", {}).get("emailAddress", {}).get("address", "").lower()
            subject   = msg.get("subject", "")
            body_html = msg.get("body", {}).get("content", "")

            if self.is_newsletter(sender, subject, body_html):
                results.append({
                    "id":        msg["id"],
                    "subject":   subject,
                    "sender":    sender,
                    "received":  msg["receivedDateTime"],
                    "body_html": body_html,
                })
                log.info(f"  Newsletter found: '{subject}' from {sender}")

        log.info(f"Outlook: {len(results)} newsletters found in {len(messages)} unread emails.")
        return results


# ── Gmail fetcher ──────────────────────────────────────────────────────────────
class GmailFetcher(EmailFetcher):
    """
    Fetches newsletters from Gmail via Gmail REST API.
    Uses OAuth2 installed app flow — on first run it opens a browser
    window to authorise access, then saves a token for future runs.

    Required config keys:
        gmail_credentials_file  (path to credentials.json downloaded from Google Cloud)
        email_address

    OAuth2 token is cached in tokens/gmail_token.json and refreshed automatically.
    """

    SCOPES         = ["https://www.googleapis.com/auth/gmail.readonly"]
    TOKEN_FILE     = TOKEN_DIR / "gmail_token.json"

    def authenticate(self) -> Credentials:
        log.info("Authenticating with Gmail...")
        creds = None

        # Load cached token if it exists
        if self.TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(self.TOKEN_FILE), self.SCOPES)

        # Refresh or re-authorise as needed
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                log.info("  Refreshing Gmail token...")
                creds.refresh(GoogleAuthRequest())
            else:
                log.info("  Opening browser for Gmail authorisation (first time only)...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.cfg["gmail_credentials_file"], self.SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Cache the token for next run
            with open(self.TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        self._creds = creds
        log.info("  ✓ Gmail authenticated")
        return creds

    def fetch_newsletters(self) -> list[dict]:
        creds   = self.authenticate()
        service = build("gmail", "v1", credentials=creds)

        # Search for unread emails from today
        since        = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        query        = f"is:unread after:{since}"

        response = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()

        message_ids = [m["id"] for m in response.get("messages", [])]
        results     = []

        for msg_id in message_ids:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()

            headers   = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            subject   = headers.get("Subject", "")
            sender    = headers.get("From",    "").lower()
            received  = headers.get("Date",    datetime.now(timezone.utc).isoformat())
            body_html = self._extract_html_body(msg)

            if self.is_newsletter(sender, subject, body_html):
                results.append({
                    "id":        msg_id,
                    "subject":   subject,
                    "sender":    sender,
                    "received":  received,
                    "body_html": body_html,
                })
                log.info(f"  ✓ Newsletter: '{subject}' from {sender}")

        log.info(f"Gmail: {len(results)} newsletters found in {len(message_ids)} unread emails.")
        return results

    def _extract_html_body(self, msg: dict) -> str:
        """Walk the MIME tree and return the HTML body part."""
        payload = msg.get("payload", {})

        # Single part message
        if "parts" not in payload:
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        # Multipart — find text/html part
        for part in payload["parts"]:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

            # Nested multipart (e.g. multipart/alternative inside multipart/mixed)
            if "parts" in part:
                for subpart in part["parts"]:
                    if subpart.get("mimeType") == "text/html":
                        data = subpart.get("body", {}).get("data", "")
                        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

        return ""


# ── Registry — add new providers here ─────────────────────────────────────────
FETCHER_REGISTRY: dict[str, type[EmailFetcher]] = {
    "outlook": OutlookFetcher,
    "gmail":   GmailFetcher,
}


def get_fetcher(cfg: dict) -> EmailFetcher:
    """
    Factory function — returns the right fetcher based on config.
    config.json must have: "email_provider": "outlook" or "gmail"
    """
    provider = cfg.get("email_provider", "").lower()
    if provider not in FETCHER_REGISTRY:
        raise ValueError(
            f"Unknown email_provider '{provider}'. "
            f"Valid options: {list(FETCHER_REGISTRY.keys())}"
        )
    log.info(f"Using fetcher: {provider}")
    return FETCHER_REGISTRY[provider](cfg)