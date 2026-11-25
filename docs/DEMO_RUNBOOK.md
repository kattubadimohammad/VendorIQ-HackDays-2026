# VendorIQ Phase 15 — Demo Runbook

**Purpose:** rehearse a 5–7 minute, repeatable, synthetic-only demonstration.

## Safety first

1. Do not use the production database `vendoriq.sqlite` for rehearsal.
2. Set `VENDORIQ_DB_PATH` to a new isolated file before launching Streamlit.
3. Confirm the sidebar labels the active database as **Isolated**.
4. Use only synthetic demo suppliers and quotations. Never use Walmart data, real supplier quotations, personal data, or confidential files.
5. Keep API mode OFFLINE. Do not expose `.env`, API keys, database files, or uploads.

Example PowerShell (from the repository root):

```powershell
$env:APP_MODE = "OFFLINE"
$env:VENDORIQ_DB_PATH = "data\vendoriq_demo_qa.sqlite"
..venv\Scripts\streamlit.exe run app.py
```

First create the synthetic-only database with the committed seeder. From the repository root run:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
.\.venv\Scripts\python.exe scripts\seed_demo.py --db data\vendoriq_demo.sqlite
$env:APP_MODE = "OFFLINE"
$env:VENDORIQ_DB_PATH = (Resolve-Path data\vendoriq_demo.sqlite).Path
.\.venv\Scripts\streamlit.exe run app.py
```

The seeder refuses the default production filename and refuses to add records to a database that already contains suppliers or briefs. It creates one fictional sourcing brief, three fictional supplier profiles, and deterministic evaluation scorecards. Confirm the sidebar shows the isolated database before proceeding. Do not switch to the production database to make the demo look populated.

## Scenario

A buyer is sourcing flexible barrier packaging for India. Three fictional supplier candidates submit illustrative quotations. The mandatory OTR ceiling for the scenario is 1.0 cc/m²/day.

Use `demo_data/quotation_scenario.csv` as the source sheet. It is a reference sheet, not a supported upload format. Enter the values manually in **Quotations & Analysis → Ingestion & Ingestion Preview → Manual Proposal Entry**.

| Supplier alias | Currency / unit price | OTR claim | Scenario result |
|---|---:|---:|---|
| Alpha Barrier Films | USD 1.52 | 0.8 | Claim within scenario limit; TDS absent |
| Beta Green Packaging | USD 1.28 | 1.6 | Fails mandatory OTR limit despite lowest price |
| Gamma Flexible Films | INR 139.00 | 0.9 | Claim within scenario limit; freight/duties missing |

All three names, prices, specifications, and claims are fictional. They are not market benchmarks, supplier recommendations, or independently verified product performance.

## Manual-entry guidance

For each supplier:
- Select the correct supplier and active sourcing brief.
- Enter the quoted unit price, currency, quantity, MOQ, lead time, Incoterm, and payment terms from the CSV.
- Enter the illustrative structure, thickness, OTR, WVTR, and cited standards.
- Leave **Authentic TDS Attached** unchecked because the scenario does not include actual TDS documents.
- Keep freight and duties/taxes at zero only as the UI's numeric input; narrate that the supplier did not provide those costs. Do not describe zero as a confirmed free-shipping or duty-free quote.
- Submit the manual quotation and complete the buyer review/confirmation steps required by the UI.
- Do not mark supplier capability or performance claims as independently VERIFIED.

**Important:** The UI's default manual form may contain example values. Replace every relevant field for each supplier. Do not leave defaults in place accidentally.

## Demo sequence

1. **Executive Overview:** state the sourcing brief and clarify candidate status.
2. **Sourcing Brief:** explain requirement and scoring criteria.
3. **Supplier Discovery / Evaluation:** show candidate discovery and deterministic score; explain missing data is not negative proof.
4. **Evidence & Trust:** distinguish self-reported, unverified, missing, and independently verified evidence.
5. **RFQ Preparation:** show draft and approval controls; do not actually send anything.
6. **Quotations & Analysis:** manually enter the three synthetic quotations; review provenance and buyer confirmations.
7. **Comparison / Recommendations:** demonstrate that Beta's lower price does not compensate for a mandatory OTR failure. Explain that Alpha/Gamma claims are not verified without TDS/lab evidence.
8. **Human Approval:** show the approval gate, but do not claim a real supplier award or create a purchase order.

## Expected narration

"VendorIQ makes the trade-offs visible. Beta is the lowest illustrative price, but its OTR claim fails the mandatory scenario threshold. Alpha and Gamma have claims within the threshold, but no TDS is attached, so the claims are not independently verified. Gamma's freight and duties are not provided, so its landed cost is incomplete. A buyer must obtain evidence and approve any final decision."

## Stop conditions

Stop the demo rather than improvising if:
- the active database is not clearly isolated;
- supplier records are absent or real supplier data appears;
- quotation defaults have not been replaced;
- the UI implies unsupported verification;
- the comparison ranks a technically non-compliant quotation as eligible;
- missing landed-cost components are silently treated as zero confirmed cost.

## Known preparation dependency

The repository now contains a deterministic synthetic-only database bootstrap and synthetic quotation reference data. The bootstrap still needs to be executed locally and followed by a browser rehearsal before calling the demo fully rehearsed.
