-- ==============================================================================
-- VendorIQ Database Schema (SQLite)
-- Phase 4: Persistence Layer
-- ==============================================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------------------
-- 1. Sourcing Briefs
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sourcing_briefs (
    brief_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    buyer_organization TEXT NOT NULL,
    description TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    budget_min REAL,
    budget_max REAL,
    timeline_weeks INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL
);

-- ------------------------------------------------------------------------------
-- 2. Supplier Profiles
-- Company-level data only; personal email/PII prohibited.
-- Enforces domain uniqueness for duplicate handling and caching.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_profiles (
    supplier_id TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    description TEXT,
    headquarters_country TEXT NOT NULL,
    headquarters_city TEXT,
    employee_count INTEGER,
    employee_range TEXT,
    industries_json TEXT NOT NULL DEFAULT '[]',
    certifications_json TEXT NOT NULL DEFAULT '[]',
    annual_revenue_usd REAL,
    year_founded INTEGER,
    business_signals_json TEXT NOT NULL DEFAULT '[]',
    corporate_contact_email TEXT,
    corporate_website TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    is_synthetic INTEGER NOT NULL DEFAULT 0,
    overall_provenance TEXT NOT NULL DEFAULT 'UNVERIFIED_OR_MISSING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supplier_domain ON supplier_profiles(domain);

-- ------------------------------------------------------------------------------
-- 3. Scoring Runs
-- Tracks execution of multi-dimensional evaluation for a sourcing brief.
-- Stores the configured weights (35% / 25% / 20% / 10% / 10% or customized).
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scoring_runs (
    run_id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES sourcing_briefs(brief_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scoring_runs_brief ON scoring_runs(brief_id);

-- ------------------------------------------------------------------------------
-- 4. Supplier Evaluations
-- Scores for individual suppliers within a scoring run.
-- Stores composite total score and detailed factor breakdown with provenance.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    total_score REAL NOT NULL,
    data_completeness_percentage REAL NOT NULL,
    factor_scores_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'AI_ASSESSMENT',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES scoring_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (supplier_id) REFERENCES supplier_profiles(supplier_id) ON DELETE CASCADE,
    CONSTRAINT uq_run_supplier UNIQUE (run_id, supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluations_run ON supplier_evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_supplier ON supplier_evaluations(supplier_id);

-- ------------------------------------------------------------------------------
-- 5. RFQ Documents
-- Draft and finalized Request for Quotation documents generated for suppliers.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rfq_documents (
    rfq_id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    target_supplier_ids_json TEXT NOT NULL,
    title TEXT NOT NULL,
    scope_of_work TEXT NOT NULL,
    technical_requirements_json TEXT NOT NULL,
    submission_deadline TEXT NOT NULL,
    response_instructions TEXT NOT NULL,
    evaluation_criteria_summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES sourcing_briefs(brief_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rfqs_brief ON rfq_documents(brief_id);

-- ------------------------------------------------------------------------------
-- 6. Supplier Evidence & Intelligence
-- Structured evidence records tracking verification status across 7 categories.
-- Enforces Truth in Evidence boundaries (VERIFIED, SELF_REPORTED, UNVERIFIED, MISSING).
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_evidence (
    evidence_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    category TEXT NOT NULL,
    claim TEXT NOT NULL,
    status TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT,
    verified_at TEXT,
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (supplier_id) REFERENCES supplier_profiles(supplier_id) ON DELETE CASCADE,
    CONSTRAINT uq_supplier_category UNIQUE (supplier_id, category)
);

CREATE INDEX IF NOT EXISTS idx_evidence_supplier ON supplier_evidence(supplier_id);
CREATE INDEX IF NOT EXISTS idx_evidence_category ON supplier_evidence(category);
CREATE INDEX IF NOT EXISTS idx_evidence_status ON supplier_evidence(status);

-- ------------------------------------------------------------------------------
-- 7. Supplier Shortlists & Shortlist Items (Phase 10)
-- Allows buyers to curate, annotate, track status, and export shortlisted vendors.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_shortlists (
    shortlist_id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES sourcing_briefs(brief_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shortlists_brief ON supplier_shortlists(brief_id);

CREATE TABLE IF NOT EXISTS shortlist_items (
    item_id TEXT PRIMARY KEY,
    shortlist_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CANDIDATE',
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (shortlist_id) REFERENCES supplier_shortlists(shortlist_id) ON DELETE CASCADE,
    FOREIGN KEY (supplier_id) REFERENCES supplier_profiles(supplier_id) ON DELETE CASCADE,
    CONSTRAINT uq_shortlist_supplier UNIQUE (shortlist_id, supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_shortlist_items_list ON shortlist_items(shortlist_id);
CREATE INDEX IF NOT EXISTS idx_shortlist_items_supp ON shortlist_items(supplier_id);

-- ------------------------------------------------------------------------------
-- 8. Supplier RFQs & RFQ Audit Trail (Phase 11)
-- Structured, supplier-specific RFQs with 7 lifecycle states, human gates,
-- document tracking, and complete audit trail.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_rfqs (
    rfq_id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL,
    shortlist_id TEXT,
    supplier_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    buyer_info_json TEXT NOT NULL,
    supplier_target_json TEXT NOT NULL,
    product_requirements_json TEXT NOT NULL,
    commercial_requirements_json TEXT NOT NULL,
    requested_documents_json TEXT NOT NULL,
    docx_path TEXT,
    pdf_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES sourcing_briefs(brief_id) ON DELETE CASCADE,
    FOREIGN KEY (supplier_id) REFERENCES supplier_profiles(supplier_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_supplier_rfqs_brief ON supplier_rfqs(brief_id);
CREATE INDEX IF NOT EXISTS idx_supplier_rfqs_supplier ON supplier_rfqs(supplier_id);
CREATE INDEX IF NOT EXISTS idx_supplier_rfqs_status ON supplier_rfqs(status);

CREATE TABLE IF NOT EXISTS rfq_audit_trail (
    audit_id TEXT PRIMARY KEY,
    rfq_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    comments TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (rfq_id) REFERENCES supplier_rfqs(rfq_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rfq_audit_rfq ON rfq_audit_trail(rfq_id);

-- ------------------------------------------------------------------------------
-- 9. Supplier Quotations & Quotation Audit Trail (Phase 12)
-- Stores structured supplier quotation submissions, extraction provenance,
-- commercial parameters, technical compliance, and immutable audit history.
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supplier_quotations (
    quotation_id TEXT PRIMARY KEY,
    rfq_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    submission_date TEXT NOT NULL,
    validity_end_date TEXT,
    file_type TEXT NOT NULL,
    raw_file_path TEXT,
    file_hash_sha256 TEXT,
    commercial_json TEXT NOT NULL,
    compliance_json TEXT NOT NULL,
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (rfq_id) REFERENCES supplier_rfqs(rfq_id) ON DELETE CASCADE,
    FOREIGN KEY (supplier_id) REFERENCES supplier_profiles(supplier_id) ON DELETE CASCADE,
    FOREIGN KEY (brief_id) REFERENCES sourcing_briefs(brief_id) ON DELETE CASCADE,
    CONSTRAINT uq_rfq_quotation_version UNIQUE (rfq_id, version)
);

CREATE INDEX IF NOT EXISTS idx_quotations_rfq ON supplier_quotations(rfq_id);
CREATE INDEX IF NOT EXISTS idx_quotations_supplier ON supplier_quotations(supplier_id);
CREATE INDEX IF NOT EXISTS idx_quotations_brief ON supplier_quotations(brief_id);
CREATE INDEX IF NOT EXISTS idx_quotations_status ON supplier_quotations(status);

CREATE TABLE IF NOT EXISTS quotation_audit_trail (
    audit_id TEXT PRIMARY KEY,
    quotation_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    action TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    comments TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (quotation_id) REFERENCES supplier_quotations(quotation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_quotation_audit_quote ON quotation_audit_trail(quotation_id);
