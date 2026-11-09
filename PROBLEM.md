# Problem statement

Roadway Rentals is a fictional car rental company used as the setting for this project.

A large share of a rental company's support volume is not new bookings but customers
**servicing a rental they already have**: extending it, changing the time or location,
upgrading, or cancelling. Human agents handle these today. The question this project
explores is whether an AI agent can take a meaningful share of that volume, safely, without
putting revenue or customer trust at risk.

The goal is a working prototype of that agent against a mock reservation API
([`docs/api-reference.md`](docs/api-reference.md)) and a help-center knowledge base
([`data/knowledge-base/articles.json`](data/knowledge-base/articles.json)), framed as a
single-market pilot that does a few things well rather than everything badly.

## Workflows in the problem space

- **Extend**: push out the return date and time on an active rental.
- **Modify**: change the pickup or drop-off time, or the location.
- **Upgrade**: move a customer from Standard to Preferred.
- **Cancel**: cancel a rental, with any refund or penalty per policy.

The API supports all four, plus `quote` and `availability`. Choosing which to support, and
defending that choice, is part of the design. This project supports **Extend** end to end
and hands the rest to a human; the reasoning is in the [README](README.md) and the design
spec under `docs/superpowers/specs/`.

## Constraints

- Treat the API as a real production integration: it is strict about inputs and has
  occasional transient errors and slow responses.
- Any rule that touches money or identity must hold even if the model misbehaves.
- The agent must work on reservations it has never seen, not only the test accounts.

## Test accounts

Each row is a reservation and the email on file, which authorizes a write. Reads do not
need the email.

| Reservation | Email on file |
|---|---|
| `RWR-29471835` | `sarah.johnson@example.com` |
| `RWR-48372915` | `marcus.lee@example.com` |
| `RWR-77001020` | `priya.patel@example.com` |
| `RWR-66002030` | `david.kim@example.com` |
| `RWR-50000001` | `robert.chen@example.com` |
| `RWR-99004050` | `tomas.rivera@example.com` |

*All companies, people, systems and data here are fictional.*
