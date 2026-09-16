# Reasoning Behind This Project

This document collects the *why* behind each design decision in the AV Room
Tracker, in the order the problems came up. The brief itself was short — a
paper register that doesn't get updated, gear going missing, double-booked
projectors, borrowers hanging on to things, and a wish for deposits and
nudges. Each section below is a problem from that brief, the options that
were on the table, and why the chosen approach won.

---

## 1. The data model: why split "kind of gear" from "specific unit"

The brief has one sentence that quietly determines the whole schema:
*"Students keep asking 'is a DSLR free this weekend?'"*

That's a question about a **category** ("a DSLR" — any one of them), but
the actual business process — checking something out, tracking who has it,
charging a late fee — has to happen against **one physical, trackable
object** (a specific serial-numbered camera). If those two concepts are
collapsed into one table, you end up in one of two bad places:

- Model availability as a single row per equipment *type* with a count
  field (`available_units: 2`) — simple, but now you can't tell *which*
  camera someone has, can't track condition/damage per unit, and
  incrementing/decrementing a counter is exactly the kind of shared mutable
  state that causes double-booking races.
- Model everything at the unit level only, with no shared "type" — now
  "is a DSLR free" requires querying and grouping units by name every time,
  and shared attributes (deposit amount, late fee rate) have to be
  duplicated across every unit of the same model instead of living in one
  place.

Splitting them — **EquipmentModel** (the category, holding shared policy:
deposit amount, late fee rate, standard loan length) and **Item** (the
individual serialized unit, holding status and condition) — means:
- "Is a DSLR free this weekend?" is answered by counting free Items under
  one EquipmentModel — a natural query, not a workaround.
- Deposit/fee policy lives in exactly one place per equipment type, so
  changing the DSLR's late fee doesn't require updating three rows.
- Each physical unit has its own history (which borrower had it, when,
  any damage notes) — useful for exactly the "gear goes missing" problem
  in the brief, since you can trace a specific unit's custody chain.

**Booking** was deliberately built as *one* table covering both the
reservation and the physical loan (`reserved → checked_out → returned`)
rather than two separate tables (a `Reservation` table and a `Loan` table).
The reasoning: a reservation and a loan are the same real-world thing at
different points in its life, not two different kinds of things. Splitting
them would mean duplicating item/borrower/date fields across two tables and
writing code to migrate a row from one table to the other at pickup time —
extra complexity for no real benefit, since nothing in the brief needs
reservations and loans to be queried or reported on separately.

**Borrower** exists as its own table (rather than just a name/email string
on Booking) because the brief implies borrower-level state that persists
across bookings: how many items they currently hold, whether they've been
suspended for repeat lateness, what club they represent. That's identity,
not just a label — it needed its own row.

---

## 2. Checking availability for a *future* date range, not just "right now"

The naive version of "is this available" is a status flag on Item:
`available` / `on_loan`. That answers "is it free right now," but the
actual question people ask is date-scoped ("is a DSLR free *this
weekend*?") — a flag can't represent that; it has no notion of future time
at all.

The fix: availability isn't a stored property, it's a **derived query** —
does any active Booking for this item/model overlap the requested range?
The overlap test used throughout is the standard interval-intersection
check:

```
startA <= endB AND startB <= endA
```

This was chosen over hand-rolling separate cases ("starts during," "ends
during," "fully spans") because those are all just special cases this one
inequality already covers — fewer cases means fewer places to get the
boundary condition wrong.

Two choices embedded in that query matter:
- **Filter by status** (`reserved`, `checked_out` only) — a `cancelled` or
  `returned` booking must not block a new one even if its dates overlap,
  since it's no longer a live claim on the item.
- **Compare against `due_date`, not `returned_at`** — `returned_at` is
  null until the item is actually physically back. Availability has to be
  computed against when the item is *supposed* to be free, not wait on an
  actual event that hasn't happened yet. (A borrower returning late and
  overlapping into someone else's pickup is a real-world scheduling problem
  handled operationally — the front desk shouldn't hand out the next
  reservation until the previous item is physically back — not something
  the availability formula itself should try to predict.)

For a whole EquipmentModel ("is a DSLR free"), the same overlap check runs
per Item under that model, and the count of Items with **no** overlapping
active booking is the number free. `EquipmentModel.available_unit_count()`
in `models.py` does exactly this.

---

## 3. Preventing the same unit from being double-booked

This is the brief's most concrete pain point: *"two clubs show up for the
same projector."* An availability check alone doesn't prevent this — it
only tells you the state at the instant you asked. Two people can both ask
at the same moment, both see "free," and both proceed to book. The
underlying problem is a **race condition**: check-then-write isn't atomic
unless something makes it atomic.

Three layers were considered:

1. **Trust the application-level check alone.** Rejected — this is
   exactly the race described above. Fine for a demo, not fine for a
   system meant to replace a broken paper process with something more
   reliable.
2. **Transaction + row lock** (`SELECT ... FOR UPDATE` around the check and
   the insert). This closes the race *as long as every booking path goes
   through the same lock*, but has a gap: if there are zero existing
   bookings for an item, there's nothing to lock, so two brand-new
   bookings for a previously-unbooked item could still race.
3. **A database-level constraint that makes the invalid state
   unrepresentable.** This is what was actually built: a Postgres
   `EXCLUDE USING gist` constraint on `(item_id, date_range)`, scoped to
   active statuses. Two overlapping bookings for the same item literally
   cannot both exist as committed rows — the second `INSERT` fails outright,
   regardless of timing, regardless of which code path tried to write it.

Layer 3 was chosen as the real safety net, with the application-level
check (layer 1, in `booking_logic.create_booking`) kept as a fast, friendly
first pass — it produces a clear error message ("that projector was just
booked by someone else") in the common case, and the constraint is there
to catch the rare case that slips past it, converted back into the same
friendly `BookingError` rather than a raw database exception leaking to
the user.

This is also *why the project uses Postgres instead of SQLite*, even
though SQLite would have been simpler to set up in a Codespace. SQLite has
no equivalent to an exclusion constraint; getting the same guarantee there
would mean falling back entirely on transaction-level locking (layer 2),
which has that gap. Given that "two clubs, same projector" is explicitly
named in the brief as a real recurring failure, closing it at the database
level rather than only in application code felt like the right trade for
the extra setup cost of running Postgres.

---

## 4. Late fee and deposit refund: the formula, and the edge cases that shape it

The core formula is simple:

```
days_late = max(0, ceil(hours_late_after_grace / 24))
late_fee  = min(days_late * late_fee_per_day, deposit_collected)
refund    = deposit_collected - late_fee
```

Every non-obvious part of this exists because of a specific edge case that
would otherwise cause a bad interaction at the front desk:

- **`max(0, ...)`** — an early or on-time return must never produce a
  negative fee. Trivial, but worth stating explicitly since it's the kind
  of thing that's easy to get backwards in a first draft.
- **`min(fee, deposit_collected)`** — the fee is capped at the deposit
  already on file. The alternative (letting the fee exceed the deposit)
  turns a simple "keep part of the deposit" transaction into a debt
  someone has to chase down — a different, messier system than what an AV
  room actually needs. If someone is *egregiously* late (weeks, not days),
  that's a case for manual staff escalation (suspend the borrower, chase
  replacement cost separately), not something the automatic formula should
  try to solve by charging an unbounded fee.
- **Rounding up to whole days (`ceil`), not partial days** — this is the
  standard "library fine" model. It was chosen over fractional-day pricing
  because it's unambiguous to explain to a student at the desk: any part of
  a day late is a full day's fee, full stop, no argument about whether it
  was "really" 6 hours or 8.
- **A grace period before the clock starts** — without this, someone
  returning 20 minutes late because the AV room was locked would eat a
  full day's fee, which is disproportionate and exactly the sort of thing
  that turns into a bitter argument at the desk. `GRACE_PERIOD_HOURS`
  (default 2, configurable via `.env`) exists specifically to absorb that
  case without needing manual override.

Two related things were deliberately left as *separate, unbuilt* concerns
rather than folded into this formula:
- **Never returned at all.** This isn't a late fee anymore, it's
  effectively a loss — it needs its own policy (e.g. a cutoff at 14 days
  overdue → mark `lost`, forfeit deposit, consider replacement cost). That
  doesn't fit the return-time calculation, since there's no `returned_at`
  event to calculate from; it belongs in a scheduled sweep instead.
- **Late-return history affecting future borrowing rights** (e.g. "3 late
  returns this semester → account suspended"). Not required for the fee
  math itself, but a natural extension of the Booking history already
  being kept, and arguably a better lever than the fee alone against the
  brief's "borrowers hang on to things far too long" complaint — a fee is
  easy for someone indifferent (or well-off) to just absorb.

---

## 5. Capping how much one person can book at once

The brief's line — *"one person shouldn't be able to book out half the
room at once"* — needed translating into an actual rule before it could
become code. A few different things "half the room" could mean were
considered:

- Raw count of items held (simplest)
- Total deposit value held (more relevant to a business protecting cash
  exposure than to a college AV room)
- Per-category limit (stops someone locking out *every* projector even if
  their total item count is otherwise fine — the closest match to "two
  clubs need a projector and one person has three")
- A dynamic percentage of total inventory

**Item count** and **per-category count** were the two implemented,
because together they directly address the scenario actually named in the
brief, without adding complexity (deposit-value caps, dynamic percentages)
that nothing in the brief calls for. Both caps live on `Borrower`
(`max_concurrent_items`, `max_per_category`) rather than as global
constants, specifically so they're overridable per person — e.g. a
long-standing club lead or a staff member could reasonably get a higher
limit than a first-year student, without that requiring a code change.

The critical detail in both checks is that they're **scoped to the
requested date range**, using the same overlap condition as the
availability check — not "how many things has this person ever booked."
Someone who held 3 items last month but has since returned them all should
be able to book freely today; someone trying to hold 3 overlapping items
*this weekend* should be blocked. Time-scoping the cap is what makes it
match the brief's actual concern (hoarding **at once**), rather than
penalizing someone for being a frequent, well-behaved borrower over time.

The order of checks in `create_booking()` — borrower active status, then
concurrent-item cap, then per-category cap, then unit availability — was
chosen so the cheapest, most decisive checks run first and a rejected
request gets a specific, useful error message (why it failed) rather than
a generic "not available."

---

## 6. Transferring an active loan between borrowers

The requirement — an active loan can move to a new borrower, the due date
carries over unchanged, and the item's availability is unaffected — is
really a statement about *what must not change* when a transfer happens.
That framing drove the implementation more than anything else.

**The Booking row is mutated in place, not replaced.** The alternative
would be to close out the existing Booking (mark it `returned`) and open a
fresh one for the new borrower. That was rejected because it's exactly
what the requirement rules out: closing the first booking would trigger a
real return event, and opening a second one means two rows now exist
against the same item, which is indistinguishable — to every availability
query and to the exclusion constraint — from an actual double-booking. The
whole point of the constraint from Section 3 is that two active bookings
can never coexist on one item; a transfer has to work *with* that
constraint, not need an exception carved out of it. So the transfer only
ever touches `borrower_id` (and the deposit, see below) on the existing
row — `item_id`, `start_date`, `due_date`, and `status` never move. That's
what makes "the item's availability is unaffected" true by construction
rather than something that has to be separately verified after the fact:
no other query in the system can tell a transfer happened at all.

**The due date is never recalculated.** Because `due_date` was already a
field independent of `checked_out_at` (see `models.py` — it's set once at
booking creation, not derived from pickup time), there was no
recalculation to avoid in the first place; the requirement was already
satisfied by the existing schema. This is a case where an earlier decision
(keeping `due_date` as its own stored field rather than computing it from
`checked_out_at + standard_loan_days` every time) paid off unexpectedly.

**Deposit and late-fee responsibility are settled at the transfer moment,
not carried forward.** The brief doesn't specify what happens to the
money when a loan changes hands, so a decision had to be made explicitly:
the outgoing borrower is treated as if they were returning the item right
now — the same `days_late` / `late_fee` / `refund` formula from Section 4
runs against the transfer timestamp instead of an actual return timestamp
— and the incoming borrower posts a fresh deposit. The reasoning: whoever
was responsible for the item up to this point should be charged for
whatever lateness happened on their watch, not the person taking over: if
Asha returned a tripod three days late and someone else then borrowed it,
nobody would expect the new borrower to inherit Asha's fine. A transfer is
the same handoff, just without the item physically passing through the
front desk in between. Because this settlement reuses the exact same
`_late_fee_and_refund()` function as a normal return (refactored out of
`return_booking` specifically so the two could never drift apart), a loan
transferred while already overdue produces the same fee a real return
would have, rather than a second, slightly-different formula that happens
to agree most of the time.

This settlement is logged to a new **BookingTransfer** table rather than
overwriting fields on Booking itself, because `Booking.late_fee_charged`
and `Booking.deposit_refunded` are specifically the *final* return
figures — overwriting them mid-loan would make an eventual real return
compute against numbers that no longer mean what they say. Keeping the
audit trail in a separate table also directly serves the "gear goes
missing, nobody can say who had what" complaint from the original brief:
a chain of transfers is now a queryable history, not something that
overwrites itself.

**The incoming borrower still has to pass the same caps.** Without this,
a transfer would be a loophole around Section 5's per-borrower limits — a
student at their concurrent-item cap could simply have a friend "hold" an
item on paper and transfer it to them the moment it clears. The cap checks
in `transfer_booking()` are the same logic as `create_booking()`, run
against the *remaining* loan window (today through the existing due date)
rather than the original start date, since only the time left on the loan
is what the incoming borrower is actually taking on.

**A day-one bug this feature exposed, and the fix.** Building the
per-category cap test for transfers surfaced a real gap in the overlap
logic that predates the transfer feature entirely: every overlap check
compared against `due_date` directly, which works fine right up until an
item becomes overdue. Once `due_date` has slipped into the past while the
item is still checked out (not yet returned), a *future* date-range query
no longer "overlaps" it by the plain interval test — so an overdue item
that's still physically out could incorrectly show as available next
week, and a borrower already holding an overdue item could incorrectly
clear the cap check for a second item. The fix, applied everywhere an
overlap is computed (`Item.is_free`, `EquipmentModel.available_unit_count`,
and both cap checks): a `checked_out` booking's effective busy-until date
is `max(due_date, today)`, never earlier than today, since being overdue
without being returned means it's still in someone's hands right now,
whatever the original due date says. A `reserved` booking doesn't need
this adjustment — a future reservation's due date is still in the future
by definition. This was caught by testing the transfer feature's cap
enforcement specifically, not by inspection — worth noting as a case where
building the harder feature (transfer) surfaced a bug in logic that had
already shipped and looked correct in isolation.

**Race safety.** `transfer_booking()` opens by taking a row lock on the
Booking (`SELECT ... FOR UPDATE`) before reading its status or borrower,
inside a single transaction that also does the settlement and the update.
This closes the same category of race discussed in Section 3: without the
lock, a transfer and a simultaneous return (or two simultaneous transfer
attempts) could both read the booking as `checked_out` and both proceed,
leaving the row in a state determined by whichever write happened to
commit last rather than by either operation's own logic.

---

## 7. Why Flask + Postgres, and why this particular file layout

Given the brief was for a real, deployable tool rather than a one-off
demo, and given the double-booking guarantee specifically depends on a
Postgres feature (GiST exclusion constraints via `btree_gist`), Postgres
was the natural database choice, and Flask was chosen as a minimal,
transparent framework that doesn't hide the SQL/transaction behavior this
project's correctness actually depends on — a heavier framework or an ORM
that abstracted transactions away would have made the race-condition
reasoning (Section 3) harder to see and verify directly in the code.

The business logic (`booking_logic.py`) was deliberately kept separate
from the HTTP layer (`routes.py`). Every rule discussed above —
availability, overlap prevention, both caps, the fee formula — lives in
one file with no Flask-specific code in it. This means the rules can be
tested directly (as they were, before any web page was involved — see the
smoke tests run against `create_booking`, `check_out_booking`, and
`return_booking` directly) and reused from a future script (e.g. the
reminder-nudge job described in the README) without needing to go through
HTTP at all.

`init_db.py` is kept separate from `app.py`'s normal startup path
deliberately — schema changes (adding a constraint) are a one-time,
explicit operation, not something that should silently run every time the
web server boots.
