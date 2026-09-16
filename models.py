import uuid
from datetime import date, datetime, timezone

from sqlalchemy import case, func

from extensions import db


def _effective_due_date_expr():
    """
    SQL expression for a booking's *effective* busy-until date.

    A 'reserved' booking is busy through its due_date, as recorded.
    A 'checked_out' booking is busy through max(due_date, today) - if it's
    overdue but hasn't been returned yet, it's still physically out right
    now, so it must never appear "free" for a future date range just
    because its due_date has technically passed. Without this, an overdue
    item that's still out would incorrectly start showing as available for
    next week the moment its due date slipped into the past.
    """
    return case(
        (Booking.status == "checked_out", func.greatest(Booking.due_date, func.current_date())),
        else_=Booking.due_date,
    )


def _uuid():
    return str(uuid.uuid4())


class EquipmentModel(db.Model):
    """A *kind* of gear, e.g. 'Canon EOS 90D' or 'Epson Projector'.

    This is what people mean when they ask "is a DSLR free this weekend?" -
    the question is about the category, not one specific unit.
    """

    __tablename__ = "equipment_models"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # camera, projector, mic, tripod, ...
    deposit_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    late_fee_per_day = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    standard_loan_days = db.Column(db.Integer, nullable=False, default=3)

    items = db.relationship("Item", backref="equipment_model", lazy=True)

    def available_unit_count(self, start_date: date, end_date: date) -> int:
        """How many physical units of this model are free for the whole range."""
        total = Item.query.filter_by(
            equipment_model_id=self.id
        ).filter(Item.status != "lost").count()

        busy_item_ids = (
            db.session.query(Booking.item_id)
            .join(Item, Item.id == Booking.item_id)
            .filter(
                Item.equipment_model_id == self.id,
                Booking.status.in_(["reserved", "checked_out"]),
                Booking.start_date <= end_date,
                _effective_due_date_expr() >= start_date,
            )
            .distinct()
            .count()
        )
        return max(0, total - busy_item_ids)

    def __repr__(self):
        return f"<EquipmentModel {self.name}>"


class Item(db.Model):
    """One physical, trackable unit of an EquipmentModel."""

    __tablename__ = "items"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    equipment_model_id = db.Column(
        db.String(36), db.ForeignKey("equipment_models.id"), nullable=False
    )
    asset_tag = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="available")
    # available | reserved | on_loan | maintenance | lost
    condition_notes = db.Column(db.Text, nullable=True)

    bookings = db.relationship("Booking", backref="item", lazy=True)

    def is_free(self, start_date: date, end_date: date) -> bool:
        conflict = Booking.query.filter(
            Booking.item_id == self.id,
            Booking.status.in_(["reserved", "checked_out"]),
            Booking.start_date <= end_date,
            _effective_due_date_expr() >= start_date,
        ).first()
        return conflict is None

    def __repr__(self):
        return f"<Item {self.asset_tag}>"


class Borrower(db.Model):
    __tablename__ = "borrowers"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    name = db.Column(db.String(120), nullable=False)
    student_id = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    club_affiliation = db.Column(db.String(120), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    max_concurrent_items = db.Column(db.Integer, nullable=False, default=3)
    max_per_category = db.Column(db.Integer, nullable=False, default=1)

    bookings = db.relationship("Booking", backref="borrower", lazy=True)

    def __repr__(self):
        return f"<Borrower {self.name}>"


class Booking(db.Model):
    """Covers both a reservation and the physical loan - same lifecycle,
    one status field: reserved -> checked_out -> returned (or cancelled)."""

    __tablename__ = "bookings"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    item_id = db.Column(db.String(36), db.ForeignKey("items.id"), nullable=False)
    borrower_id = db.Column(db.String(36), db.ForeignKey("borrowers.id"), nullable=False)

    start_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)

    checked_out_at = db.Column(db.DateTime(timezone=True), nullable=True)
    returned_at = db.Column(db.DateTime(timezone=True), nullable=True)

    status = db.Column(db.String(20), nullable=False, default="reserved")
    # reserved | checked_out | returned | cancelled

    deposit_collected = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    late_fee_charged = db.Column(db.Numeric(10, 2), nullable=True)
    deposit_refunded = db.Column(db.Numeric(10, 2), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self):
        return f"<Booking {self.id} item={self.item_id} status={self.status}>"


class BookingTransfer(db.Model):
    """
    Audit record of an active loan changing hands mid-loan.

    The Booking row itself is mutated in place (its item_id, start_date,
    due_date and status never change - only borrower_id does), which is
    exactly what keeps the item's availability unaffected by a transfer:
    nothing about the item or its date range changes, so every existing
    overlap check and the database's exclusion constraint see no
    difference at all. This table exists purely to keep an audit trail of
    who held the item and when, and to settle each borrower's deposit and
    late-fee exposure at the moment responsibility changes hands.
    """

    __tablename__ = "booking_transfers"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    booking_id = db.Column(db.String(36), db.ForeignKey("bookings.id"), nullable=False)
    from_borrower_id = db.Column(db.String(36), db.ForeignKey("borrowers.id"), nullable=False)
    to_borrower_id = db.Column(db.String(36), db.ForeignKey("borrowers.id"), nullable=False)

    transferred_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Settlement for the outgoing borrower, computed as of the transfer
    # moment using the same late-fee formula as a normal return.
    prior_deposit_collected = db.Column(db.Numeric(10, 2), nullable=False)
    prior_late_fee_charged = db.Column(db.Numeric(10, 2), nullable=False)
    prior_deposit_refunded = db.Column(db.Numeric(10, 2), nullable=False)

    # Fresh deposit taken from the incoming borrower.
    new_deposit_collected = db.Column(db.Numeric(10, 2), nullable=False)

    notes = db.Column(db.Text, nullable=True)

    booking = db.relationship("Booking", backref="transfers")
    from_borrower = db.relationship("Borrower", foreign_keys=[from_borrower_id])
    to_borrower = db.relationship("Borrower", foreign_keys=[to_borrower_id])

    def __repr__(self):
        return f"<BookingTransfer booking={self.booking_id} {self.from_borrower_id}->{self.to_borrower_id}>"


class Notification(db.Model):
    """Log of reminder emails/texts sent, so we don't nudge the same person twice."""

    __tablename__ = "notifications"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    booking_id = db.Column(db.String(36), db.ForeignKey("bookings.id"), nullable=False)
    type = db.Column(db.String(30), nullable=False)  # due_soon | overdue | reminder_2
    sent_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    booking = db.relationship("Booking", backref="notifications")
