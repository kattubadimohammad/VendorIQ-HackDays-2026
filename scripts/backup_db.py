import shutil
import datetime
import sqlite3
from pathlib import Path

ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
src = Path("vendoriq.sqlite")
dst = Path(f"vendoriq_backup_{ts}.sqlite")

shutil.copy2(src, dst)

conn = sqlite3.connect(dst)
suppliers = conn.execute("SELECT COUNT(*) FROM supplier_profiles").fetchone()[0]
briefs = conn.execute("SELECT COUNT(*) FROM sourcing_briefs").fetchone()[0]
evals = conn.execute("SELECT COUNT(*) FROM supplier_evaluations").fetchone()[0]
evidence = conn.execute("SELECT COUNT(*) FROM supplier_evidence").fetchone()[0]
scoring_runs = conn.execute("SELECT COUNT(*) FROM scoring_runs").fetchone()[0]
conn.close()

print(f"Verified Backup {dst} created successfully:")
print(f"  - Suppliers: {suppliers}")
print(f"  - Sourcing Briefs: {briefs}")
print(f"  - Scoring Runs: {scoring_runs}")
print(f"  - Supplier Evaluations: {evals}")
print(f"  - Evidence Records: {evidence}")
