"""
Populates sample data so you can try the app immediately.
Run after init_db.py:
    python seed.py
"""
from app import create_app
from extensions import db
from models import EquipmentModel, Item, Borrower

app = create_app()

with app.app_context():
    if EquipmentModel.query.first():
        print("Data already exists - skipping seed.")
    else:
        dslr = EquipmentModel(
            name="Canon EOS 90D", type="camera",
            deposit_amount=100, late_fee_per_day=5, standard_loan_days=3,
        )
        projector = EquipmentModel(
            name="Epson Projector", type="projector",
            deposit_amount=60, late_fee_per_day=8, standard_loan_days=2,
        )
        mic = EquipmentModel(
            name="Shure SM58 Mic", type="mic",
            deposit_amount=20, late_fee_per_day=2, standard_loan_days=2,
        )
        tripod = EquipmentModel(
            name="Manfrotto Tripod", type="tripod",
            deposit_amount=15, late_fee_per_day=1, standard_loan_days=5,
        )
        db.session.add_all([dslr, projector, mic, tripod])
        db.session.flush()  # so the models get IDs before we reference them

        items = [
            Item(equipment_model_id=dslr.id, asset_tag="DSLR-01"),
            Item(equipment_model_id=dslr.id, asset_tag="DSLR-02"),
            Item(equipment_model_id=dslr.id, asset_tag="DSLR-03"),
            Item(equipment_model_id=projector.id, asset_tag="PROJ-01"),
            Item(equipment_model_id=projector.id, asset_tag="PROJ-02"),
            Item(equipment_model_id=mic.id, asset_tag="MIC-01"),
            Item(equipment_model_id=mic.id, asset_tag="MIC-02"),
            Item(equipment_model_id=mic.id, asset_tag="MIC-03"),
            Item(equipment_model_id=mic.id, asset_tag="MIC-04"),
            Item(equipment_model_id=tripod.id, asset_tag="TRIPOD-01"),
            Item(equipment_model_id=tripod.id, asset_tag="TRIPOD-02"),
        ]
        db.session.add_all(items)

        borrowers = [
            Borrower(name="Asha Rao", student_id="S1001", email="asha@college.edu",
                      club_affiliation="Film Society"),
            Borrower(name="Dev Mehta", student_id="S1002", email="dev@college.edu",
                      club_affiliation="Dance Club"),
            Borrower(name="Priya Nair", student_id="S1003", email="priya@college.edu"),
        ]
        db.session.add_all(borrowers)

        db.session.commit()
        print("Seed data created: 4 equipment models, 11 items, 3 borrowers.")
