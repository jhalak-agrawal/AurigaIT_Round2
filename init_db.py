"""
Creates all tables, then adds the Postgres-specific guardrail that makes
double-booking the same item structurally impossible - not just checked
in application code, but rejected by the database itself.

Run this once after the Postgres container is up:
    python init_db.py
"""

from sqlalchemy import text

from app import create_app
from extensions import db

app = create_app()

with app.app_context():
    db.create_all()

    with db.engine.begin() as conn:
        # btree_gist lets a GiST exclusion constraint compare a plain
        # equality column (item_id) alongside a range column (date_range).
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist;"))

        # A generated column that mirrors start_date/due_date as a range,
        # so Postgres can index and compare it natively.
        conn.execute(
            text(
                """
                ALTER TABLE bookings
                ADD COLUMN IF NOT EXISTS date_range daterange
                GENERATED ALWAYS AS (daterange(start_date, due_date, '[]')) STORED;
                """
            )
        )

        # The actual guardrail: reject any new row whose (item_id, date_range)
        # overlaps an existing row for the same item, among active bookings.
        # This is what closes the race condition that application-level
        # checks alone can't fully close.
        conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'no_overlapping_bookings'
                    ) THEN
                        ALTER TABLE bookings
                        ADD CONSTRAINT no_overlapping_bookings
                        EXCLUDE USING gist (
                            item_id WITH =,
                            date_range WITH &&
                        )
                        WHERE (status IN ('reserved', 'checked_out'));
                    END IF;
                END $$;
                """
            )
        )

    print("Database initialized: tables created, exclusion constraint in place.")
