import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://av_admin:av_password@localhost:5432/av_room",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

    # Grace period before a late fee starts accruing, in hours.
    GRACE_PERIOD_HOURS = float(os.environ.get("GRACE_PERIOD_HOURS", 2))
