"""
VendorIQ UI Views Package.
"""

from ui.views.executive_view import render_executive_view
from ui.views.brief_view import render_brief_view
from ui.views.discovery_view import render_discovery_view
from ui.views.evaluation_view import render_evaluation_view
from ui.views.comparison_view import render_comparison_view
from ui.views.evidence_view import render_evidence_view
from ui.views.rfq_view import render_rfq_view
from ui.views.copilot_view import render_copilot_view
from ui.views.quotation_view import render_quotation_view

__all__ = [
    "render_executive_view",
    "render_brief_view",
    "render_discovery_view",
    "render_evaluation_view",
    "render_comparison_view",
    "render_evidence_view",
    "render_rfq_view",
    "render_copilot_view",
    "render_quotation_view",
]
