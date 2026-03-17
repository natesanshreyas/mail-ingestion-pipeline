#!/usr/bin/env python3
"""
send_test_emails.py — Send realistic test emails to a monitored mailbox.

Use this during demos or end-to-end testing to watch emails stream through
the ingestion pipeline in real time.

Usage:
    python scripts/send_test_emails.py \
        --to broker@yourdomain.com \
        --from-mailbox sender@yourdomain.com \
        [--count 5] \
        [--delay 15] \
        [--loop]

    --to           Monitored mailbox to send emails TO (the one with a Graph subscription)
    --from-mailbox A licensed Exchange Online mailbox to send FROM (can be the same as --to
                   if you want the mailbox to receive mail from itself)
    --count        Number of emails to send (default: 5; ignored if --loop is set)
    --delay        Seconds between emails (default: 15)
    --loop         Keep sending indefinitely until Ctrl-C (great for live demos)

Prerequisites:
    pip install msal httpx

    The app registration used by the pipeline needs ONE additional permission:
        Azure Portal → Entra ID → App registrations → mail-ingestion-pipeline
        → API permissions → Add → Microsoft Graph → Application → Mail.Send
        → Grant admin consent

Environment variables (same ones used by the pipeline):
    GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
import time
from datetime import datetime, timezone

import httpx
import msal

# ---------------------------------------------------------------------------
# Sample emails — realistic insurance broker scenarios
# ---------------------------------------------------------------------------

SAMPLE_EMAILS = [
    {
        "sender_name": "James Whitfield",
        "sender_email": "j.whitfield@brokergroup.com",
        "subject": "New submission – commercial property, Apex Manufacturing",
        "body": (
            "Hi,\n\n"
            "Please find attached a new submission for Apex Manufacturing LLC. "
            "They are seeking coverage for a 45,000 sq ft light-manufacturing facility "
            "in Columbus, Ohio. Replacement cost estimate is $4.2M. "
            "No losses in the past 5 years. Target effective date is April 1.\n\n"
            "Let me know if you need additional information to quote.\n\n"
            "Thanks,\nJames Whitfield\nWhitfield Brokerage Group"
        ),
    },
    {
        "sender_name": "Sarah Okonkwo",
        "sender_email": "s.okonkwo@primecapitalins.com",
        "subject": "Renewal quote request – GL policy #GL-2021-44812",
        "body": (
            "Hello,\n\n"
            "I am writing to request a renewal quote for policy #GL-2021-44812, "
            "which expires on May 15. The insured is Meridian Logistics Inc., a "
            "regional trucking company based in Atlanta. Their fleet has grown from "
            "18 to 24 vehicles over the past year and they have added a new terminal "
            "in Nashville.\n\n"
            "Please confirm whether the current limits ($1M/$2M) are still available "
            "at a competitive rate given the fleet expansion.\n\n"
            "Best regards,\nSarah Okonkwo\nPrime Capital Insurance Partners"
        ),
    },
    {
        "sender_name": "David Reinholt",
        "sender_email": "dreinholt@riskadvisors.net",
        "subject": "Urgent: claim notice – water damage, policy #CP-8801",
        "body": (
            "Team,\n\n"
            "I need to report a loss for one of our accounts. "
            "On the evening of March 10, a burst pipe caused significant water damage "
            "to the second floor of the insured premises at 220 Oak Street, Denver CO. "
            "Preliminary damage estimate from the restoration contractor is $85,000–$110,000.\n\n"
            "Policy number is CP-8801, insured is Ridgeline Retail Partners LLC. "
            "Please open a claim and advise on next steps for the adjuster visit.\n\n"
            "Regards,\nDavid Reinholt\nRisk Advisors Network"
        ),
    },
    {
        "sender_name": "Michelle Tan",
        "sender_email": "mtan@harborsidebrokers.com",
        "subject": "Quote request – D&O coverage for Series B startup",
        "body": (
            "Hi there,\n\n"
            "I have a new client, a SaaS startup that recently closed a $12M Series B round. "
            "They are looking for Directors & Officers coverage ahead of their next board meeting. "
            "The company has 38 employees and approximately $3.2M in annual recurring revenue. "
            "No prior D&O claims.\n\n"
            "Can you provide a quote for a $5M limit? They need it bound within 10 days.\n\n"
            "Thank you,\nMichelle Tan\nHarborside Brokers"
        ),
    },
    {
        "sender_name": "Tom Galloway",
        "sender_email": "tgalloway@gallowayinsurance.com",
        "subject": "Mid-term endorsement request – add new location",
        "body": (
            "Hello,\n\n"
            "Our client, Blue Ridge Hospitality Group, has opened a third restaurant location "
            "at 1500 Peachtree Street NW, Atlanta GA 30309. "
            "They need to add this location to their existing BOP policy (#BOP-2024-77023) "
            "effective immediately.\n\n"
            "Square footage is 3,800 sq ft, similar to their existing locations. "
            "Please issue the endorsement and confirm the adjusted premium.\n\n"
            "Thanks,\nTom Galloway\nGalloway Insurance Agency"
        ),
    },
    {
        "sender_name": "Priya Mehta",
        "sender_email": "p.mehta@trianglecoverage.com",
        "subject": "Non-renewal notice received – need alternative markets",
        "body": (
            "Hi,\n\n"
            "We just received a non-renewal notice on behalf of Coastal Seafood Distributors. "
            "Their current carrier is citing prior losses (two liability claims in 2023 totaling $310K). "
            "Policy expires June 1 and we need alternative markets urgently.\n\n"
            "The account is a wholesale seafood distributor with $8M in revenue and a 12,000 sq ft "
            "refrigerated warehouse in Portland, OR. Do you have appetite for this risk?\n\n"
            "Regards,\nPriya Mehta\nTriangle Coverage Solutions"
        ),
    },
    {
        "sender_name": "Carlos Vega",
        "sender_email": "c.vega@vegariskgroup.com",
        "subject": "Workers comp audit – discrepancy in payroll figures",
        "body": (
            "Hello,\n\n"
            "I am reaching out regarding the workers compensation premium audit for "
            "policy #WC-2023-19044 (insured: Summit Staffing Solutions). "
            "The auditor's final payroll figure of $2.4M does not match our client's "
            "records, which show $1.87M. There appears to be a classification error "
            "on the clerical staff.\n\n"
            "Can someone from the audit team review and contact me directly? "
            "My client is disputing the additional premium of $18,400.\n\n"
            "Thank you,\nCarlos Vega\nVega Risk Group"
        ),
    },
    {
        "sender_name": "Linda Forsythe",
        "sender_email": "lforsythe@northstarbrokers.com",
        "subject": "Excess liability quote – $10M umbrella, manufacturing risk",
        "body": (
            "Hi,\n\n"
            "We are seeking an excess liability quote for one of our larger manufacturing accounts. "
            "The insured is Keystone Industrial Products, a metal fabrication company with "
            "$22M in annual revenues. They currently carry $1M/$2M primary GL and are looking "
            "for a $10M umbrella.\n\n"
            "They have one products liability claim from 2021 ($45K, closed). "
            "Please advise on your appetite and estimated premium range.\n\n"
            "Best,\nLinda Forsythe\nNorthStar Brokers LLC"
        ),
    },
]


# ---------------------------------------------------------------------------
# Graph send helper
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"MSAL token error: {result.get('error_description', result)}")
    return result["access_token"]


def send_email(
    token: str,
    from_mailbox: str,
    to_address: str,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
) -> None:
    """Send one email via Graph API using the app's Mail.Send permission."""
    http = httpx.Client(timeout=30)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
            "from": {"emailAddress": {"name": sender_name, "address": sender_email}},
        },
        "saveToSentItems": False,
    }
    r = http.post(
        f"{GRAPH_BASE}/users/{from_mailbox}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"sendMail failed {r.status_code}: {r.text[:300]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send test emails to a monitored mailbox to exercise the ingestion pipeline."
    )
    parser.add_argument("--to",            required=True, help="Monitored mailbox to deliver emails TO")
    parser.add_argument("--from-mailbox",  required=True, help="Licensed mailbox to send FROM (needs Mail.Send)")
    parser.add_argument("--count",         type=int, default=5, help="Number of emails to send (default: 5)")
    parser.add_argument("--delay",         type=int, default=15, help="Seconds between emails (default: 15)")
    parser.add_argument("--loop",          action="store_true", help="Send indefinitely until Ctrl-C")
    args = parser.parse_args()

    tenant_id     = os.environ.get("GRAPH_TENANT_ID")     or _die("GRAPH_TENANT_ID not set")
    client_id     = os.environ.get("GRAPH_CLIENT_ID")     or _die("GRAPH_CLIENT_ID not set")
    client_secret = os.environ.get("GRAPH_CLIENT_SECRET") or _die("GRAPH_CLIENT_SECRET not set")

    print(f"\n{'='*60}")
    print(f"  Mail Ingestion Pipeline — Test Email Sender")
    print(f"{'='*60}")
    print(f"  TO          : {args.to}")
    print(f"  FROM mailbox: {args.from_mailbox}")
    print(f"  Delay       : {args.delay}s between emails")
    print(f"  Mode        : {'loop (Ctrl-C to stop)' if args.loop else f'{args.count} emails'}")
    print(f"{'='*60}\n")

    print("Acquiring Graph token...", end=" ", flush=True)
    token = _get_token(tenant_id, client_id, client_secret)
    print("OK\n")

    pool = itertools.cycle(random.sample(SAMPLE_EMAILS, len(SAMPLE_EMAILS)))
    counter = itertools.count(1)

    try:
        while True:
            n = next(counter)
            email = next(pool)

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"[{ts}] #{n:>3}  Sending: {email['subject'][:55]}")
            print(f"             From   : {email['sender_name']} <{email['sender_email']}>")

            try:
                send_email(
                    token=token,
                    from_mailbox=args.from_mailbox,
                    to_address=args.to,
                    sender_name=email["sender_name"],
                    sender_email=email["sender_email"],
                    subject=email["subject"],
                    body=email["body"],
                )
                print(f"             Status : sent ✓")
            except RuntimeError as exc:
                print(f"             Status : FAILED — {exc}")

            print()

            if not args.loop and n >= args.count:
                break

            time.sleep(args.delay)

    except KeyboardInterrupt:
        print(f"\nStopped after {n} email(s).")

    print(f"\nDone. Watch the worker logs:")
    print(f"  kubectl logs -n mail-ingestion -l app=ingestion-worker -f\n")


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
