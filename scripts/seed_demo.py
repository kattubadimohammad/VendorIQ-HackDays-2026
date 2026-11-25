"""Create a clean, synthetic-only VendorIQ demo database.

Usage:
    python scripts/seed_demo.py --db data/vendoriq_demo.sqlite

This script refuses the default production database name and refuses to add data
to a database that already contains briefs or suppliers.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core.models import (
    ScoringWeights,
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
)
from db.repository import VendorIQRepository
from ui.data_service import DashboardDataService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed an isolated synthetic VendorIQ demo database.")
    parser.add_argument("--db", required=True, help="Explicit path to a NEW or empty SQLite database file.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db).expanduser().resolve()

    if db_path.name.lower() == "vendoriq.sqlite":
        raise SystemExit("Refusing to seed vendoriq.sqlite. Choose a separate demo database filename.")
    if db_path.exists() and db_path.stat().st_size > 0:
        repo = VendorIQRepository(db_path=str(db_path))
        repo.initialize()
        if repo.list_suppliers() or repo.list_briefs():
            raise SystemExit("Refusing to modify a database that already contains suppliers or briefs.")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    repo = VendorIQRepository(db_path=str(db_path))
    repo.initialize()

    brief = SourcingBrief(
        brief_id="BRIEF-SYNTHETIC-PACKAGING-01",
        title="Synthetic Flexible Barrier Packaging Sourcing",
        buyer_organization="VendorIQ Synthetic Demo",
        description=(
            "Synthetic demonstration brief for flexible barrier packaging suppliers "
            "in India. This dataset is fictional and is not a real procurement request."
        ),
        criteria=SourcingCriteria(
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
            weights=ScoringWeights(
                industry_relevance=0.35,
                geographic_fit=0.25,
                company_size_fit=0.20,
                business_signals=0.10,
                data_completeness=0.10,
            ),
        ),
    )
    repo.create_brief(brief)

    suppliers = [
        SupplierProfile(
            supplier_id="DEMO-SYNTH-ALPHA",
            company_name="Alpha Barrier Films (Synthetic)",
            domain="alpha-barrier-films.example",
            description=(
                "Fictional synthetic manufacturer profile for flexible packaging, "
                "barrier films, BOPP specialty films, and laminated pouches."
            ),
            headquarters_country="India",
            headquarters_city="Pune",
            employee_range="201-500",
            industries=["Packaging and Containers Manufacturing"],
            is_synthetic=True,
        ),
        SupplierProfile(
            supplier_id="DEMO-SYNTH-BETA",
            company_name="Beta Green Packaging (Synthetic)",
            domain="beta-green-packaging.example",
            description=(
                "Fictional synthetic manufacturer profile for sustainable packaging "
                "containers and cartons. No barrier-film capability is asserted."
            ),
            headquarters_country="India",
            headquarters_city="Surat",
            employee_range="51-200",
            industries=["Packaging and Containers Manufacturing"],
            is_synthetic=True,
        ),
        SupplierProfile(
            supplier_id="DEMO-SYNTH-GAMMA",
            company_name="Gamma Flexible Films (Synthetic)",
            domain="gamma-flexible-films.example",
            description=(
                "Fictional synthetic producer profile for flexible packaging films "
                "and laminated rolls. No certifications are asserted."
            ),
            headquarters_country="India",
            headquarters_city="Mumbai",
            employee_range="51-200",
            industries=["Packaging and Containers Manufacturing"],
            is_synthetic=True,
        ),
    ]

    for supplier in suppliers:
        repo.create_supplier(supplier)

    service = DashboardDataService(repository=repo)
    scores = service.reevaluate_suppliers(
        brief_id=brief.brief_id,
        weights=brief.criteria.weights,
        notes="Synthetic demo seed evaluation; not a real supplier qualification.",
    )

    print("Synthetic VendorIQ demo database created.")
    print(f"Database: {db_path}")
    print(f"Brief: {brief.brief_id}")
    print(f"Suppliers seeded: {len(suppliers)}")
    print(f"Evaluation scorecards created: {len(scores)}")
    print("All supplier profiles are synthetic. No external API calls were made.")
    print("Start the app with VENDORIQ_DB_PATH set to this exact database path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
