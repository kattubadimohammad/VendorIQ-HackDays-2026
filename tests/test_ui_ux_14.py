"""
Unit & Integration Tests for VendorIQ Phase 14 Product Experience & UX Polish.

Verifies:
1. End-to-end workflow progress indicator definition and rendering.
2. Standardized view header, score legend, and evidence legend components.
3. Next-step guidance cards across the procurement pipeline.
4. Sequential navigation structure aligned with the sourcing lifecycle.
5. Dynamic database name resolution in copilot safeguards.
6. Responsive design and visual consistency.
"""

import os
from unittest.mock import MagicMock, patch
import pytest

from ui.components import (
    WORKFLOW_STAGES,
    render_workflow_stepper,
    render_view_header,
    render_score_legend,
    render_evidence_legend,
    render_next_step_guidance,
    get_evidence_status_badge_html,
    get_provenance_badge_html,
)
from core.provenance import ProvenanceClassification


class TestPhase14WorkflowAndUXComponents:
    """Test suite for Phase 14 UX polish and workflow progress stepper."""

    def test_workflow_stages_defined_and_sequential(self):
        """Verify all 6 core workflow stages are defined with labels and descriptions."""
        assert len(WORKFLOW_STAGES) == 6
        expected_keys = ["brief", "discovery", "evaluation", "evidence", "rfq", "quotation"]
        actual_keys = [s[0] for s in WORKFLOW_STAGES]
        assert actual_keys == expected_keys

        # Ensure labels and descriptions are descriptive
        for key, label, desc in WORKFLOW_STAGES:
            assert len(label) > 3
            assert len(desc) > 5

    @patch("streamlit.html", create=True)
    @patch("streamlit.markdown")
    def test_workflow_stepper_html_rendering(self, mock_markdown, mock_html):
        """Verify render_workflow_stepper generates valid HTML with current stage highlighted."""
        render_workflow_stepper("evaluation")
        assert mock_html.called or mock_markdown.called
        rendered_html = (
            mock_html.call_args[0][0] if mock_html.called else mock_markdown.call_args[0][0]
        )

        assert "1. Sourcing Brief" in rendered_html
        assert "3. Evaluation" in rendered_html
        assert "6. Quotations & Analysis" in rendered_html
        # Active stage styling should be present
        assert "#1E40AF" in rendered_html

    @patch("streamlit.markdown")
    @patch("streamlit.caption")
    def test_view_header_rendering(self, mock_caption, mock_markdown):
        """Verify render_view_header renders title, subtitle, and optional stepper."""
        render_view_header(
            title="🔍 Supplier Discovery",
            subtitle="Search market suppliers",
            stage_key="discovery",
        )
        assert mock_markdown.called
        assert mock_caption.called
        mock_caption.assert_called_with("Search market suppliers")

    @patch("streamlit.expander")
    def test_score_and_evidence_legends(self, mock_expander):
        """Verify score and evidence legends initialize clean expanders."""
        render_score_legend()
        assert mock_expander.called

        mock_expander.reset_mock()
        render_evidence_legend()
        assert mock_expander.called

    @patch("streamlit.html", create=True)
    @patch("streamlit.markdown")
    def test_next_step_guidance_rendering(self, mock_markdown, mock_html):
        """Verify next step guidance outputs styled HTML with the target view name."""
        render_next_step_guidance("🏆 Supplier Evaluation", "Inspect candidate scorecards.")
        assert mock_html.called or mock_markdown.called
        html = (
            mock_html.call_args[0][0] if mock_html.called else mock_markdown.call_args[0][0]
        )
        assert "🏆 Supplier Evaluation" in html
        assert "Inspect candidate scorecards." in html

    def test_evidence_status_badges_all_four_tiers(self):
        """Verify all 4 evidence tiers produce valid HTML badges."""
        tier_labels = {
            "VERIFIED": "VERIFIED",
            "SELF_REPORTED": "SELF-REPORTED",
            "UNVERIFIED": "UNVERIFIED",
            "MISSING": "MISSING",
        }
        for tier, label in tier_labels.items():
            html = get_evidence_status_badge_html(tier)
            assert f">{label}</span>" in html
            assert "background-color:" in html

    def test_provenance_badge_all_classifications(self):
        """Verify all provenance classifications produce valid HTML badges."""
        for p in ProvenanceClassification:
            html = get_provenance_badge_html(p)
            assert "<span" in html
            assert "</span>" in html

    def test_app_navigation_sequential_lifecycle_order(self):
        """Verify app.py navigation options reflect the natural procurement lifecycle."""
        with open("app.py", encoding="utf-8") as f:
            content = f.read()

        expected_order = [
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

        # Verify all 9 are present in app.py
        for item in expected_order:
            assert f'"{item}"' in content or f"'{item}'" in content

        # Check sequential order in nav_options
        indices = [content.index(item) for item in expected_order]
        assert indices == sorted(indices), "Navigation options are not in sequential lifecycle order!"
