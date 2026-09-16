from datetime import date, datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from booking_logic import (
    BookingError,
    cancel_booking,
    check_out_booking,
    create_booking,
    due_soon_bookings,
    overdue_bookings,
    return_booking,
    transfer_booking,
)
from extensions import db
from models import Booking, Borrower, EquipmentModel, Item

bp = Blueprint("main", __name__)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


@bp.route("/")
def index():
    """Home page: every equipment model, with availability for a chosen date range."""
    start = request.args.get("start") or date.today().isoformat()
    end = request.args.get("end") or date.today().isoformat()
    start_date = _parse_date(start)
    end_date = _parse_date(end)

    models = EquipmentModel.query.order_by(EquipmentModel.type, EquipmentModel.name).all()
    rows = [
        {
            "model": m,
            "available": m.available_unit_count(start_date, end_date),
            "total": len(m.items),
        }
        for m in models
    ]
    return render_template(
        "index.html", rows=rows, start=start, end=end
    )


@bp.route("/book", methods=["GET", "POST"])
def book():
    models = EquipmentModel.query.order_by(EquipmentModel.name).all()
    borrowers = Borrower.query.filter_by(active=True).order_by(Borrower.name).all()

    if request.method == "POST":
        try:
            booking = create_booking(
                borrower_id=request.form["borrower_id"],
                equipment_model_id=request.form["equipment_model_id"],
                start_date=_parse_date(request.form["start_date"]),
                end_date=_parse_date(request.form["end_date"]),
            )
            flash(
                f"Booked {booking.item.equipment_model.name} "
                f"(unit {booking.item.asset_tag}) for {booking.borrower.name}. "
                f"Deposit of ${booking.item.equipment_model.deposit_amount} due at pickup.",
                "success",
            )
            return redirect(url_for("main.bookings"))
        except BookingError as e:
            flash(str(e), "error")

    prefill_model = request.args.get("equipment_model_id", "")
    prefill_start = request.args.get("start", date.today().isoformat())
    prefill_end = request.args.get("end", date.today().isoformat())

    return render_template(
        "book.html",
        models=models,
        borrowers=borrowers,
        prefill_model=prefill_model,
        prefill_start=prefill_start,
        prefill_end=prefill_end,
    )


@bp.route("/bookings")
def bookings():
    """Staff view: everything currently reserved or checked out."""
    active = (
        Booking.query.filter(Booking.status.in_(["reserved", "checked_out"]))
        .order_by(Booking.start_date)
        .all()
    )
    return render_template("bookings.html", bookings=active, today=date.today())


@bp.route("/bookings/<booking_id>/checkout", methods=["POST"])
def checkout(booking_id):
    try:
        b = check_out_booking(booking_id)
        flash(
            f"Checked out {b.item.equipment_model.name} ({b.item.asset_tag}) to "
            f"{b.borrower.name}. Deposit collected: ${b.deposit_collected}.",
            "success",
        )
    except BookingError as e:
        flash(str(e), "error")
    return redirect(url_for("main.bookings"))


@bp.route("/bookings/<booking_id>/return", methods=["POST"])
def do_return(booking_id):
    try:
        b = return_booking(booking_id, grace_period_hours=current_app.config["GRACE_PERIOD_HOURS"])
        if b.late_fee_charged and b.late_fee_charged > 0:
            flash(
                f"Returned late. Late fee: ${b.late_fee_charged}. "
                f"Refunded to {b.borrower.name}: ${b.deposit_refunded}.",
                "warning",
            )
        else:
            flash(
                f"Returned on time. Full deposit refunded to {b.borrower.name}: "
                f"${b.deposit_refunded}.",
                "success",
            )
    except BookingError as e:
        flash(str(e), "error")
    return redirect(url_for("main.bookings"))


@bp.route("/bookings/<booking_id>/transfer", methods=["GET", "POST"])
def transfer(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.status != "checked_out":
        flash("Only an active (checked-out) loan can be transferred.", "error")
        return redirect(url_for("main.bookings"))

    # Anyone active except whoever currently holds it.
    borrowers = (
        Borrower.query.filter(Borrower.active.is_(True), Borrower.id != booking.borrower_id)
        .order_by(Borrower.name)
        .all()
    )

    if request.method == "POST":
        try:
            updated = transfer_booking(
                booking_id=booking_id,
                to_borrower_id=request.form["to_borrower_id"],
                grace_period_hours=current_app.config["GRACE_PERIOD_HOURS"],
                notes=request.form.get("notes") or None,
            )
            flash(
                f"Transferred {updated.item.equipment_model.name} ({updated.item.asset_tag}) "
                f"to {updated.borrower.name}. Due date unchanged: {updated.due_date}.",
                "success",
            )
            return redirect(url_for("main.bookings"))
        except BookingError as e:
            flash(str(e), "error")

    return render_template("transfer.html", booking=booking, borrowers=borrowers)


@bp.route("/bookings/<booking_id>/cancel", methods=["POST"])
def do_cancel(booking_id):
    try:
        cancel_booking(booking_id)
        flash("Booking cancelled.", "success")
    except BookingError as e:
        flash(str(e), "error")
    return redirect(url_for("main.bookings"))


@bp.route("/overdue")
def overdue():
    """Dashboard for the nudge job / front desk: what's overdue, what's due soon."""
    return render_template(
        "overdue.html",
        overdue=overdue_bookings(),
        due_soon=due_soon_bookings(days_ahead=1),
        today=date.today(),
    )


@bp.route("/borrowers/new", methods=["GET", "POST"])
def new_borrower():
    if request.method == "POST":
        b = Borrower(
            name=request.form["name"],
            student_id=request.form["student_id"],
            email=request.form["email"],
            phone=request.form.get("phone"),
            club_affiliation=request.form.get("club_affiliation"),
        )
        db.session.add(b)
        db.session.commit()
        flash(f"Added borrower {b.name}.", "success")
        return redirect(url_for("main.book"))
    return render_template("new_borrower.html")
