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
# Attio custom-field slugs include a random numeric suffix per workspace;
# override this if the "company name" field is renamed/recreated.
ATTIO_COMPANY_NAME_FIELD = os.environ.get("ATTIO_COMPANY_NAME_FIELD", "company_name_1774195769")
# People object's "UTM_Campaign" field has slug "utm_campaign_1774196226", not "utm_campaign".
ATTIO_UTM_CAMPAIGN_FIELD = os.environ.get("ATTIO_UTM_CAMPAIGN_FIELD", "utm_campaign_1774196226")
# The "New Lifecycle" list's stage attribute is a status field with slug
# "lead_captured" (not "stage") and has no "Not Interested" option, so
# lead_not_interested maps to "Churned" instead.
ATTIO_STAGE_FIELD = os.environ.get("ATTIO_STAGE_FIELD", "lead_captured")
# No campaign field exists on the list yet; set ATTIO_CAMPAIGN_FIELD once one
# is added (e.g. a text attribute with slug "instantly_campaign").
ATTIO_CAMPAIGN_FIELD = os.environ.get("ATTIO_CAMPAIGN_FIELD")

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

EVENT_STAGE_MAP = {
    "email_sent": "Lead Captured",
    "reply_received": "Engaged",
    "lead_interested": "Interested",
    "lead_not_interested": "Churned",
}


def normalize_campaign_name(campaign_name: str) -> str:
    return "".join(campaign_name.split()).lower()


async def fetch_lead_details(client: httpx.AsyncClient, email: str) -> dict:
    # Instantly's webhook payload only includes lead_email/campaign_name; the
    # lead's first_name/last_name/company_name live on the Lead record itself.
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
    }


async def upsert_person(client: httpx.AsyncClient, payload: dict) -> dict | None:
    email = payload.get("lead_email")
    if not email:
        logger.warning("Skipping upsert_person: missing email in payload")
        return None

    attributes = {"email_addresses": [email]}
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

    data = resp.json()
    return data.get("data", {}).get("id", {}).get("record_id")


async def upsert_company(client: httpx.AsyncClient, company_name: str, email: str) -> str | None:
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

    data = resp.json()
    return data.get("data", {}).get("id", {}).get("record_id")


async def create_list_entry(client: httpx.AsyncClient, company_record_id: str, stage: str, campaign_name: str | None) -> None:
    entry_values = {ATTIO_STAGE_FIELD: stage}
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


async def find_list_entry_id(client: httpx.AsyncClient, company_record_id: str) -> str | None:
    resp = await client.get(
        f"{ATTIO_BASE_URL}/objects/companies/records/{company_record_id}/entries",
        headers=ATTIO_HEADERS,
    )
    if resp.status_code >= 300:
        logger.error("Attio find_list_entry_id failed (%s): %s", resp.status_code, resp.text)
        return None

    for entry in resp.json().get("data", []):
        if entry.get("list_id") == ATTIO_LIST_ID:
            return entry.get("entry_id")
    return None


async def update_list_entry_stage(client: httpx.AsyncClient, entry_id: str, stage: str) -> None:
    resp = await client.patch(
        f"{ATTIO_BASE_URL}/lists/{ATTIO_LIST_ID}/entries/{entry_id}",
        headers=ATTIO_HEADERS,
        json={"data": {"entry_values": {ATTIO_STAGE_FIELD: stage}}},
    )
    if resp.status_code >= 300:
        logger.error("Attio update_list_entry_stage failed (%s): %s", resp.status_code, resp.text)


def normalize_payload(payload: dict) -> dict:
    # Instantly's real webhook body uses camelCase for these three fields
    # (firstName/lastName/companyName), not the snake_case shown in their
    # docs example. Normalize to snake_case so the rest of the pipeline only
    # has to deal with one convention.
    normalized = dict(payload)
    if not normalized.get("first_name") and payload.get("firstName"):
        normalized["first_name"] = payload["firstName"]
    if not normalized.get("last_name") and payload.get("lastName"):
        normalized["last_name"] = payload["lastName"]
    if not normalized.get("company_name") and payload.get("companyName"):
        normalized["company_name"] = payload["companyName"]
    return normalized


async def handle_event(payload: dict) -> None:
    payload = normalize_payload(payload)
    event_type = payload.get("event_type")
    email = payload.get("lead_email")
    logger.info("Received event=%s email=%s at %s", event_type, email, datetime.now(timezone.utc).isoformat())

    stage = EVENT_STAGE_MAP.get(event_type)
    if stage is None:
        logger.warning("Unhandled event_type: %s", event_type)
        return

    if not email:
        logger.warning("Skipping event %s: missing email", event_type)
        return

    async with httpx.AsyncClient(timeout=15) as client:
        if not payload.get("company_name"):
            # Some event types (e.g. reply_received) may not carry lead
            # details inline; fall back to looking the lead up by email.
            lead_details = await fetch_lead_details(client, email)
            payload = {**payload, **{k: v for k, v in lead_details.items() if v}}

        company_name = payload.get("company_name")

        person_record_id = await upsert_person(client, payload)
        if not person_record_id:
            return

        if not company_name:
            logger.warning("Skipping list entry for %s: missing company_name (list is keyed on companies)", email)
            return

        company_record_id = await upsert_company(client, company_name, email)
        if not company_record_id:
            return

        # This list does not enforce one-entry-per-record (no 409 on duplicate
        # create), so always check for an existing entry first to avoid piling
        # up duplicates on repeated webhooks for the same lead.
        entry_id = await find_list_entry_id(client, company_record_id)
        if entry_id:
            await update_list_entry_stage(client, entry_id, stage)
        else:
            await create_list_entry(client, company_record_id, stage, payload.get("campaign_name"))


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
