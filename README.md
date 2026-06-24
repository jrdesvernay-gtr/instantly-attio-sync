# Instantly → Attio Sync

Webhook server that syncs Instantly.ai lead events into Attio CRM in real time: upserts a Person record (contact + UTM_Campaign), upserts a Company record (matched by normalized website URL), and manages that Company's entry in the "New Lifecycle" list.

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
- A Company is upserted per event, matched on the lead's `website` field (query string stripped, path kept) via a custom `instantly_website_url` text attribute. Matching on email/registrable-domain alone would incorrectly merge unrelated SMB leads that share a third-party host (Facebook, eBay, Alibaba pages all resolve to the same root domain); each business's specific page is the actual unique identity. Attio doesn't support `is_unique` on text attributes, so the match-then-create lookup happens in application code (`find_company_by_website`), not via Attio's built-in `matching_attribute` upsert. Falls back to email-domain matching only when a payload has no `website` field.
- `company_name` (and `first_name`/`last_name`) come from the webhook payload's camelCase fields (`companyName`/`firstName`/`lastName` — Instantly's real payload differs from their docs example). If still missing, falls back to looking the lead up via Instantly's `/leads/list` API by email. If a company name still can't be found, the list entry is skipped (logged) since the list is keyed on Companies.
- The list does not enforce one-entry-per-record, so the server always looks up an existing entry for the company first and `PATCH`es its stage; only creates a new entry if none exists yet.
- Switching the company-matching scheme means Company records created under the old domain-based identity won't merge with new website-based ones — expect one-time duplicates for companies already in Attio from before this change.
