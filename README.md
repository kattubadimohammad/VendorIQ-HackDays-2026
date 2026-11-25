# VendorIQ — Your AI Sourcing Partner

VendorIQ is an AI-assisted sourcing and procurement intelligence prototype built for **Homingo Hack Days 2026**. It helps buyers structure a sourcing brief, discover and evaluate supplier candidates, inspect evidence quality, prepare RFQs, ingest quotations, compare commercial and technical terms, and record human-controlled decisions.

> **Important:** VendorIQ is a decision-support prototype. Supplier records and claims must be independently verified before commercial use. An evaluated candidate is not a verified, certified, qualified, or approved supplier. VendorIQ does not automatically contact suppliers, issue purchase orders, or execute awards.

## Workflow

1. **Sourcing brief** — capture requirements, target industry/geography, and scoring weights.
2. **Supplier discovery** — identify and shortlist potential supplier candidates.
3. **Supplier evaluation** — apply deterministic, explainable multi-factor scoring.
4. **Evidence & trust** — classify information as verified, self-reported, unverified, or missing.
5. **RFQ preparation** — generate supplier-specific RFQ documents and support review/approval.
6. **Quotation analysis** — ingest PDF or XLSX quotations (or enter data manually), review extracted fields, normalize supported currencies, disclose incomplete landed costs, assess technical compliance, and record human decisions.

The dashboard includes Executive Overview, Sourcing Brief, Supplier Discovery, Supplier Evaluation, Evidence & Trust, Supplier Comparison, AI Sourcing Copilot, RFQ Preparation, and Quotations & Analysis.

## Governance and limitations

- Deterministic supplier scoring is separate from AI assistance; AI does not set supplier scores.
- Missing information is not proof of poor supplier quality.
- Self-reported claims are not independent verification.
- Mandatory technical failures cannot be offset by a lower price.
- Exchange-rate benchmarks are static reference values, not live market rates.
- Missing freight, duties, taxes, or other landed-cost components are disclosed, not invented.
- The current quotation parser targets text-based PDF and OpenXML XLSX; scanned PDFs and legacy binary XLS are not supported.
- Quotation extraction requires buyer review and confirmation.
- No automated supplier email, purchase order creation, or award execution.

## Technology

Python, Streamlit, SQLite, Pydantic, Pandas, Plotly, python-docx, ReportLab, and pytest. Pinned dependencies are listed in requirements.txt.

## Run locally

Clone the repository:

    git clone https://github.com/kattubadimohammad/VendorIQ-HackDays-2026.git
    cd VendorIQ-HackDays-2026

Create and activate a virtual environment (Windows PowerShell):

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1

Install dependencies:

    python -m pip install --upgrade pip
    pip install -r requirements.txt

Copy .env.example to .env. The default is OFFLINE mode; API keys are not required for the offline fallback. Never commit .env or share secrets.

Start the dashboard:

    streamlit run app.py

Open the local URL printed by Streamlit (normally http://localhost:8501).

## Tests and database isolation

Run tests with:

    pytest -q

For manual QA, set VENDORIQ_DB_PATH to a dedicated temporary SQLite file rather than the default database. Example PowerShell:

    $env:VENDORIQ_DB_PATH = "data/qa_local.sqlite"
    streamlit run app.py

The data/, storage/, SQLite databases, local settings, and secrets are intentionally excluded from Git.

## Documentation

- Problem statement: docs/PROBLEM_STATEMENT.md
- Architecture: docs/ARCHITECTURE.md
- Demo script: docs/DEMO_SCRIPT.md
- Submission checklist: docs/SUBMISSION_CHECKLIST.md
- Demo data notes: demo_data/DEMO_DATA_NOTES.md

## Responsible use

Use synthetic or explicitly authorized data for demonstrations. Do not upload confidential employer information, personal data, API keys, supplier quotations, or internal procurement records to this public repository.
