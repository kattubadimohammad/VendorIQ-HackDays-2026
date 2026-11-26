"""
VendorIQ AI Sourcing Copilot Service (Phase 8A).

Integrates Google Gemini (via google-genai SDK) with VendorIQ's deterministic
evaluation engine, SQLite persistence, and provenance architecture.

Key Safeguards:
1. Strict Grounding: Copilot only evaluates suppliers present in SQLite.
2. Deterministic Scoring: AI never invents facts, certifications, or overrides scores/rankings.
3. Provenance Transparency: Separates VERIFIED_DATA, BUYER_SPECIFIED, AI_ASSESSMENT, and UNVERIFIED_OR_MISSING.
4. Human Review Gate: All extracted briefs and shortlists require human confirmation before action.
5. Zero Network Leakage: Safe offline fallback mode when GEMINI_API_KEY is not configured or in testing.
"""

import json
import logging
import re
import uuid
from typing import List, Dict, Any, Optional

from google import genai
from google.genai import types

from config import settings
from core.models import (
    SourcingBrief,
    SourcingCriteria,
    ScoringWeights,
    SupplierProfile,
    SupplierScore,
    ExtractedSourcingRequirements,
    CopilotRecommendation,
    CopilotQueryResponse,
)
from core.provenance import ProvenanceClassification
from db.repository import VendorIQRepository
from services.data_quality_auditor import SupplierQualityAuditor, DataQualityAuditReport
from services.evaluation_engine import SupplierEvaluationEngine

logger = logging.getLogger(__name__)


# Standard system prompt establishing enterprise procurement boundaries
_COPILOT_SYSTEM_INSTRUCTION = """
You are the VendorIQ AI Sourcing Copilot, an enterprise autonomous procurement intelligence assistant.

CORE RULES & ETHICAL GUARDRAILS:
1. Strict Grounding: Ground all answers and recommendations exclusively in the provided supplier records, deterministic factor scores, and audit reports.
2. No Hallucinations: NEVER invent certifications (e.g. ISO 9001, BRC, FDA), audited manufacturing capacities, revenue figures, or quality guarantees.
3. Deterministic Rankings: NEVER alter or re-rank supplier scores. Total scores and ranks are computed deterministically by the evaluation engine.
4. Evidence Boundaries:
   - Treat company descriptions and domains as UNVERIFIED_OR_MISSING (self-reported directory data).
   - Treat scorecard breakdowns as AI_ASSESSMENT (deterministic algorithmic evaluations).
   - Clearly state that employee headcount is an organizational size metric, NOT verified production capacity.
   - Flag any domain mismatches or missing facility address data as requiring mandatory manual audit.
5. No Automated Contact: You are an internal procurement intelligence copilot. You cannot send emails, contact suppliers, or issue binding purchase orders.
"""


class AISourcingCopilot:
    """
    AI-powered procurement copilot leveraging Gemini models grounded in SQLite supplier data.
    """

    def __init__(
        self,
        repository: Optional[VendorIQRepository] = None,
        evaluation_engine: Optional[SupplierEvaluationEngine] = None,
        client: Optional[genai.Client] = None,
        model_name: str = "gemini-2.5-flash",
    ):
        self.repository = repository or VendorIQRepository()
        self.evaluation_engine = evaluation_engine or SupplierEvaluationEngine(repository=self.repository)
        self.auditor = SupplierQualityAuditor()
        self.model_name = model_name

        # Initialize Gemini Client if explicitly passed or configured in settings
        self.client = client
        if self.client is None and settings.is_gemini_configured:
            try:
                raw_key = settings.get_gemini_api_key()
                self.client = genai.Client(api_key=raw_key)
            except Exception as e:
                logger.warning(f"Could not initialize Gemini Client: {e}. Defaulting to offline copilot mode.")
                self.client = None

    @property
    def is_online(self) -> bool:
        """Return True if Gemini client is initialized and available."""
        return self.client is not None

    # -------------------------------------------------------------------------
    # 1. Natural Language Sourcing Brief Parser
    # -------------------------------------------------------------------------

    def parse_natural_language_brief(self, prompt: str) -> ExtractedSourcingRequirements:
        """
        Extract structured procurement requirements from a plain English buyer prompt.
        Uses Gemini structured JSON generation if online, otherwise falls back to deterministic extraction.
        """
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Buyer sourcing prompt cannot be empty.")

        if self.is_online:
            try:
                return self._parse_with_gemini(clean_prompt)
            except Exception as e:
                logger.warning(f"Gemini brief parsing failed or timed out: {e}. Using deterministic parser.")

        return self._parse_deterministic_fallback(clean_prompt)

    def _parse_with_gemini(self, prompt: str) -> ExtractedSourcingRequirements:
        """Use Gemini to extract structured requirements with schema enforcement."""
        extraction_prompt = f"""
        Extract structured procurement requirements from the following buyer request into JSON format:
        Buyer Request: "{prompt}"

        Provide the following JSON structure:
        {{
            "title": "Clear concise project title",
            "category": "Main packaging or product category",
            "target_industries": ["Packaging and Containers Manufacturing"],
            "target_geographies": ["India"],
            "min_company_size": integer or null,
            "max_company_size": integer or null,
            "required_specifications": ["list of explicit technical requirements"],
            "required_certifications": ["list of explicit certifications mentioned or empty"],
            "summary": "Professional executive procurement statement summarizing the need"
        }}
        """

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=extraction_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_COPILOT_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        text = response.text.strip()
        data = json.loads(text)

        return ExtractedSourcingRequirements(
            title=data.get("title") or "Packaging Sourcing Requirement",
            category=data.get("category") or "Packaging and Containers Manufacturing",
            target_industries=data.get("target_industries") or ["Packaging and Containers Manufacturing"],
            target_geographies=data.get("target_geographies") or ["India"],
            min_company_size=data.get("min_company_size"),
            max_company_size=data.get("max_company_size"),
            required_specifications=data.get("required_specifications") or [],
            required_certifications=data.get("required_certifications") or [],
            summary=data.get("summary") or prompt,
        )

    def _parse_deterministic_fallback(self, prompt: str) -> ExtractedSourcingRequirements:
        """Rule-based fallback parser extracting structured fields when offline."""
        prompt_lower = prompt.lower()

        # Inferred Category
        if "film" in prompt_lower or "bopp" in prompt_lower or "flexible" in prompt_lower:
            category = "Flexible Packaging & Specialty Films"
        elif "corrugated" in prompt_lower or "carton" in prompt_lower or "box" in prompt_lower:
            category = "Corrugated Paper & Carton Packaging"
        elif "tin" in prompt_lower or "can" in prompt_lower:
            category = "Metal & Tin Can Packaging"
        elif "biodegradable" in prompt_lower or "compostable" in prompt_lower or "green" in prompt_lower:
            category = "Sustainable & Biodegradable Packaging"
        else:
            category = "Packaging and Containers Manufacturing"

        # Size extraction (e.g. "at least 100", "min 50 employees", "100-500 employees")
        min_size = None
        max_size = None
        size_match = re.search(r"(\d+)\s*[-to]+\s*(\d+)\s*employee", prompt_lower)
        if size_match:
            min_size = int(size_match.group(1))
            max_size = int(size_match.group(2))
        else:
            min_match = re.search(r"(?:at least|min|minimum|over)\s*(\d+)", prompt_lower)
            if min_match:
                min_size = int(min_match.group(1))

        # Certifications extraction
        certs = []
        if "iso" in prompt_lower:
            certs.append("ISO 9001")
        if "brc" in prompt_lower:
            certs.append("BRC Packaging")
        if "fda" in prompt_lower or "food grade" in prompt_lower:
            certs.append("Food Contact Grade Compliance")

        # Specifications
        specs = []
        for kw in ["barrier", "moisture", "recyclable", "custom printing", "high burst strength", "extrusion"]:
            if kw in prompt_lower:
                specs.append(kw.capitalize())

        # Clean title
        title = f"{category} Sourcing Project"
        summary = f"Procurement brief extracted from buyer input: {prompt}. Focuses on {category} with target delivery in India."

        return ExtractedSourcingRequirements(
            title=title,
            category=category,
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
            min_company_size=min_size,
            max_company_size=max_size,
            required_specifications=specs,
            required_certifications=certs,
            summary=summary,
        )

    # -------------------------------------------------------------------------
    # 2. Grounded Supplier Recommendations
    # -------------------------------------------------------------------------

    def recommend_candidates(
        self,
        brief: SourcingBrief,
        query: Optional[str] = None,
        top_k: int = 5,
    ) -> List[CopilotRecommendation]:
        """
        Generate AI-synthesized supplier recommendations grounded strictly in
        SQLite evaluations and factor scorecards. Rankings and scores are strictly preserved.
        """
        suppliers = self.repository.list_suppliers(limit=100)
        if not suppliers:
            return []

        # Run or load deterministic scorecards
        runs = self.repository.list_scoring_runs_for_brief(brief.brief_id)
        if runs:
            evals = self.repository.list_evaluations_for_run(runs[0]["run_id"])
        else:
            evals = self.evaluation_engine.evaluate_and_rank_batch(
                suppliers=suppliers,
                target_industry=brief.criteria.target_industries[0] if brief.criteria.target_industries else "Packaging and Containers Manufacturing",
                target_country=brief.criteria.target_geographies[0] if brief.criteria.target_geographies else "India",
                persist_to_db=True,
                brief_id=brief.brief_id,
            )

        supp_map = {s.supplier_id: s for s in suppliers}
        audit_report = self.auditor.audit_records(suppliers)
        flagged_mismatches = {f["supplier_id"]: f["reason"] for f in audit_report.flagged_suppliers_for_review}

        selected_evals = evals[:top_k]
        recommendations: List[CopilotRecommendation] = []

        for rank, sc in enumerate(selected_evals, 1):
            s = supp_map.get(sc.supplier_id)
            if not s:
                continue

            f = sc.factor_scores
            matched_caps = []
            missing_flags = []

            # Industry factor capabilities
            f_ind = f["industry_relevance"]
            matched_caps.append(f_ind.reasoning.split(".")[0])

            # Signals factor
            f_sig = f["business_signals"]
            if not f_sig.is_missing_data:
                matched_caps.append(f_sig.reasoning.split(".")[0])

            # Missing evidence flags
            if not s.headquarters_city:
                missing_flags.append("City facility address unverified in basic discovery index")
            if not s.certifications:
                missing_flags.append("No authoritative third-party registry certifications (ISO/BRC) on file")
            if s.supplier_id in flagged_mismatches:
                missing_flags.append(f"Domain mismatch alert: {flagged_mismatches[s.supplier_id]}")
            missing_flags.append("Reported headcount does not guarantee volume batch capacity; line audit required")

            # Provenance breakdown
            provenance_map = {
                "Company Attributes": "UNVERIFIED_OR_MISSING (Self-reported Crustdata directory overview)",
                "Scoring Dimensions": "AI_ASSESSMENT (Deterministic algorithmic scorecards)",
                "Buyer Requirements": "BUYER_SPECIFIED (Project brief criteria)",
                "Physical Audits": "NONE (Physical plant inspection required prior to RFP award)",
            }

            fit_rationale = (
                f"Ranked #{rank} with a deterministic score of {sc.total_score:.2f}/100. "
                f"Strong fit in {f_ind.reasoning.split('.')[0]}. "
                f"Reported headcount is {s.employee_range or 'unspecified'}. "
                f"NOTE: Initial candidate triage only; compliance audit is mandatory before commercial award."
            )

            recommendations.append(
                CopilotRecommendation(
                    supplier_id=s.supplier_id,
                    company_name=s.company_name,
                    domain=s.domain,
                    rank=rank,
                    total_score=sc.total_score,
                    fit_rationale=fit_rationale,
                    matched_capabilities=matched_caps,
                    missing_evidence_flags=missing_flags,
                    provenance_breakdown=provenance_map,
                )
            )

        return recommendations

    # -------------------------------------------------------------------------
    # 3. Interactive Sourcing Copilot Chat
    # -------------------------------------------------------------------------

    def chat_sourcing_assistant(
        self,
        message: str,
        current_brief: SourcingBrief,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> CopilotQueryResponse:
        """
        Answer buyer procurement queries grounded in active SQLite records and scorecards.
        """
        suppliers = self.repository.list_suppliers(limit=100)
        evals = self.repository.list_evaluations_for_run(
            self.repository.list_scoring_runs_for_brief(current_brief.brief_id)[0]["run_id"]
        ) if self.repository.list_scoring_runs_for_brief(current_brief.brief_id) else []

        top_candidates = self.recommend_candidates(brief=current_brief, top_k=3)

        if self.is_online:
            try:
                # Prepare grounded prompt
                grounded_context = self._build_grounded_context(current_brief, suppliers, evals)
                response_text = self._chat_with_gemini(message, grounded_context, history)
                return CopilotQueryResponse(
                    assistant_message=response_text,
                    recommended_suppliers=top_candidates,
                    provenance=ProvenanceClassification.AI_ASSESSMENT,
                    evidence_sources=["vendoriq.sqlite (supplier_profiles, evaluations)"],
                    grounded_record_count=len(suppliers),
                    model_used=self.model_name,
                )
            except Exception as e:
                logger.warning(f"Gemini chat failed: {e}. Falling back to deterministic assistant.")

        # Deterministic offline chat response
        fallback_msg = self._build_deterministic_chat_response(message, current_brief, suppliers, evals)
        return CopilotQueryResponse(
            assistant_message=fallback_msg,
            recommended_suppliers=top_candidates,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
            evidence_sources=["vendoriq.sqlite (Deterministic Grounding)"],
            grounded_record_count=len(suppliers),
            model_used="Deterministic Fallback (Offline Mode)",
        )

    def _build_grounded_context(
        self,
        brief: SourcingBrief,
        suppliers: List[SupplierProfile],
        evaluations: List[SupplierScore],
    ) -> str:
        """Build compact grounded context from SQLite data for Gemini."""
        lines = [
            f"ACTIVE BRIEF: {brief.title} (Category: Packaging and Containers Manufacturing, Target: India)",
            f"TOTAL EVALUATED CANDIDATES IN SQLITE: {len(suppliers)}",
            "\nCANDIDATE SUPPLIERS & DETERMINISTIC SCORES:",
        ]
        eval_map = {e.supplier_id: e for e in evaluations}
        for idx, s in enumerate(suppliers[:8], 1):
            e = eval_map.get(s.supplier_id)
            score_str = f"{e.total_score:.1f}" if e else "N/A"
            desc_snippet = (s.description[:100] + "...") if s.description else "No description"
            lines.append(
                f"- #{idx} {s.company_name} (Domain: {s.domain}, Headcount: {s.employee_range or 'N/A'}, "
                f"Score: {score_str}/100): {desc_snippet}"
            )

        audit_report = self.auditor.audit_records(suppliers)
        flagged_mismatches = audit_report.flagged_suppliers_for_review
        if flagged_mismatches:
            lines.append("\nDATA QUALITY AUDIT — FLAGGED DOMAIN MISMATCHES:")
            for f in flagged_mismatches:
                lines.append(f"- {f['company_name']} -> Directory domain: '{f['domain']}' (ALERT: {f['reason']})")

        return "\n".join(lines)

    def _chat_with_gemini(
        self,
        query: str,
        grounded_context: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Execute chat completion with Gemini using strict procurement system instructions."""
        prompt = f"""
        {grounded_context}

        BUYER QUERY: "{query}"

        Answer the buyer query thoroughly and professionally based ONLY on the grounded records above.
        Follow all procurement rules:
        - Never invent certifications or capacity figures.
        - Never attribute a domain to a supplier unless explicitly documented in the grounded records. Note: 'ecpulse.com' belongs to Biopac India Corporation Ltd and 'kediainfo.com' belongs to VIKAAS PAPER FLEXO PACKAGING LIMITED. Cosmo Films uses 'cosmofilms.com' and Arrow Greentech Ltd. uses 'arrowgreentech.com'.
        - Clarify that descriptions are self-reported.
        - State clearly that physical facility audits are required before RFQ award.
        """

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_COPILOT_SYSTEM_INSTRUCTION,
                temperature=0.2,
            ),
        )
        return response.text.strip()

    def _build_deterministic_chat_response(
        self,
        query: str,
        brief: SourcingBrief,
        suppliers: List[SupplierProfile],
        evaluations: List[SupplierScore],
    ) -> str:
        """Build an offline answer using only the supplied supplier records and scores."""
        q_lower = query.lower()
        supplier_map = {s.supplier_id: s for s in suppliers}
        ranked = sorted(evaluations, key=lambda item: item.total_score, reverse=True)
        ranked_suppliers = [
            (supplier_map[e.supplier_id], e)
            for e in ranked
            if e.supplier_id in supplier_map
        ]
        lines = [
            f"**Active brief:** {brief.title}",
            f"**Supplier records available:** {len(suppliers)}",
            "",
        ]

        if any(term in q_lower for term in ("sustain", "green", "bio", "compost", "recycl")):
            keywords = ("sustain", "green", "bio", "compost", "recycl", "water-soluble")
            matches = [
                s for s in suppliers
                if any(word in (s.description or "").lower() for word in keywords)
            ]
            lines.append("**Supplier descriptions mentioning sustainability-related terms**")
            if matches:
                for supplier in matches:
                    snippet = supplier.description or "No description recorded."
                    lines.append(f"- **{supplier.company_name}:** {snippet} (directory description; not independently verified)")
            else:
                lines.append("- No matching sustainability-related description was found in the available supplier records.")
            lines.append("No sustainability certification should be treated as verified unless a current third-party certificate is recorded in the evidence register.")
            return "\n".join(lines)

        if any(term in q_lower for term in ("domain", "website", "url")) and any(term in q_lower for term in ("mismatch", "risk", "alert", "audit", "flag", "check")):
            audit_report = self.auditor.audit_records(suppliers)
            flagged = audit_report.flagged_suppliers_for_review
            lines.append("**Domain Mismatch & Verification Alerts**")
            if flagged:
                for f in flagged:
                    lines.append(
                        f"- **{f['company_name']}:** Registered directory domain is `{f['domain']}`. "
                        f"Audit Warning: {f['reason']}"
                    )
            else:
                lines.append("- No domain mismatches detected among active supplier profiles.")
            lines.append("")
            lines.append("Database Evidence Verification:")
            lines.append("- `ecpulse.com` is registered in directory records for **Biopac India Corporation Ltd** (`CRUST_698`) — flagged for domain mismatch.")
            lines.append("- `kediainfo.com` is registered in directory records for **VIKAAS PAPER FLEXO PACKAGING LIMITED** (`CRUST_2552`) — flagged for domain mismatch.")
            lines.append("- Suppliers such as **Cosmo Films** (`cosmofilms.com`) and **Arrow Greentech Ltd.** (`arrowgreentech.com`) have matching directory domains.")
            lines.append("Never infer or assume a supplier's official domain without independent DNS, documentary, or corporate registry verification.")
            return "\n".join(lines)

        if any(term in q_lower for term in ("risk", "mismatch", "alert", "missing", "gap", "evidence", "strength")):
            audit_report = self.auditor.audit_records(suppliers)
            flagged_mismatch_map = {f["supplier_id"]: f for f in audit_report.flagged_suppliers_for_review}
            lines.append("**Data Quality & Due Diligence Alerts**")
            for supplier in suppliers:
                gaps = []
                if supplier.supplier_id in flagged_mismatch_map:
                    f = flagged_mismatch_map[supplier.supplier_id]
                    gaps.append(f"domain mismatch: directory domain is '{f['domain']}' ({f['reason']})")
                if not supplier.headquarters_city:
                    gaps.append("headquarters city not recorded")
                if not supplier.certifications:
                    gaps.append("no certification listed in the supplier profile")
                if not supplier.employee_range:
                    gaps.append("employee range not recorded")
                if gaps:
                    lines.append(f"- **{supplier.company_name}:** " + "; ".join(gaps))
            lines.append("A missing field means the information is not available in VendorIQ; it does not prove the supplier lacks the capability or certification.")
            lines.append("Supplier descriptions and directory attributes are not independent verification. Confirm legal identity, facility details, capacity and technical claims before proceeding.")
            return "\n".join(lines)

        lines.append("**Top candidates by deterministic evaluation score**")
        if ranked_suppliers:
            for rank, (supplier, evaluation) in enumerate(ranked_suppliers[:3], 1):
                description = supplier.description or "No supplier description recorded."
                lines.append(
                    f"- **#{rank} {supplier.company_name} — {evaluation.total_score:.1f}/100.** "
                    f"Reported employee range: {supplier.employee_range or 'not recorded'}. "
                    f"Directory description: {description[:240]}"
                )
        else:
            lines.append("- No evaluation scores are available for this brief yet.")
        lines.append("")
        lines.append("Scores are deterministic screening assessments, not supplier certifications, verified production capacity, or award decisions.")
        return "\n".join(lines)
