# Instantly → Attio Sync

Webhook server that syncs Instantly.ai lead events into Attio CRM in real time: upserts a Person record (contact + UTM_Campaign), upserts a Company record (matched by email domain), and manages that Company's entry in the "New Lifecycle" list.

## Setup

1. Install dependencies:
   ```
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your real keys:
   ```
   cp .env.example .env
   ```
   - `ATTIO_API_KEY` — Attio API key with read/write access to People and Lists
   - `INSTANTLY_API_KEY` — Instantly API key (kept for reference / future auth use)
   - `ATTIO_LIST_ID` — target list ID (defaults to "New Lifecycle": `5a24da59-7219-401f-8995-5ce5b82d0264`)

   **Never commit `.env`** — it's already in `.gitignore`. If a key is ever pasted somewhere insecure (chat, ticket, etc.), rotate it in the Attio/Instantly dashboard.

## Run locally

```
uvicorn instantly_attio_sync:app --reload --port 8000
```

- Health check: `GET http://localhost:8000/health`
- Mock test event (fires a synthetic `email_sent` payload through the real handler): `GET http://localhost:8000/test`

To receive real Instantly webhooks locally, expose your server with a tunnel (e.g. `ngrok http 8000`) and set that URL as the webhook target in Instantly's campaign settings.

## Deploy to Railway

1. Push this repo to GitHub (or use `railway up` directly).
2. Create a new Railway project from the repo. The included `Procfile` tells Railway how to start the app:
   ```
   web: uvicorn instantly_attio_sync:app --host 0.0.0.0 --port $PORT
   ```
3. In Railway's project settings, add the same environment variables from `.env.example` (with real values).
4. Once deployed, set the Railway URL + `/webhook/instantly` as the webhook endpoint in Instantly.

## Instantly webhook subscription

Subscribe only to the events in `EVENT_STAGE_MAP` (`instantly_attio_sync.py`), not "all events" — anything else is dropped on arrival and just wastes webhook volume:

| Instantly event_type | Attio stage |
|---|---|
| `email_sent` | Lead Captured |
| `reply_received` | Engaged |
| `lead_interested` | Interested |
| `lead_not_interested` | Churned |

These event names and the `lead_email` payload field are taken from Instantly's [webhook events schema reference](https://developer.instantly.ai/guides/webhook-events.md) — verify against that doc if Instantly changes their API.

## Behavior notes

- All incoming events are logged with timestamp, event type, and lead email.
- The server always returns `200` to Instantly, even on internal/Attio errors, so Instantly won't endlessly retry. Errors are logged with the full Attio response body.
- Events without a `lead_email` are logged and skipped.
- A Company is upserted per event, matched by the lead's email domain (Attio's Companies object has no usable `company_name` matching attribute, only `domains`). If `company_name` isn't present in the payload, the list entry is skipped (logged) since the list is keyed on Companies.
- The list does not enforce one-entry-per-record, so the server always looks up an existing entry for the company first and `PATCH`es its stage; only creates a new entry if none exists yet.
