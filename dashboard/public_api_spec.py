"""The OpenAPI 3.1 document for /v1, hand-authored.

Not generated from the FastAPI routes on purpose. The generated document would
describe the handler signatures — which is not the same thing as the contract,
and would silently change shape whenever someone refactored a query parameter.
This file IS the contract: it is what a customer's team reads, what their
client generator consumes, and what a reviewer diffs when someone proposes a
breaking change.

Served at GET /v1/openapi.json, unauthenticated (it describes the API, it
contains no tenant data).
"""

from __future__ import annotations


def _err(description: str) -> dict:
    return {"description": description,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def build_spec(base_url: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "LesCall Integration API",
            "version": "2026-08-19",
            "summary": "Push contacts into LesCall and export call intelligence, "
                       "transcripts and recordings into your CRM or ERP.",
            "description": (
                "## Authentication\n\n"
                "Send your key on every request:\n\n"
                "```\nAuthorization: Bearer lk_live_…\n```\n\n"
                "`X-API-Key: lk_live_…` is accepted as an alternative for HTTP "
                "connectors that cannot set an Authorization header. Keys are "
                "created in the LesCall dashboard under **Settings → API access**; "
                "the secret is shown once, at creation.\n\n"
                "## Rate limits\n\n"
                "Every response carries `X-RateLimit-Limit`, "
                "`X-RateLimit-Remaining`, `X-RateLimit-Reset` (unix seconds), "
                "`X-RateLimit-Window` (`minute`/`day`) and `X-RateLimit-Scope` "
                "(`key`/`account`). A `429` also carries `Retry-After`.\n\n"
                "The budget is **per account and shared across all its keys** — "
                "creating more keys does not create more throughput. A specific "
                "source IP can be granted a higher allowance; ask your LesCall "
                "contact.\n\n"
                "## Access control\n\n"
                "Your account may be restricted to a list of source IP "
                "addresses. Call `GET /v1/me` to see the address we observe and "
                "whether a restriction is in force.\n\n"
                "## Syncing calls\n\n"
                "Poll `GET /v1/calls?order=asc&cursor=<last id you imported>`. "
                "Store `next_cursor` after each page. Keyset pagination means a "
                "call arriving mid-import can never make you skip or duplicate a "
                "record.\n\n"
                "Call analysis is asynchronous and lands a short time after the "
                "call ends: `analyzed: false` means come back, not failure.\n\n"
                "## Idempotency\n\n"
                "Send `Idempotency-Key: <your uuid>` on POSTs. A repeat of the "
                "same key replays the original response instead of writing "
                "again; reusing a key with a different body is a `409`."),
            "contact": {"name": "LesCall support", "email": "tech@dhronai.com"},
        },
        "servers": [{"url": base_url, "description": "Production"}],
        "security": [{"bearerAuth": []}, {"apiKeyHeader": []}],
        "tags": [
            {"name": "Identity", "description": "Verify a key and read its limits."},
            {"name": "Contacts", "description": "Push leads and contacts into LesCall."},
            {"name": "Calls", "description": "Export calls, intelligence, transcripts and audio."},
        ],
        "paths": {
            "/v1/me": {
                "get": {
                    "tags": ["Identity"], "operationId": "whoami",
                    "summary": "Verify a key",
                    "description": "Returns the account, the granted scopes, the "
                                   "effective rate limits, and the source IP we "
                                   "observed. The first call to make when an "
                                   "integration misbehaves.",
                    "responses": {
                        "200": {"description": "Key is valid",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Identity"}}}},
                        "401": _err("Missing, malformed, revoked or expired key"),
                        "403": _err("Source IP not allowed, or API disabled for this account"),
                        "429": _err("Rate limit exceeded"),
                    }}},
            "/v1/agents": {
                "get": {"tags": ["Identity"], "operationId": "listAgents",
                        "summary": "List your agents",
                        "description": "Maps the `agent` slug on a call to a display name.",
                        "responses": {"200": {"description": "OK"}}}},
            "/v1/contacts": {
                "get": {
                    "tags": ["Contacts"], "operationId": "listContacts",
                    "summary": "List contacts",
                    "parameters": [
                        {"$ref": "#/components/parameters/cursor"},
                        {"$ref": "#/components/parameters/limit"},
                        {"$ref": "#/components/parameters/order"},
                        {"name": "group_id", "in": "query", "schema": {"type": "integer"}},
                        {"name": "phone", "in": "query", "schema": {"type": "string"},
                         "description": "Match by number, in any format — comparison "
                                        "is on the last 10 significant digits."},
                        {"name": "q", "in": "query", "schema": {"type": "string"},
                         "description": "Substring match on name or phone."},
                    ],
                    "responses": {"200": {"description": "A page of contacts",
                                          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ContactList"}}}}}},
                "post": {
                    "tags": ["Contacts"], "operationId": "upsertContact",
                    "summary": "Push a contact",
                    "description": "Upserts by default — the same number pushed "
                                   "twice updates rather than duplicating. Only "
                                   "the fields you send are written, so a sync "
                                   "cannot blank out notes typed in the dashboard.",
                    "parameters": [{"$ref": "#/components/parameters/idempotency"}],
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/ContactInput"},
                        "examples": {"lead": {"value": {
                            "phone": "+919876543210", "name": "Ramesh Patel",
                            "group": "Website leads",
                            "fields": {"crm_id": "LD-88421", "budget": "45L",
                                       "project": "Pramukh Aura"}}}}}}},
                    "responses": {
                        "201": {"description": "Contact created",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Contact"}}}},
                        "200": {"description": "Existing contact updated",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Contact"}}}},
                        "400": _err("Unusable phone number or malformed body"),
                        "409": _err("Number already exists and upsert was disabled"),
                    }}},
            "/v1/contacts/bulk": {
                "post": {
                    "tags": ["Contacts"], "operationId": "bulkContacts",
                    "summary": "Push up to 500 contacts",
                    "description": "Always 200 with a per-row result. Rows that "
                                   "fail do not roll back the rows that succeeded.",
                    "parameters": [{"$ref": "#/components/parameters/idempotency"}],
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"type": "object", "required": ["contacts"], "properties": {
                            "contacts": {"type": "array", "maxItems": 500,
                                         "items": {"$ref": "#/components/schemas/ContactInput"}},
                            "group": {"type": "string",
                                      "description": "Default group for rows that name none. Created if new."},
                            "upsert": {"type": "boolean", "default": True}}}}}},
                    "responses": {"200": {"description": "Per-row outcome",
                                          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BulkResult"}}}}}}},
            "/v1/contacts/{contact_id}": {
                "get": {"tags": ["Contacts"], "operationId": "getContact",
                        "parameters": [{"$ref": "#/components/parameters/contactId"}],
                        "responses": {"200": {"description": "OK",
                                              "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Contact"}}}},
                                      "404": _err("No such contact")}},
                "patch": {"tags": ["Contacts"], "operationId": "updateContact",
                          "summary": "Update a contact",
                          "description": "Only the fields you send are written; an "
                                         "omitted field keeps its current value and "
                                         "`fields` merges rather than replaces, so a "
                                         "partial sync cannot blank out notes entered "
                                         "in the dashboard. Sending a different `phone` "
                                         "RENUMBERS this contact — the row and all its "
                                         "call history are kept — and returns 409 if "
                                         "that number already belongs to someone else.",
                          "parameters": [{"$ref": "#/components/parameters/contactId"}],
                          "requestBody": {"required": True, "content": {"application/json": {
                              "schema": {"$ref": "#/components/schemas/ContactInput"}}}},
                          "responses": {"200": {"description": "Updated"},
                                        "409": _err("That number belongs to another contact"),
                                        "404": _err("No such contact")}},
                "delete": {"tags": ["Contacts"], "operationId": "deleteContact",
                           "parameters": [{"$ref": "#/components/parameters/contactId"}],
                           "responses": {"200": {"description": "Deleted"},
                                         "404": _err("No such contact")}}},
            "/v1/contact-groups": {
                "get": {"tags": ["Contacts"], "operationId": "listGroups",
                        "responses": {"200": {"description": "OK"}}},
                "post": {"tags": ["Contacts"], "operationId": "createGroup",
                         "requestBody": {"required": True, "content": {"application/json": {
                             "schema": {"type": "object", "required": ["name"],
                                        "properties": {"name": {"type": "string"}}}}}},
                         "responses": {"201": {"description": "Created"}}}},
            "/v1/calls": {
                "get": {
                    "tags": ["Calls"], "operationId": "listCalls",
                    "summary": "List calls",
                    "description": "Newest first by default. For incremental "
                                   "sync use `order=asc` and pass the previous "
                                   "page's `next_cursor`.",
                    "parameters": [
                        {"$ref": "#/components/parameters/cursor"},
                        {"$ref": "#/components/parameters/limit"},
                        {"$ref": "#/components/parameters/order"},
                        {"name": "since", "in": "query", "schema": {"type": "string"},
                         "description": "Only calls that started at or after this "
                                        "time (YYYY-MM-DD or RFC 3339)."},
                        {"name": "until", "in": "query", "schema": {"type": "string"}},
                        {"name": "direction", "in": "query",
                         "schema": {"type": "string", "enum": ["inbound", "outbound"]}},
                        {"name": "agent", "in": "query", "schema": {"type": "string"}},
                        {"name": "sentiment", "in": "query",
                         "schema": {"type": "string", "enum": ["positive", "neutral", "negative"]}},
                        {"name": "outcome_kind", "in": "query", "schema": {"type": "string"}},
                        {"name": "phone", "in": "query", "schema": {"type": "string"}},
                        {"name": "contact_id", "in": "query", "schema": {"type": "integer"}},
                        {"name": "analyzed", "in": "query", "schema": {"type": "boolean"},
                         "description": "Restrict to calls the AI has (or has not) processed."},
                        {"name": "min_lead_score", "in": "query", "schema": {"type": "integer"},
                         "description": "Only calls scoring at least this (0-100)."},
                    ],
                    "responses": {"200": {"description": "A page of calls",
                                          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CallList"}}}}}}},
            "/v1/calls/{call_id}": {
                "get": {
                    "tags": ["Calls"], "operationId": "getCall",
                    "summary": "Export one call in full",
                    "description": "Call metadata, the AI intelligence record, "
                                   "commitments, action items, the transcript and "
                                   "where to fetch the recording. Sections your "
                                   "key lacks scope for are omitted and listed in "
                                   "`omitted_sections` rather than failing the request.",
                    "parameters": [
                        {"$ref": "#/components/parameters/callId"},
                        {"name": "include_transcript", "in": "query",
                         "schema": {"type": "boolean", "default": True}}],
                    "responses": {"200": {"description": "The full record",
                                          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CallDetail"}}}},
                                  "404": _err("No such call")}}},
            "/v1/calls/{call_id}/intelligence": {
                "get": {"tags": ["Calls"], "operationId": "getCallIntelligence",
                        "summary": "AI summary, scores, commitments",
                        "parameters": [{"$ref": "#/components/parameters/callId"}],
                        "responses": {"200": {"description": "`analyzed:false` "
                                                             "means analysis is still pending."},
                                      "404": _err("No such call")}}},
            "/v1/calls/{call_id}/transcript": {
                "get": {"tags": ["Calls"], "operationId": "getCallTranscript",
                        "parameters": [
                            {"$ref": "#/components/parameters/callId"},
                            {"name": "format", "in": "query",
                             "schema": {"type": "string", "enum": ["json", "text"], "default": "json"}}],
                        "responses": {"200": {"description": "Turns, or text/plain when format=text"},
                                      "404": _err("No such call")}}},
            "/v1/calls/{call_id}/recording": {
                "get": {"tags": ["Calls"], "operationId": "getCallRecording",
                        "summary": "Download the call audio",
                        "description": "Streams a 16-bit WAV. The call record "
                                       "also carries a credential-free "
                                       "`recording.download_url` for systems that "
                                       "need a plain link.",
                        "parameters": [{"$ref": "#/components/parameters/callId"}],
                        "responses": {"200": {"description": "WAV audio",
                                              "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}},
                                      "404": _err("No such call, or it has no recording")}}},
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            },
            "parameters": {
                "cursor": {"name": "cursor", "in": "query", "schema": {"type": "string"},
                           "description": "`next_cursor` from the previous page."},
                "limit": {"name": "limit", "in": "query",
                          "schema": {"type": "integer", "default": 50, "maximum": 200}},
                "order": {"name": "order", "in": "query",
                          "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}},
                "contactId": {"name": "contact_id", "in": "path", "required": True,
                              "schema": {"type": "string"}},
                "callId": {"name": "call_id", "in": "path", "required": True,
                           "schema": {"type": "string"},
                           "description": "The call's `call_id` (e.g. `919876543210_20260812_143210`)."},
                "idempotency": {"name": "Idempotency-Key", "in": "header",
                                "schema": {"type": "string"},
                                "description": "Your own unique id for this write. "
                                               "A repeat replays the first response."},
            },
            "schemas": {
                "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
                    "code": {"type": "string",
                             "enum": ["unauthenticated", "invalid_key", "key_revoked",
                                      "key_expired", "api_disabled", "ip_not_allowed",
                                      "insufficient_scope", "rate_limited", "not_found",
                                      "invalid_request", "conflict", "idempotency_mismatch",
                                      "plan_required", "internal_error", "unavailable"]},
                    "message": {"type": "string"},
                    "request_id": {"type": "string"}}}}},
                "Identity": {"type": "object", "properties": {
                    "account": {"type": "string"},
                    "key": {"type": "object"},
                    "client_ip": {"type": "string"},
                    "rate_limits": {"type": "object"},
                    "ip_restricted": {"type": "boolean"}}},
                "ContactInput": {"type": "object", "required": ["phone"], "properties": {
                    "phone": {"type": "string",
                              "description": "Any format. A 10-digit number without a "
                                             "country code is treated as Indian (+91)."},
                    "name": {"type": "string"},
                    "notes": {"type": "string"},
                    "gender": {"type": "string"},
                    "group": {"type": "string", "description": "Group name; created if new."},
                    "group_id": {"type": "integer"},
                    "fields": {"type": "object", "additionalProperties": True,
                               "description": "Any extra CRM fields; merged, never replaced."},
                    "upsert": {"type": "boolean", "default": True}}},
                "Contact": {"type": "object", "properties": {
                    "id": {"type": "string"}, "phone": {"type": "string"},
                    "name": {"type": "string"}, "notes": {"type": "string"},
                    "group_id": {"type": ["integer", "null"]},
                    "group_name": {"type": ["string", "null"]},
                    "fields": {"type": "object"},
                    "call_status": {"type": "string"}, "disposition": {"type": "string"},
                    "call_count": {"type": "integer"},
                    "last_called_at": {"type": ["string", "null"], "format": "date-time"},
                    "created_at": {"type": "string", "format": "date-time"}}},
                "ContactList": {"allOf": [{"$ref": "#/components/schemas/ListEnvelope"}, {
                    "type": "object", "properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/Contact"}}}}]},
                "BulkResult": {"type": "object", "properties": {
                    "submitted": {"type": "integer"}, "created": {"type": "integer"},
                    "updated": {"type": "integer"}, "failed": {"type": "integer"},
                    "results": {"type": "array", "items": {"type": "object", "properties": {
                        "index": {"type": "integer"},
                        "status": {"type": "string", "enum": ["created", "updated", "failed"]},
                        "id": {"type": "string"}, "error": {"type": "string"}}}}}},
                "ListEnvelope": {"type": "object", "properties": {
                    "object": {"type": "string", "const": "list"},
                    "has_more": {"type": "boolean"},
                    "next_cursor": {"type": ["string", "null"]}}},
                "CallSummary": {"type": "object", "properties": {
                    "call_id": {"type": "string", "description": "Stable public identifier. Store this."},
                    "direction": {"type": "string", "enum": ["inbound", "outbound"]},
                    "agent": {"type": ["string", "null"]},
                    "phone": {"type": "string"},
                    "contact_id": {"type": ["string", "null"]},
                    "contact_name": {"type": "string"},
                    "language": {"type": ["string", "null"]},
                    "started_at": {"type": ["string", "null"], "format": "date-time"},
                    "ended_at": {"type": ["string", "null"], "format": "date-time"},
                    "duration_seconds": {"type": ["number", "null"]},
                    "answer_state": {"type": "string", "enum": ["answered", "empty"],
                                     "description": "`empty` = connected but nobody really spoke."},
                    "outcome": {"type": ["string", "null"]},
                    "summary": {"type": "string"},
                    "analyzed": {"type": "boolean"},
                    "has_recording": {"type": "boolean"},
                    "scores": {"type": "object"},
                    "links": {"type": "object"}}},
                "CallList": {"allOf": [{"$ref": "#/components/schemas/ListEnvelope"}, {
                    "type": "object", "properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/CallSummary"}}}}]},
                "Intelligence": {"type": "object", "properties": {
                    "processed_at": {"type": ["string", "null"], "format": "date-time"},
                    "summary": {"type": "string"},
                    "outcome_summary": {"type": "string"},
                    "customer_intent": {"type": "string"},
                    "sentiment": {"type": ["string", "null"]},
                    "urgency": {"type": ["string", "null"]},
                    "scores": {"type": "object", "properties": {
                        "lead_score": {"type": ["integer", "null"]},
                        "opportunity_score": {"type": ["integer", "null"]},
                        "risk_score": {"type": ["integer", "null"]},
                        "conversion_probability": {"type": ["number", "null"]}}},
                    "topics": {"type": "array", "items": {"type": "object"}},
                    "objections": {"type": "array", "items": {"type": "object"}},
                    "buying_signals": {"type": "array", "items": {"type": "object"}},
                    "pain_points": {"type": "array", "items": {"type": "object"}},
                    "entities": {"type": "object"},
                    "recommendations": {"type": "object"}}},
                "CallDetail": {"type": "object", "properties": {
                    "call": {"$ref": "#/components/schemas/CallSummary"},
                    "intelligence": {"oneOf": [{"$ref": "#/components/schemas/Intelligence"}, {"type": "null"}]},
                    "transcript": {"type": "object", "properties": {
                        "text": {"type": "string"},
                        "turns": {"type": "array", "items": {"type": "object", "properties": {
                            "at": {"type": ["string", "null"]},
                            "speaker": {"type": "string"},
                            "text": {"type": "string"}}}}}},
                    "recording": {"type": "object", "properties": {
                        "available": {"type": "boolean"},
                        "download_url": {"type": "string",
                                         "description": "Credential-free tokenized link."},
                        "stream_url": {"type": "string",
                                       "description": "Needs the API key."},
                        "mime_type": {"type": "string"}}},
                    "commitments": {"type": "array", "items": {"type": "object"}},
                    "action_items": {"type": "array", "items": {"type": "object"}},
                    "collected_data": {"type": "object",
                                       "description": "Fields the agent captured on the call."},
                    "omitted_sections": {"type": "array", "items": {"type": "string"}}}},
            },
        },
    }
