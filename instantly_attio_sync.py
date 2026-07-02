from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

ATTIO_API_KEY = os.environ["ATTIO_API_KEY"]
ATTIO_LIST_ID = os.environ["ATTIO_LIST_ID"]
INSTANTLY_API_KEY = os.environ["INSTANTLY_API_KEY"]
ATTIO_COMPANY_NAME_FIELD = os.environ.get("ATTIO_COMPANY_NAME_FIELD", "company_name_1774195769")
ATTIO_UTM_CAMPAIGN_FIELD = os.environ.get("ATTIO_UTM_CAMPAIGN_FIELD", "utm_campaign_1774196226")
ATTIO_STAGE_FIELD = os.environ.get("ATTIO_STAGE_FIELD", "lead_captured")
ATTIO_CAMPAIGN_FIELD = os.environ.get("ATTIO_CAMPAIGN_FIELD")
ATTIO_WEBSITE_FIELD = os.environ.get("ATTIO_WEBSITE_FIELD", "instantly_website_url")

ATTIO_BASE_URL = "https://api.attio.com/v2"
ATTIO_HEADERS = {
    "Authorization": f"Bearer {ATTIO_API_KEY}",
    "Content-Type": "application/json",
}

INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
INSTANTLY_HEADERS = {
    "Authorization": f"Bearer {INSTANTLY_API_KEY}",
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("instantly_attio_sync")

app = FastAPI()

# Instantly webhook event → Attio stage (used when creating a new list entry)
EVENT_STAGE_MAP = {
    "email_sent": "Lead Captured",
    "reply_received": "Engaged",
    "lead_interested": "Interested",
    "lead_not_interested": "Churned",
}

# PostHog event name → Attio stage
POSTHOG_STAGE_MAP = {
    "checkout_lead_captured_server": "Checkout Started",
    "trial_started": "Trial Started",
    "trial_converted": "Converted — Paid",
}

# Stage priority — higher number = further in the funnel.
# A stage update is only applied when the new stage is higher priority
# than the current one, preventing earlier events from overwriting progress.
# "Churned" is 0 so it can only be set if nothing else has been set yet;
# use Attio manually to churn a lead that already progressed.
STAGE_PRIORITY: dict[str, int] = {
    "Lead Captured": 1,
    "Contacted / Nurturing": 2,
    "Engaged": 3,
    "Interested": 4,
    "Meeting Booked": 5,
    "Checkout Started": 6,
    "Trial Started": 7,
    "Trial In Flight": 8,
    "Upgrade Page Visited": 9,
    "Converted — Paid": 10,
    "Trial Expired — No Convert": 11,
    "Churned": 0,
}


def stage_allows_update(current: str | None, new: str) -> bool:
    """Return True only if new stage is a forward move (or no current stage)."""
    if current is None:
        return True
    return STAGE_PRIORITY.get(new, 0) > STAGE_PRIORITY.get(current, 0)


def normalize_campaign_name(campaign_name: str) -> str:
    return "".join(campaign_name.split()).lower()


def normalize_website(website: str) -> str:
    return website.split("?")[0]


def normalize_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("first_name") and payload.get("firstName"):
        normalized["first_name"] = payload["firstName"]
    if not normalized.get("last_name") and payload.get("lastName"):
        normalized["last_name"] = payload["lastName"]
    if not normalized.get("company_name") and payload.get("companyName"):
        normalized["company_name"] = payload["companyName"]
    return normalized


async def fetch_lead_details(client: httpx.AsyncClient, email: str) -> dict:
    resp = await client.post(
        f"{INSTANTLY_BASE_URL}/leads/list",
        headers=INSTANTLY_HEADERS,
        json={"contacts": [email], "limit": 1},
    )
    if resp.status_code >= 300:
        logger.error("Instantly fetch_lead_details failed (%s): %s", resp.status_code, resp.text)
        return {}

    items = resp.json().get("items", [])
    if not items:
        logger.warning("No Instantly lead found for %s", email)
        return {}

    lead = items[0]
    return {
        "first_name": lead.get("first_name"),
        "last_name": lead.get("last_name"),
        "company_name": lead.get("company_name"),
        "website": lead.get("website"),
    }


async def upsert_person(client: httpx.AsyncClient, payload: dict) -> str | None:
    email = payload.get("lead_email")
    if not email:
        logger.warning("Skipping upsert_person: missing email in payload")
        return None

    attributes: dict = {"email_addresses": [email]}
    first_name = payload.get("first_name")
    last_name = payload.get("last_name")
    if first_name or last_name:
        full_name = " ".join(part for part in (first_name, last_name) if part)
        attributes["name"] = {
            "first_name": first_name or "",
            "last_name": last_name or "",
            "full_name": full_name,
        }
    if payload.get("company_name"):
        attributes[ATTIO_COMPANY_NAME_FIELD] = payload["company_name"]
    if payload.get("campaign_name"):
        attributes[ATTIO_UTM_CAMPAIGN_FIELD] = normalize_campaign_name(payload["campaign_name"])

    resp = await client.put(
        f"{ATTIO_BASE_URL}/objects/people/records",
        headers=ATTIO_HEADERS,
        params={"matching_attribute": "email_addresses"},
        json={"data": {"values": attributes}},
    )
    if resp.status_code >= 300:
        logger.error("Attio upsert_person failed (%s): %s", resp.status_code, resp.text)
        return None

    return resp.json().get("data", {}).get("id", {}).get("record_id")


async def find_company_by_website(client: httpx.AsyncClient, website: str) -> str | None:
    resp = await client.post(
        f"{ATTIO_BASE_URL}/objects/companies/records/query",
        headers=ATTIO_HEADERS,
        json={"filter": {ATTIO_WEBSITE_FIELD: website}, "limit": 1},
    )
    if resp.status_code >= 300:
        logger.error("Attio find_company_by_website failed (%s): %s", resp.status_code, resp.text)
        return None

    items = resp.json().get("data", [])
    return items[0]["id"]["record_id"] if items else None


async def find_company_by_name(client: httpx.AsyncClient, company_name: str) -> str | None:
    resp = await client.post(
        f"{ATTIO_BASE_URL}/objects/companies/records/query",
        headers=ATTIO_HEADERS,
        json={"filter": {"name": company_name}, "limit": 1},
    )
    if resp.status_code >= 300:
        logger.error("Attio find_company_by_name failed (%s): %s", resp.status_code, resp.text)
        return None

    items = resp.json().get("data", [])
    return items[0]["id"]["record_id"] if items else None


async def upsert_company(client: httpx.AsyncClient, company_name: str, email: str, website: str | None) -> str | None:
    if website:
        normalized_website = normalize_website(website)
        record_id = await find_company_by_website(client, normalized_website)
        if record_id:
            return record_id

        resp = await client.post(
            f"{ATTIO_BASE_URL}/objects/companies/records",
            headers=ATTIO_HEADERS,
            json={"data": {"values": {"name": company_name, ATTIO_WEBSITE_FIELD: normalized_website}}},
        )
    else:
        domain = email.split("@")[-1].lower()
        resp = await client.put(
            f"{ATTIO_BASE_URL}/objects/companies/records",
            headers=ATTIO_HEADERS,
            params={"matching_attribute": "domains"},
            json={"data": {"values": {"domains": [domain], "name": company_name}}},
        )

    if resp.status_code >= 300:
        logger.error("Attio upsert_company failed (%s): %s", resp.status_code, resp.text)
        return None

    return resp.json().get("data", {}).get("id", {}).get("record_id")


async def find_list_entry(client: httpx.AsyncClient, company_record_id: str) -> dict | None:
    """Return {"entry_id": ..., "current_stage": ...} for the company's list entry, or None."""
    resp = await client.get(
        f"{ATTIO_BASE_URL}/objects/companies/records/{company_record_id}/entries",
        headers=ATTIO_HEADERS,
    )
    if resp.status_code >= 300:
        logger.error("Attio find_list_entry failed (%s): %s", resp.status_code, resp.text)
        return None

    for entry in resp.json().get("data", []):
        if entry.get("list_id") == ATTIO_LIST_ID:
            stage_values = entry.get("entry_values", {}).get(ATTIO_STAGE_FIELD, [])
            current_stage = stage_values[0].get("status", {}).get("title") if stage_values else None
            return {"entry_id": entry.get("entry_id"), "current_stage": current_stage}
    return None


async def create_list_entry(client: httpx.AsyncClient, company_record_id: str, stage: str, campaign_name: str | None) -> None:
    entry_values: dict = {ATTIO_STAGE_FIELD: stage}
    if campaign_name and ATTIO_CAMPAIGN_FIELD:
        entry_values[ATTIO_CAMPAIGN_FIELD] = campaign_name

    resp = await client.post(
        f"{ATTIO_BASE_URL}/lists/{ATTIO_LIST_ID}/entries",
        headers=ATTIO_HEADERS,
        json={
            "data": {
                "parent_record_id": company_record_id,
                "parent_object": "companies",
                "entry_values": entry_values,
            }
        },
    )
    if resp.status_code == 409:
        logger.info("List entry for company %s already exists, skipping", company_record_id)
        return
    if resp.status_code >= 300:
        logger.error("Attio create_list_entry failed (%s): %s", resp.status_code, resp.text)


async def update_list_entry_stage(client: httpx.AsyncClient, entry_id: str, stage: str) -> None:
    resp = await client.patch(
        f"{ATTIO_BASE_URL}/lists/{ATTIO_LIST_ID}/entries/{entry_id}",
        headers=ATTIO_HEADERS,
        json={"data": {"entry_values": {ATTIO_STAGE_FIELD: stage}}},
    )
    if resp.status_code >= 300:
        logger.error("Attio update_list_entry_stage failed (%s): %s", resp.status_code, resp.text)


async def handle_event(payload: dict) -> None:
    payload = normalize_payload(payload)
    event_type = payload.get("event_type")
    email = payload.get("lead_email")
    logger.info("Received event=%s email=%s at %s", event_type, email, datetime.now(timezone.utc).isoformat())

    if event_type not in EVENT_STAGE_MAP:
        logger.warning("Unhandled event_type: %s", event_type)
        return

    if not email:
        logger.warning("Skipping event %s: missing email", event_type)
        return

    async with httpx.AsyncClient(timeout=15) as client:
        if not payload.get("company_name"):
            lead_details = await fetch_lead_details(client, email)
            payload = {**payload, **{k: v for k, v in lead_details.items() if v}}

        company_name = payload.get("company_name")

        person_record_id = await upsert_person(client, payload)
        if not person_record_id:
            return

        if not company_name:
            logger.warning("Skipping list entry for %s: missing company_name", email)
            return

        company_record_id = await upsert_company(client, company_name, email, payload.get("website"))
        if not company_record_id:
            return

        existing = await find_list_entry(client, company_record_id)
        if existing:
            # For email_sent, follow-up emails move the stage to "Contacted / Nurturing".
            # All other events use their mapped stage directly.
            new_stage = "Contacted / Nurturing" if event_type == "email_sent" else EVENT_STAGE_MAP[event_type]
            if stage_allows_update(existing["current_stage"], new_stage):
                logger.info("Updating stage %s → %s for company %s", existing["current_stage"], new_stage, company_record_id)
                await update_list_entry_stage(client, existing["entry_id"], new_stage)
            else:
                logger.info("Skipping stage update: %s → %s would be a downgrade", existing["current_stage"], new_stage)
        else:
            # First contact — always "Lead Captured" regardless of event type.
            await create_list_entry(client, company_record_id, "Lead Captured", payload.get("campaign_name"))


async def handle_posthog_event(email: str, posthog_event: str) -> None:
    """Update a company's lifecycle stage based on a PostHog product event."""
    stage = POSTHOG_STAGE_MAP.get(posthog_event)
    if not stage:
        logger.warning("Unhandled PostHog event: %s", posthog_event)
        return

    logger.info("PostHog event=%s email=%s → stage=%s", posthog_event, email, stage)

    async with httpx.AsyncClient(timeout=15) as client:
        # Resolve the person to get their company name, then find the company record.
        person_resp = await client.post(
            f"{ATTIO_BASE_URL}/objects/people/records/query",
            headers=ATTIO_HEADERS,
            json={"filter": {"email_addresses": {"$eq": email}}, "limit": 1},
        )
        if person_resp.status_code >= 300:
            logger.error("Attio person lookup failed (%s): %s", person_resp.status_code, person_resp.text)
            return

        people = person_resp.json().get("data", [])
        if not people:
            logger.warning("No Attio person found for PostHog email %s", email)
            return

        # Extract company name from the custom field on the person record.
        person_values = people[0].get("values", {})
        company_name_values = person_values.get(ATTIO_COMPANY_NAME_FIELD, [])
        company_name = company_name_values[0].get("value") if company_name_values else None

        if not company_name:
            logger.warning("Person %s has no company_name; cannot update list entry", email)
            return

        company_record_id = await find_company_by_name(client, company_name)
        if not company_record_id:
            logger.warning("No Attio company found for name '%s' (email %s)", company_name, email)
            return

        existing = await find_list_entry(client, company_record_id)
        if existing:
            if stage_allows_update(existing["current_stage"], stage):
                logger.info("PostHog: updating stage %s → %s for company %s", existing["current_stage"], stage, company_record_id)
                await update_list_entry_stage(client, existing["entry_id"], stage)
            else:
                logger.info("PostHog: skipping stage update %s → %s (downgrade)", existing["current_stage"], stage)
        else:
            await create_list_entry(client, company_record_id, stage, None)


@app.post("/webhook/instantly")
async def instantly_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        logger.error("Failed to parse webhook payload as JSON")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    try:
        await handle_event(payload)
    except Exception:
        logger.exception("Unhandled error processing event %s", payload.get("event_type"))

    return JSONResponse(status_code=200, content={"status": "received"})


@app.post("/webhook/posthog")
async def posthog_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        logger.error("Failed to parse PostHog webhook payload as JSON")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    try:
        event = payload.get("event")
        # PostHog sends the user identifier as distinct_id; fall back to properties.email.
        email = payload.get("distinct_id") or payload.get("properties", {}).get("email")
        if not email or "@" not in email:
            logger.warning("PostHog webhook missing usable email (distinct_id=%s)", payload.get("distinct_id"))
            return JSONResponse(status_code=200, content={"status": "ignored"})
        await handle_posthog_event(email, event)
    except Exception:
        logger.exception("Unhandled error processing PostHog event %s", payload.get("event"))

    return JSONResponse(status_code=200, content={"status": "received"})


@app.get("/health")
async def health():
    return {"status": "alive"}


@app.get("/test")
async def test():
    mock_payload = {
        "event_type": "email_sent",
        "lead_email": "test.lead@example.com",
        "first_name": "Test",
        "last_name": "Lead",
        "company_name": "Example Co",
        "campaign_name": "Test Campaign",
    }
    await handle_event(mock_payload)
    return {"status": "test event fired", "payload": mock_payload}
