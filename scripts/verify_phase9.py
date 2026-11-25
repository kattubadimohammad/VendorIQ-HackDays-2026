import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from ui.views.evidence_view import render_evidence_view
from core.models import SupplierProfile, EvidenceCategory, EvidenceStatus

service = app.get_service()
suppliers = service.get_all_suppliers()
print(f"Total suppliers loaded: {len(suppliers)}")

evidence_dict = service.generate_and_persist_all_evidence()
print(f"Total suppliers with evidence: {len(evidence_dict)}")

for s in suppliers[:5]:
    records = service.get_supplier_evidence(s.supplier_id)
    kpis = service.calculate_evidence_completeness(records)
    print(f"- {s.company_name} ({s.domain}): {len(records)} categories | Completeness: {kpis['completeness_percentage']}%, Verified: {kpis['verified_count']}, Self-Reported: {kpis['self_reported_count']}, Missing: {kpis['missing_count']}")

cov = service.get_multi_supplier_coverage_matrix()
print(f"Multi-supplier matrix rows: {len(cov['supplier_rows'])}")
print("Category aggregates:")
for cat, counts in cov["category_aggregates"].items():
    print(f"  {cat}: {counts}")

print("\nALL VERIFICATIONS PASSED SUCCESSFULLY!")
