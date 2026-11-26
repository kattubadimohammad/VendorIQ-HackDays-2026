"""
VendorIQ - Autonomous Procurement & Sourcing Intelligence Platform.

Main Streamlit Application Entrypoint.
Interactive Multi-Page Sourcing Dashboard (Phase 7).
"""

import streamlit as st
import os
import sys
import importlib

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db.connection
import db.repository
import ui.data_service
import ui.views.copilot_view
import ui.views.evidence_view
import ui.views.discovery_view
import ui.views.quotation_view
import services.evidence_service
import services.discovery_service

from config import settings
from ui.data_service import DashboardDataService
from ui.components import render_header, render_trust_disclaimer
from ui.views import (
    render_executive_view,
    render_brief_view,
    render_discovery_view,
    render_evaluation_view,
    render_comparison_view,
    render_evidence_view,
    render_rfq_view,
    render_copilot_view,
    render_quotation_view,
)


def get_service() -> DashboardDataService:
    """Initialize data service instance with refreshed modules."""
    importlib.reload(db.connection)
    importlib.reload(db.repository)
    importlib.reload(services.evidence_service)
    importlib.reload(services.discovery_service)
    importlib.reload(ui.components)
    importlib.reload(ui.data_service)
    importlib.reload(ui.views.brief_view)
    importlib.reload(ui.views.copilot_view)
    importlib.reload(ui.views.evidence_view)
    importlib.reload(ui.views.discovery_view)
    importlib.reload(ui.views.evaluation_view)
    importlib.reload(ui.views.comparison_view)
    importlib.reload(ui.views.executive_view)
    importlib.reload(ui.views.rfq_view)
    importlib.reload(ui.views.quotation_view)
    from ui.data_service import DashboardDataService as FreshService
    return FreshService()


def main():
    # 1. Page Configuration
    st.set_page_config(
        page_title="VendorIQ - Sourcing Intelligence Dashboard",
        page_icon="📦",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    service = get_service()

    # 2. Ensure default packaging brief exists
    briefs = service.get_all_briefs()
    if not briefs:
        from core.models import SourcingBrief, SourcingCriteria, ScoringWeights
        default_brief = SourcingBrief(
            brief_id="BRIEF-PACKAGING-IN-01",
            title="Sustainable and Industrial Packaging Suppliers in India",
            buyer_organization="VendorIQ Sourcing Project",
            description="Identification and qualification of packaging and container manufacturers in India for retail, industrial, and flexible barrier packaging requirements.",
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
        service.create_brief(default_brief)
        briefs = [default_brief]

    # Map briefs for selector
    brief_options = {f"{b.title} ({b.brief_id})": b for b in briefs}

    # 3. Sidebar Navigation & Global Controls
    with st.sidebar:
        st.markdown("## 📦 **VendorIQ**")
        st.caption("Autonomous Procurement Intelligence v1.0.0")
        st.markdown("---")

        st.markdown("### 🗂️ Active Sourcing Brief")
        selected_brief_key = st.selectbox(
            "Select Project Brief",
            options=list(brief_options.keys()),
            index=0,
        )
        current_brief = brief_options[selected_brief_key]

        st.markdown("---")
        st.markdown("### 🧭 Dashboard Navigation")
        nav_options = [
            "📊 Executive Overview",
            "📋 Sourcing Brief",
            "🔍 Supplier Discovery",
            "🏆 Supplier Evaluation",
            "🛡️ Evidence & Trust",
            "⚖️ Supplier Comparison",
            "🤖 AI Sourcing Copilot",
            "📝 RFQ Preparation",
            "💰 Quotations & Analysis",
        ]
        selected_nav = st.radio("Go to View:", nav_options, index=0)

        st.markdown("---")
        st.markdown("### 🔒 System & Safety Status")
        app_mode_display = settings.APP_MODE.value if hasattr(settings.APP_MODE, "value") else str(settings.APP_MODE)
        st.write(f"- **Runtime Mode**: `Local SQLite Read Mode`")
        st.write(f"- **Configured APP_MODE**: `{app_mode_display}`")
        st.write(f"- **Active Network Calls**: `0 (Local Database Only)`")
        st.write(f"- **Historical Search**: `0.6 API Credits ($0.00 USD)`")
        st.write(f"- **Dashboard Session Credits**: `0 Credits`")
        active_db_path = getattr(service.repository, "db_path", None) or settings.effective_database_path
        active_db_name = os.path.basename(str(active_db_path)) if active_db_path and active_db_path != ":memory:" else ":memory:"
        is_prod = active_db_name.lower() == "vendoriq.sqlite"
        db_badge = "🛡️ (Production)" if is_prod else "🧪 (Isolated)"
        st.write(f"- **Database**: `{active_db_name}` {db_badge}")
        st.write(f"- **Candidate Status**: `EVALUATED_CANDIDATE`")

        st.caption("Operating strictly from local SQLite. Zero active API connections or external credit usage.")

    # 4. Load Data for Selected Brief
    suppliers = service.get_all_suppliers(limit=100)
    audit_report = service.audit_suppliers(suppliers)
    evaluations = service.get_evaluations(current_brief.brief_id)

    # 5. Top Header Banner
    render_header(subtitle=f"Active Brief: {current_brief.title}")

    # 6. Render Selected View
    if selected_nav == "📊 Executive Overview":
        ui.views.executive_view.render_executive_view(
            brief=current_brief,
            suppliers=suppliers,
            evaluations=evaluations,
            audit_report=audit_report,
        )
    elif selected_nav == "🤖 AI Sourcing Copilot":
        ui.views.copilot_view.render_copilot_view(
            service=service,
            current_brief=current_brief,
            suppliers=suppliers,
            evaluations=evaluations,
        )
    elif selected_nav == "📋 Sourcing Brief":
        ui.views.brief_view.render_brief_view(service=service, current_brief=current_brief)
    elif selected_nav == "🔍 Supplier Discovery":
        ui.views.discovery_view.render_discovery_view(
            suppliers=suppliers,
            brief=current_brief,
            evaluations=evaluations,
            service=service,
        )
    elif selected_nav == "🏆 Supplier Evaluation":
        ui.views.evaluation_view.render_evaluation_view(evaluations=evaluations)
    elif selected_nav == "⚖️ Supplier Comparison":
        ui.views.comparison_view.render_comparison_view(
            suppliers=suppliers,
            evaluations=evaluations,
            audit_report=audit_report,
        )
    elif selected_nav == "🛡️ Evidence & Trust":
        ui.views.evidence_view.render_evidence_view(suppliers=suppliers, evaluations=evaluations, service=service)
    elif selected_nav == "📝 RFQ Preparation":
        ui.views.rfq_view.render_rfq_view(
            service=service,
            brief=current_brief,
            suppliers=suppliers,
            evaluations=evaluations,
        )
    elif selected_nav == "💰 Quotations & Analysis":
        ui.views.quotation_view.render_quotation_view(
            service=service,
            brief=current_brief,
            suppliers=suppliers,
        )

    # 7. Ethical Trust Disclaimer Footer
    st.markdown("---")
    render_trust_disclaimer()


if __name__ == "__main__":
    main()
