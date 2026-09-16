"""
All the rules from the brief live here, in one place:
 - availability checking for a future date range
 - preventing overlapping bookings on the same item
 - capping how much one borrower can hold at once
 - late fee + deposit refund calculation on return

Each "write" operation runs inside a single transaction so the check and
the write can't be split apart by a race condition (see create_booking).
"""

import math
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Booking, BookingTransfer, EquipmentModel, Item, Borrower, _effective_due_date_expr


class BookingError(Exception):
    """Raised for any rule violation - caller shows this message to the user."""


def _late_fee_and_refund(
    due_date: date,
    as_of: datetime,
    deposit_collected: Decimal,
    late_fee_per_day: Decimal,
    grace_period_hours: float,
) -> tuple[int, Decimal, Decimal]:
    """
    The one late-fee formula, shared by an actual return and a mid-loan
    transfer settlement, so the two can never drift out of sync:

        days_late = max(0, ceil((hours_late - grace_period) / 24))
        late_fee  = min(days_late * late_fee_per_day, deposit_collected)
        refund    = deposit_collected - late_fee

    `as_of` is the moment the fee is being assessed at - the actual return
    time for a normal return, or the transfer time when handing the loan
    to a new borrower mid-loan.
    """
    due_end_of_day = datetime.combine(due_date, datetime.max.time(), tzinfo=timezone.utc)

    if as_of <= due_end_of_day:
        days_late = 0
    else:
        hours_late = (as_of - due_end_of_day).total_seconds() / 3600
        hours_late_after_grace = max(0.0, hours_late - grace_period_hours)
        days_late = math.ceil(hours_late_after_grace / 24) if hours_late_after_grace > 0 else 0

    late_fee_computed = Decimal(days_late) * late_fee_per_day
    late_fee_charged = min(late_fee_computed, deposit_collected)
    refund = deposit_collected - late_fee_charged
    return days_late, late_fee_charged, refund


def get_available_units(equipment_model_id: str, start_date: date, end_date: date) -> int:
    model = EquipmentModel.query.get(equipment_model_id)
    if model is None:
        raise BookingError("No such equipment model.")
    return model.available_unit_count(start_date, end_date)


def find_free_item(equipment_model_id: str, start_date: date, end_date: date) -> Item | None:
    """Pick one specific free unit of a model for the given range, or None."""
    candidates = Item.query.filter_by(
        equipment_model_id=equipment_model_id
    ).filter(Item.status != "lost").all()

    for item in candidates:
        if item.is_free(start_date, end_date):
            return item
    return None


def create_booking(
    borrower_id: str,
    equipment_model_id: str,
    start_date: date,
    end_date: date,
) -> Booking:
    """
    Reserves one unit of the given equipment model for the borrower.

    Enforces, in order:
      1. borrower is active (not suspended)
      2. borrower's concurrent-item cap for this date range
      3. borrower's per-category cap for this date range
      4. an actual free unit exists for these dates

    The final INSERT is protected against races by the database's
    exclusion constraint (see init_db.py) - if two requests slip past the
    application-level checks at the same instant, the second INSERT will
    be rejected by Postgres itself and we surface that as a BookingError.
    """
    if start_date > end_date:
        raise BookingError("Start date must be before end date.")

    borrower = Borrower.query.get(borrower_id)
    if borrower is None:
        raise BookingError("No such borrower.")
    if not borrower.active:
        raise BookingError("This borrower's account is suspended (e.g. for repeated late returns).")

    model = EquipmentModel.query.get(equipment_model_id)
    if model is None:
        raise BookingError("No such equipment model.")

    # --- cap check 1: total concurrent items across all categories ---
    concurrent_count = (
        Booking.query.filter(
            Booking.borrower_id == borrower_id,
            Booking.status.in_(["reserved", "checked_out"]),
            Booking.start_date <= end_date,
            _effective_due_date_expr() >= start_date,
        ).count()
    )
    if concurrent_count >= borrower.max_concurrent_items:
        raise BookingError(
            f"{borrower.name} already has {concurrent_count} item(s) booked for "
            f"overlapping dates (limit: {borrower.max_concurrent_items})."
        )

    # --- cap check 2: per-category limit ---
    category_count = (
        db.session.query(Booking)
        .join(Item, Item.id == Booking.item_id)
        .filter(
            Booking.borrower_id == borrower_id,
            Item.equipment_model_id == equipment_model_id,
            Booking.status.in_(["reserved", "checked_out"]),
            Booking.start_date <= end_date,
            _effective_due_date_expr() >= start_date,
        )
        .count()
    )
    if category_count >= borrower.max_per_category:
        raise BookingError(
            f"{borrower.name} already has {category_count} {model.name}(s) booked "
            f"for overlapping dates (limit: {borrower.max_per_category})."
        )

    # --- find a specific free unit and try to book it ---
    item = find_free_item(equipment_model_id, start_date, end_date)
    if item is None:
        raise BookingError(f"No {model.name} is available for those dates.")

    booking = Booking(
        item_id=item.id,
        borrower_id=borrower_id,
        start_date=start_date,
        due_date=end_date,
        status="reserved",
        deposit_collected=Decimal("0.00"),  # collected at pickup, see check_out_booking
    )
    db.session.add(booking)
    try:
        db.session.commit()
    except IntegrityError:
        # The DB-level exclusion constraint caught a race that slipped past
        # the checks above (e.g. simultaneous requests for the last unit).
        db.session.rollback()
        raise BookingError(
            f"That {model.name} was just booked by someone else for an overlapping date. "
            "Please try again."
        )

    return booking


def check_out_booking(booking_id: str) -> Booking:
    """Mark a reserved booking as physically picked up, and collect the deposit."""
    booking = Booking.query.get(booking_id)
    if booking is None:
        raise BookingError("No such booking.")
    if booking.status != "reserved":
        raise BookingError(f"Booking is '{booking.status}', not 'reserved' - cannot check out.")

    model = booking.item.equipment_model
    booking.status = "checked_out"
    booking.checked_out_at = datetime.now(timezone.utc)
    booking.deposit_collected = model.deposit_amount
    db.session.commit()
    return booking


def return_booking(booking_id: str, grace_period_hours: float = 2.0) -> Booking:
    """
    Marks a booking returned and computes the late fee / deposit refund:

        days_late = max(0, ceil((hours_late - grace_period) / 24))
        late_fee  = min(days_late * late_fee_per_day, deposit_collected)
        refund    = deposit_collected - late_fee

    The fee is always capped at the deposit already collected - a student
    should never end up owing more than what they put down.
    """
    booking = Booking.query.get(booking_id)
    if booking is None:
        raise BookingError("No such booking.")
    if booking.status != "checked_out":
        raise BookingError(f"Booking is '{booking.status}', not 'checked_out' - cannot return.")

    now = datetime.now(timezone.utc)
    booking.returned_at = now

    model = booking.item.equipment_model
    _, late_fee_charged, refund = _late_fee_and_refund(
        due_date=booking.due_date,
        as_of=now,
        deposit_collected=booking.deposit_collected,
        late_fee_per_day=model.late_fee_per_day,
        grace_period_hours=grace_period_hours,
    )

    booking.late_fee_charged = late_fee_charged
    booking.deposit_refunded = refund
    booking.status = "returned"

    db.session.commit()
    return booking


def transfer_booking(
    booking_id: str,
    to_borrower_id: str,
    grace_period_hours: float = 2.0,
    notes: str | None = None,
) -> Booking:
    """
    Hands an active (checked-out) loan from its current borrower to a new
    one, mid-loan, without touching the item or its dates:

      - the Booking row keeps the same item_id, start_date, due_date and
        status ('checked_out') - only borrower_id changes. That's what
        makes the transfer invisible to availability checks and the
        database's overlap constraint: nothing about the item's claim on
        those dates is altered, so no other borrower's view of the room
        changes at all.
      - the due date is never recalculated - it carries over unchanged,
        exactly as specified.
      - the outgoing borrower is settled as of the transfer moment, using
        the exact same late-fee formula as a normal return (so a loan
        transferred while already overdue correctly charges the outgoing
        borrower for the lateness that happened on their watch, not the
        incoming borrower).
      - the incoming borrower posts a fresh deposit and is checked against
        their own borrowing caps for the remaining loan period, so a
        transfer can't be used to route around the concurrent-item or
        per-category limits.

    Runs inside a single transaction with a row lock on the booking, so a
    transfer can't race with a simultaneous return or a second transfer of
    the same booking.
    """
    # Lock the booking row for the duration of this transaction - this is
    # what prevents a concurrent return or a second transfer request from
    # reading stale state while we're mid-decision.
    booking = (
        Booking.query.filter_by(id=booking_id)
        .with_for_update()
        .first()
    )
    if booking is None:
        raise BookingError("No such booking.")
    if booking.status != "checked_out":
        raise BookingError(
            f"Booking is '{booking.status}', not 'checked_out' - only an active loan can be transferred."
        )

    from_borrower = booking.borrower
    to_borrower = Borrower.query.get(to_borrower_id)
    if to_borrower is None:
        raise BookingError("No such borrower to transfer to.")
    if to_borrower.id == from_borrower.id:
        raise BookingError("That borrower already holds this item.")
    if not to_borrower.active:
        raise BookingError(f"{to_borrower.name}'s account is suspended and can't accept a transfer.")

    model = booking.item.equipment_model
    now = datetime.now(timezone.utc)

    # --- cap checks for the incoming borrower, scoped to the remaining loan period ---
    # Uses "today" rather than the booking's original start_date, since only
    # the remaining window matters for someone taking over partway through.
    remaining_start = max(date.today(), booking.start_date)
    remaining_end = booking.due_date

    concurrent_count = Booking.query.filter(
        Booking.borrower_id == to_borrower.id,
        Booking.status.in_(["reserved", "checked_out"]),
        Booking.start_date <= remaining_end,
        _effective_due_date_expr() >= remaining_start,
    ).count()
    if concurrent_count >= to_borrower.max_concurrent_items:
        raise BookingError(
            f"{to_borrower.name} already has {concurrent_count} item(s) booked for "
            f"overlapping dates (limit: {to_borrower.max_concurrent_items}) - can't accept this transfer."
        )

    category_count = (
        db.session.query(Booking)
        .join(Item, Item.id == Booking.item_id)
        .filter(
            Booking.borrower_id == to_borrower.id,
            Item.equipment_model_id == model.id,
            Booking.status.in_(["reserved", "checked_out"]),
            Booking.start_date <= remaining_end,
            _effective_due_date_expr() >= remaining_start,
        )
        .count()
    )
    if category_count >= to_borrower.max_per_category:
        raise BookingError(
            f"{to_borrower.name} already has {category_count} {model.name}(s) booked "
            f"for overlapping dates (limit: {to_borrower.max_per_category}) - can't accept this transfer."
        )

    # --- settle the outgoing borrower as of right now, same formula as a real return ---
    _, prior_late_fee, prior_refund = _late_fee_and_refund(
        due_date=booking.due_date,
        as_of=now,
        deposit_collected=booking.deposit_collected,
        late_fee_per_day=model.late_fee_per_day,
        grace_period_hours=grace_period_hours,
    )

    # --- collect a fresh deposit from the incoming borrower ---
    new_deposit = model.deposit_amount

    transfer = BookingTransfer(
        booking_id=booking.id,
        from_borrower_id=from_borrower.id,
        to_borrower_id=to_borrower.id,
        prior_deposit_collected=booking.deposit_collected,
        prior_late_fee_charged=prior_late_fee,
        prior_deposit_refunded=prior_refund,
        new_deposit_collected=new_deposit,
        notes=notes,
    )
    db.session.add(transfer)

    # Mutate the booking in place: borrower and deposit change, everything
    # that governs availability (item_id, start_date, due_date, status)
    # is left exactly as it was.
    booking.borrower_id = to_borrower.id
    booking.deposit_collected = new_deposit

    db.session.commit()
    return booking


def cancel_booking(booking_id: str) -> Booking:
    booking = Booking.query.get(booking_id)
    if booking is None:
        raise BookingError("No such booking.")
    if booking.status not in ("reserved",):
        raise BookingError("Only a reserved (not yet checked out) booking can be cancelled.")
    booking.status = "cancelled"
    db.session.commit()
    return booking


def overdue_bookings():
    """Anything checked out, past its due date, not yet returned - for the nudge job / dashboard."""
    today = date.today()
    return Booking.query.filter(
        Booking.status == "checked_out",
        Booking.due_date < today,
    ).all()


def due_soon_bookings(days_ahead: int = 1):
    """Checked-out items due within `days_ahead` days - for a friendly reminder before it's overdue."""
    from datetime import timedelta

    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    return Booking.query.filter(
        Booking.status == "checked_out",
        Booking.due_date >= today,
        Booking.due_date <= horizon,
    ).all()
