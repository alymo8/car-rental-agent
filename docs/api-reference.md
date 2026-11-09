# Roadway API Reference

The Roadway reservation-servicing API your agent calls. It's a single HTTP service —
the base URL and your access key are in `.env` (`ROADWAY_API_URL`, `ROADWAY_API_KEY`).

## Conventions

- **Base URL:** `ROADWAY_API_URL` (from `.env`).
- **Auth:** every request must send your key in the `X-API-Key` header. There is no
  login or token exchange — your agent works directly against reservation IDs.
- **Content type:** request bodies are JSON; send `Content-Type: application/json`.
- **Success** responses are JSON objects; write operations include `"success": true`.
- **Errors** use a consistent envelope:
  ```json
  { "success": false, "error": { "code": "STRING_CODE", "message": "...", "details": { } } }
  ```

## Reliability & validation (please read)

This API behaves like a real production system, not a sandbox:

- It is **strict about inputs.** Malformed dates are rejected, and write operations
  require complete payment details (see below). Validation failures return `4xx` with an
  error envelope.
- It is **not perfectly reliable.** It is roughly 99% reliable, but you should expect
  **occasional transient errors** (`5xx`) and the occasional slow or timed-out response,
  particularly on availability lookups and on write operations. Your agent is expected to
  handle these gracefully — retry where appropriate, degrade sensibly, and log enough to
  debug.

## Verification, writes & idempotency

- **Reads are open; writes require verification.** `GET /reservations`, `/availability`, and
  `/quote` need only your `X-API-Key`. The write operations (`extend`, `modify`, `cancel`,
  `upgrade`) additionally require the **email on file** for the reservation/customer, passed
  as `"email"` in the request body. A missing or wrong email returns `403 VERIFICATION_FAILED`.
  (The email is **not** returned by `GET /reservations` — in production the customer provides
  it; for development, the emails are in `PROBLEM.md` under Test accounts.)
- **Writes are ephemeral.** This is a shared, read-only-at-heart mock: write operations return
  a realistic success or failure but **do not persist**. You can call `extend`/`cancel`/etc.
  freely while developing — you won't mutate shared state, and there's nothing to reset.
- **Idempotency (optional).** Write endpoints accept an `Idempotency-Key` header. If you retry
  a request with the same key after it has already succeeded, the original response is replayed
  rather than processed again — useful for safe retries.

---

## Endpoints

### GET `/reservations/{reservation_id}`
Look up an active reservation.

**Example**
```
GET /reservations/RWR-29471835
X-API-Key: <your key>
```
```json
{
  "reservation_id": "RWR-29471835",
  "customer_id": "CUST-847291",
  "customer_name": "Sarah Johnson",
  "membership_status": "roadway_preferred",
  "vehicle": { "type": "midsize_sedan", "description": "Chevrolet Malibu or similar", "make_model": "2025 Chevrolet Malibu", "color": "Silver", "license_plate": "8ABC123" },
  "pickup_location": { "code": "LAX", "name": "Los Angeles International Airport", "address": "9217 Airport Blvd, Los Angeles, CA 90045" },
  "return_location": { "code": "LAX", "name": "Los Angeles International Airport", "address": "9217 Airport Blvd, Los Angeles, CA 90045" },
  "dates": { "pickup_datetime": "2027-06-12T10:00:00-07:00", "current_return_datetime": "2027-06-15T14:00:00-07:00", "original_return_datetime": "2027-06-15T14:00:00-07:00" },
  "pricing": { "daily_rate": 45.99, "currency": "USD" },
  "payment": { "card_on_file": { "type": "Visa", "last_four": "4832" }, "total_charged": 137.97 },
  "status": "active"
}
```
`membership_status` is `roadway_preferred` or `standard`. Errors: `404 RESERVATION_NOT_FOUND`.

---

### GET `/availability`
Check vehicle availability at a location for a date range.

**Query params:** `location` (code, e.g. `LAX`), `vehicle_type`, `start_date` (`YYYY-MM-DD`), `end_date` (`YYYY-MM-DD`).

```
GET /availability?location=LAX&vehicle_type=midsize_sedan&start_date=2027-06-15&end_date=2027-06-17
```
```json
{
  "location": { "code": "LAX", "name": "Los Angeles International Airport" },
  "requested_vehicle_type": "midsize_sedan",
  "date_range": { "start": "2027-06-15", "end": "2027-06-17" },
  "availability": {
    "requested_type": { "vehicle_type": "midsize_sedan", "available": true, "available_count": 4, "daily_rate": 45.99, "description": "Chevrolet Malibu or similar" },
    "alternative_types_at_location": [
      { "vehicle_type": "compact", "available_count": 7, "daily_rate": 38.99, "description": "Toyota Corolla or similar" }
    ]
  },
  "nearby_locations": [
    { "code": "SNA", "name": "John Wayne Airport (Orange County)", "availability": { "midsize_sedan": { "available": true, "available_count": 5, "daily_rate": 45.99 } } }
  ]
}
```
Errors: `400 INVALID_DATE`, `404 LOCATION_NOT_FOUND`.

**Vehicle types:** `compact`, `midsize_sedan`, `fullsize_sedan`, `suv`, `minivan`, `luxury`.

---

### POST `/reservations/{reservation_id}/quote`
Price a proposed change before committing. No side effects.

**Body:** `{ "change_type": "extend" | "modify", "new_return_datetime": "...", "new_return_location": "..." }` (`new_return_datetime` required).

```json
{
  "success": true,
  "reservation_id": "RWR-29471835",
  "change_type": "extend",
  "quote": {
    "current_return_datetime": "2027-06-15T14:00:00-07:00",
    "new_return_datetime": "2027-06-17T14:00:00-07:00",
    "charges": { "daily_rate": 45.99, "extension_days": 2, "subtotal": 91.98, "late_fee": 0.0, "one_way_fee": 0.0, "taxes_and_fees": 8.51, "total_charged": 100.49, "currency": "USD" }
  }
}
```

---

### POST `/reservations/{reservation_id}/extend`
Extend a rental to a new return date/time.

**Body**
```json
{
  "new_return_datetime": "2027-06-17T14:00:00-07:00",
  "email": "sarah.johnson@example.com",
  "payment": { "use_card_on_file": true, "cvv": "847", "billing_zip": "90210" }
}
```
`email` (verification), `payment.cvv`, and `payment.billing_zip` are all **required**. Success
returns a `confirmation_number`, an `extension_details` block (incl. `extension_days`,
`late_return`), and a `charges` breakdown. Errors: `403 VERIFICATION_FAILED`,
`400 PAYMENT_VALIDATION_ERROR`, `400 INVALID_DATE`, `400 INVALID_EXTENSION`,
`402 PAYMENT_DECLINED`, `409 RESERVATION_NOT_ACTIVE`, `404 RESERVATION_NOT_FOUND`, transient `5xx`.

> Late return fees apply to standard members; Roadway Preferred members are exempt.

---

### POST `/reservations/{reservation_id}/modify`
Change the pickup/return time or the return location.

**Body:** any of `new_pickup_datetime`, `new_return_datetime`, `new_return_location`, plus
`email` (verification, required) and `payment` (cvv + billing_zip required).

A return-location change incurs a one-way fee and is subject to availability of your
vehicle class at the new location. If unavailable, you get
`409 VEHICLE_UNAVAILABLE` with `details.alternative_types_at_location`. Same verification and
error codes as `extend`.

---

### POST `/reservations/{reservation_id}/cancel`
Cancel a reservation; returns refund/penalty per policy.

**Body:** `{ "reason": "optional string", "email": "..." }` (`email` verification required).
Returns `cancellation_details` (`penalty`, `refund_amount`, `prepaid_amount`). Errors:
`403 VERIFICATION_FAILED`, `409 RESERVATION_NOT_ACTIVE`, `404 RESERVATION_NOT_FOUND`.

---

### POST `/customers/{customer_id}/upgrade`
Upgrade a standard customer to Roadway Preferred (eligibility-gated). Get `customer_id`
from the reservation lookup.

**Body:** `{ "email": "..." }` (verification required — the customer's email on file).
Success returns `new_membership_status: "roadway_preferred"`. Errors: `403 VERIFICATION_FAILED`,
`400 ALREADY_PREFERRED`, `400 NOT_ELIGIBLE`, `404 CUSTOMER_NOT_FOUND`.

---

## Sample reservations

See **Test accounts** in `PROBLEM.md` for reservation IDs and the email on file for each. Look
any of them up with `GET /reservations/{id}` to see vehicle, dates, location, membership tier,
and card on file, then exercise the workflows. The agent must not hard-code these IDs; it
should work on any reservation the API returns.
