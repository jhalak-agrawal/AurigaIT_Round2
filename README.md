<div align="center">

# 🎥 AV Room Tracker

**A Flask + Postgres app that replaces the AV room's broken paper register.**

Real-time availability by date range · one-click booking · deposits & late fees · race-safe double-booking prevention

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-GiST%20constraint-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Codespaces](https://img.shields.io/badge/GitHub-Codespaces%20ready-181717?logo=github&logoColor=white)](https://github.com/features/codespaces)

</div>

---

## 📋 Table of contents

- [Why this exists](#-why-this-exists)
- [What it enforces](#-what-it-enforces-matches-the-design-discussed)
- [Project layout](#-project-layout)
- [Quickstart — GitHub Codespaces](#-quickstart--github-codespaces)
- [Quickstart — running locally](#-quickstart--running-locally)
- [Using it](#-using-it)
- [Wiring up reminder nudges](#-wiring-up-actual-reminder-nudges)
- [Adjustable policy knobs](#️-adjustable-policy-knobs)
- [Notes on the exclusion constraint](#-notes-on-the-exclusion-constraint)

---

## 🧭 Why this exists

Paper registers don't get updated, gear goes missing, projectors get double-booked, and borrowers hang on to things far too long. This app fixes that with a small, transparent Flask backend and a Postgres schema that makes the worst failure — **two clubs booking the same projector** — structurally impossible, not just "checked for."

## ✅ What it enforces (matches the design discussed)

| Guarantee | How |
|---|---|
| **Date-range availability**, not just "right now" | Checks for *overlapping* bookings against the requested range, not a simple in/out flag |
| **No double-booking, ever** | Enforced twice: an application-level check at booking time, *and* a Postgres `EXCLUDE` constraint (`no_overlapping_bookings` in `init_db.py`) that makes two overlapping bookings for the same item impossible to commit — even under a race condition |
| **Per-borrower caps** | `max_concurrent_items` (total items at once) and `max_per_category` (e.g. only 1 projector at a time), both scoped to the requested date range so old, returned bookings don't count against you |
| **Mid-loan transfers** | An active loan can move to a new borrower. Due date carries over unchanged, the item's availability to everyone else is unaffected (same underlying booking row throughout), the outgoing borrower is settled for lateness using the same fee formula as a real return, and the incoming borrower posts a fresh deposit and must still pass their own caps |
| **Late fee & deposit refund** | Computed automatically at return: `days_late = ceil((hours_late − grace_period) / 24)`, `fee = min(days_late × late_fee_per_day, deposit_collected)`, `refund = deposit_collected − fee` — the fee is always capped at the deposit on file |

## 📁 Project layout

```text
app.py              Flask app factory
config.py           configuration (reads .env)
extensions.py       shared SQLAlchemy instance
models.py           EquipmentModel, Item, Borrower, Booking, Notification
booking_logic.py    all the business rules (availability, caps, fees)
routes.py           HTTP routes / views
init_db.py          creates tables + the Postgres exclusion constraint
seed.py             sample equipment/borrowers to try it out immediately
templates/          Jinja HTML templates
static/style.css    minimal styling
docker-compose.yml  Postgres container definition
.devcontainer/      GitHub Codespaces config (auto-starts Postgres)
```

## 🚀 Quickstart — GitHub Codespaces

1. Push this folder to a GitHub repo.
2. Open it in a Codespace. The devcontainer automatically:
   - installs Python dependencies
   - starts the Postgres container (`docker compose up -d db`)
   - seeds sample data
3. Once the Codespace finishes building, run:
   ```bash
   python init_db.py   # only needed once — devcontainer setup runs seed.py for you,
                        # but if you rebuild the DB, init_db.py must run before seed.py
   python app.py
   ```
4. Open the forwarded port `5000` (Codespaces will prompt you, or check the **Ports** tab). You should see the Availability page.

> **If `postCreateCommand` didn't run** (first boot sometimes races the DB container), run manually:
> ```bash
> docker compose up -d db
> sleep 5
> python init_db.py
> python seed.py
> python app.py
> ```

## 💻 Quickstart — running locally

Requires Docker (for Postgres) and Python 3.11+.

```bash
cp .env.example .env
pip install -r requirements.txt
docker compose up -d db
python init_db.py
python seed.py
python app.py
```

Then visit **http://localhost:5000**.

## 🖱️ Using it

| Page | Route | What it does |
|---|---|---|
| **Availability** | `/` | Pick a date range, see how many units of each equipment model are free |
| **New Booking** | `/book` | Pick a borrower and equipment model; a specific unit is auto-assigned from whatever's free. Rejected with a clear reason if the borrower's over a cap or nothing's free |
| **Active Bookings** | `/bookings` | Front-desk view. **Check Out** marks a reservation as physically picked up and collects the deposit. **Return** computes the late fee and refund automatically |
| **Transfer** | `/bookings/<id>/transfer` | Hand an active loan to a different borrower. Shows the current holder and unchanged due date; outgoing borrower's deposit is settled, incoming borrower posts a fresh deposit |
| **Overdue / Due Soon** | `/overdue` | The data source for reminder nudges |
| **Add Borrower** | `/borrowers/new` | Quick form for new students/clubs |

## 🔔 Wiring up actual reminder nudges

The brief asks for the system to "nudge people to return it." The `/overdue` page's data (via `booking_logic.overdue_bookings()` and `due_soon_bookings()`) is exactly what a scheduled job needs:

1. Add a script, e.g. `send_reminders.py`, that:
   - calls `overdue_bookings()` and `due_soon_bookings()`
   - checks the `Notification` table so the same reminder isn't sent twice in a day
   - sends an email/SMS (e.g. via `smtplib` or a service like SendGrid/Twilio)
   - logs a row to `Notification` once sent
2. Schedule it to run daily — a cron job, a GitHub Actions scheduled workflow, or (if deployed persistently) `APScheduler` inside the Flask app itself.

This is intentionally a separate script rather than baked into a request handler, since sending reminders should run on a schedule, not on a page load.

## ⚙️ Adjustable policy knobs

| Knob | Where | Default |
|---|---|---|
| `GRACE_PERIOD_HOURS` | `.env` | 2 hours before a late fee starts accruing |
| `Borrower.max_concurrent_items` / `max_per_category` | `models.py` (overridable per row) | 3 / 1 — e.g. give staff or club leads more headroom |
| `EquipmentModel.deposit_amount`, `late_fee_per_day`, `standard_loan_days` | `seed.py` or directly in the DB | set per equipment type |

## 🔒 Notes on the exclusion constraint

`init_db.py` adds a Postgres `EXCLUDE USING gist` constraint on `bookings`, keyed on `(item_id, date_range)` for rows with `status IN ('reserved', 'checked_out')`.

This is what makes double-booking impossible even if two requests hit the app at the exact same instant — **the database itself rejects the second `INSERT`**, and `booking_logic.create_booking()` catches that and turns it into a friendly error message rather than a raw exception.

This constraint is Postgres-specific (`btree_gist` extension), which is why this project uses Postgres rather than SQLite.

---

<div align="center">

See [`REASONING.md`](./REASONING.md) for the full design rationale behind every decision above.

</div>
