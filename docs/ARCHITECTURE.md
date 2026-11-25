# VendorIQ — Architecture Overview

## Application structure
VendorIQ is a Python application with a Streamlit user interface and SQLite persistence.

    Streamlit UI (app.py)
        ui/views/          Dashboard views
        ui/components.py   Shared UI components and workflow guidance
        ui/data_service.py Dashboard-facing service facade
        services/          Business workflows
        core/              Domain models, validation, provenance
        db/                SQLite connection, schema, repository
        utils/             File validation and local file storage

## Main layers
- **UI:** app.py configures Streamlit and routes users to nine dashboard views. ui/views contains executive, brief, discovery, evaluation, evidence, comparison, copilot, RFQ, and quotation views.
- **Domain:** core/models.py defines validated domain models; core/provenance.py defines provenance classifications; core/exceptions.py contains application-specific errors.
- **Services:** evaluation_engine, discovery_service, evidence_service, copilot_service, rfq_generator, rfq_service, quotation_extractor, quotation_service, and quotation_comparison implement business workflows.
- **Persistence:** db/schema.sql defines SQLite schema; db/connection.py manages connections and path selection; db/repository.py provides persistence operations.
- **Utilities:** utils/file_storage.py validates and stores supported uploaded files.

## Workflow
    Sourcing Brief -> Supplier Discovery -> Supplier Evaluation
      -> Evidence & Trust -> RFQ Preparation
      -> Quotations & Analysis -> Human decision

The dashboard also provides an Executive Overview, Supplier Comparison, and AI Sourcing Copilot.

## Trust and decision controls
- Supplier evaluation is deterministic and explainable; AI assistance does not overwrite evaluation scores.
- Provenance distinguishes verified evidence, AI assessment, buyer-specified information, unverified/missing information, and synthetic data.
- Buyer confirmation does not turn a claim into independently verified evidence.
- Mandatory technical compliance is non-compensable: price cannot offset a mandatory technical failure.
- Missing freight, duties, or other landed-cost elements are disclosed rather than fabricated.
- Static FX benchmarks are reference values, not live rates.
- Quotation fields require buyer review and confirmation.
- RFQ approval and supplier award are human-controlled. The application does not automatically email suppliers or create purchase orders.

## Quotation document support
The current deterministic extraction implementation targets text-based PDF and OpenXML XLSX files. It records field provenance such as page/cell references and flags missing or ambiguous fields for human review. Scanned/raster PDFs and legacy binary XLS files are not supported by the implemented parser.

## Security and deployment
Keep .env, API keys, Streamlit secrets, SQLite databases, backups, uploaded quotations, and generated RFQ documents out of source control. Use synthetic data for public demos. Configure persistent storage for hosted deployments; ephemeral local storage may be lost on restart. Do not connect confidential employer systems or data without explicit authorization.
