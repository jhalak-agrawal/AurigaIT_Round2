# AV Room Tracker

A small Flask + Postgres app for tracking AV equipment loans: real-time
availability by date range, one-click booking, deposit + late-fee handling
on return, and guardrails against double-booking or one person hoarding gear.

## What it enforces (matches the design discussed)

- **Availability for a future date range**, not just "right now" — checks
  for overlapping bookings, not a simple in/out flag.
- **No double-booking**: enforced two ways — an application-level check at
  booking time, *and* a Postgres exclusion constraint (`no_overlapping_bookings`
  in `init_db.py`) that makes it structurally impossible for two overlapping
  bookings to exist for the same item, even under a race condition.
- **Per-borrower caps**: `max_concurrent_items` (total items at once) and
  `max_per_category` (e.g. only 1 projector at a time), both scoped to the
  requested date range so old returned bookings don't count against you.
- **Transferring an active loan**: an item currently checked out can be
  handed to a new borrower mid-loan. The due date carries over unchanged,
  the item's availability to everyone else is unaffected (it's the same
  underlying booking row throughout, just a different borrower attached to
  it), the outgoing borrower is settled for any lateness up to that moment
  using the same fee formula as a real return, and the incoming borrower
  posts a fresh deposit and must still pass their own borrowing caps.
- **Late fee & deposit refund**, computed at return time:
  `days_late = ceil((hours_late - grace_period) / 24)`,
  `fee = min(days_late * late_fee_per_day, deposit_collected)`,
  `refund = deposit_collected - fee`. The fee is always capped at the
  deposit already on file.

## Project layout

```
app.py            - Flask app factory
config.py         - configuration (reads .env)
extensions.py     - shared SQLAlchemy instance
models.py         - EquipmentModel, Item, Borrower, Booking, Notification
booking_logic.py  - all the business rules (availability, caps, fees)
routes.py         - HTTP routes / views
init_db.py        - creates tables + the Postgres exclusion constraint
seed.py           - sample equipment/borrowers to try it out immediately
templates/        - Jinja HTML templates
static/style.css  - minimal styling
docker-compose.yml- Postgres container definition
.devcontainer/    - GitHub Codespaces config (auto-starts Postgres)
```

## Running in GitHub Codespaces

1. Push this folder to a GitHub repo.
2. Open it in a Codespace. The devcontainer will automatically:
   - install Python dependencies
   - start the Postgres container (`docker compose up -d db`)
   - seed sample data
3. Once the Codespace finishes building, run:
   ```
   python init_db.py   # only needed once — devcontainer setup runs seed.py for you,
                        # but if you rebuild the DB, init_db.py must run before seed.py
   python app.py
   ```
4. Open the forwarded port `5000` (Codespaces will prompt you, or check the
   "Ports" tab). You should see the Availability page.

If the automatic `postCreateCommand` didn't run for some reason (first boot
sometimes races the DB container), just run manually:
```
docker compose up -d db
sleep 5
python init_db.py
python seed.py
python app.py
```

## Running locally (non-Codespaces)

Requires Docker (for Postgres) and Python 3.11+.

```bash
cp .env.example .env
pip install -r requirements.txt
docker compose up -d db
python init_db.py
python seed.py
python app.py
```

Then visit http://localhost:5000.

## Using it

- **Availability** (`/`) — pick a date range, see how many units of each
  equipment model are free.
- **New Booking** (`/book`) — pick a borrower and equipment model; a
  specific unit is auto-assigned from whatever's free. Rejected with a
  clear reason if the borrower's over a cap or nothing's free.
- **Active Bookings** (`/bookings`) — front-desk view. "Check Out" marks a
  reservation as physically picked up and collects the deposit. "Return"
  computes the late fee and refund automatically.
- **Transfer** (button on a checked-out row in Active Bookings, or
  `/bookings/<id>/transfer`) — hand the loan to a different active
  borrower. Shows the current holder and unchanged due date; the outgoing
  borrower's deposit is settled (late fee if applicable) and the incoming
  borrower posts a fresh deposit.
- **Overdue / Due Soon** (`/overdue`) — the data source for reminder nudges.
- **Add Borrower** (`/borrowers/new`) — quick form for new students/clubs.

## Wiring up actual reminder nudges

The brief asks for the system to "nudge people to return it." The
`/overdue` page's data (via `booking_logic.overdue_bookings()` and
`due_soon_bookings()`) is exactly what a scheduled job needs. A simple
approach:

1. Add a script, e.g. `send_reminders.py`, that:
   - calls `overdue_bookings()` and `due_soon_bookings()`
   - for each booking, checks the `Notification` table to see if that
     type of reminder was already sent today
   - sends an email/SMS (e.g. via `smtplib` or a service like SendGrid/Twilio)
   - logs a row to `Notification` so it isn't sent twice
2. Schedule it to run daily — a cron job, a GitHub Actions scheduled
   workflow, or (if you deploy this somewhere persistent) `APScheduler`
   inside the Flask app itself.

This is intentionally left as a separate script rather than baked into a
request handler, since sending reminders should run on a schedule, not on
a page load.

## Adjustable policy knobs

- `GRACE_PERIOD_HOURS` in `.env` — how much slack before a late fee starts
  accruing (default 2 hours).
- `Borrower.max_concurrent_items` / `max_per_category` — per-borrower caps;
  defaults are set in `models.py` (3 and 1) but can be overridden per row,
  e.g. to give staff or long-standing club leads more headroom.
- `EquipmentModel.deposit_amount`, `late_fee_per_day`, `standard_loan_days`
  — set per equipment type in `seed.py` or directly in the DB.

## Notes on the exclusion constraint

`init_db.py` adds a Postgres `EXCLUDE USING gist` constraint on `bookings`,
keyed on `(item_id, date_range)` for rows with `status IN ('reserved',
'checked_out')`. This is what makes double-booking impossible even if two
requests hit the app at the exact same instant — the database itself
rejects the second INSERT, and `booking_logic.create_booking()` catches
that and turns it into a friendly error message. This constraint is
Postgres-specific (`btree_gist` extension), which is part of why this
project uses Postgres rather than SQLite.
