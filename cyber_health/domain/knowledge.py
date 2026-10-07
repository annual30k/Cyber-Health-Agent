"""Evidence-based knowledge retrieval."""

from __future__ import annotations

import uuid
from typing import Any

from ..errors import ValidationError
from ..models import QueryKnowledgeInput
from .base import ServiceCore


class KnowledgeMixin(ServiceCore):
    """Evidence-based knowledge retrieval."""

    def query_knowledge(
        self,
        *,
        query: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Verified primary evidence lookup with explicit source citation and category filtering."""
        try:
            QueryKnowledgeInput(query=query, category=category)
        except Exception as err:
            raise ValidationError(str(err)) from err

        q_lower = query.lower()
        verified_items = [
            {
                "topic": "protein_intake_for_exercising_individuals",
                "title": "ISSN Position Stand: Protein and Exercise (International Society of Sports Nutrition)",
                "source_type": "peer_reviewed_literature",
                "evidence_statement": "An overall daily protein intake in the range of 1.4-2.0 g protein/kg body weight/day for most exercising individuals is sufficient for building and maintaining muscle mass.",
                "doi_or_citation": "J Int Soc Sports Nutr. 2017;14:20. doi:10.1186/s12970-017-0177-8",
                "url": "https://doi.org/10.1186/s12970-017-0177-8",
                "category": "nutrition",
            },
            {
                "topic": "acute_cardiovascular_red_flags",
                "title": "AHA/ACC Scientific Statement: Exercise Standards for Testing and Training",
                "source_type": "clinical_guideline",
                "evidence_statement": "Exertional chest pain, unexplained syncope or pre-syncope, and disproportionate dyspnea warrant immediate exercise cessation and urgent clinical evaluation.",
                "doi_or_citation": "Circulation. 2013;128(8):873-934. doi:10.1161/CIR.0b013e31829b5b44",
                "url": "https://doi.org/10.1161/CIR.0b013e31829b5b44",
                "category": "safety",
            },
        ]

        matched = []
        for it in verified_items:
            if category and it["category"] != category:
                continue
            search_corpus = f"{it['topic']} {it['title'].lower()} {it['evidence_statement'].lower()}"
            if any(term in search_corpus for term in q_lower.split() if len(term) > 2):
                matched.append(it)

        operation_id = f"op_read_{uuid.uuid4().hex[:12]}"
        status = "success" if matched else "unavailable"
        data = {
            "query": query,
            "category": category,
            "status": status,
            "clinical_review_status": "evidence_rules_algorithmic_pending_licensed_physician_review",
            "external_dependency": "requires_credentialed_sports_dietitian_or_physician_for_individual_prescription",
            "evidence_items": matched,
            "note": "Verified primary literature only; unverified citations are withheld." if matched else "No verified primary literature matches query and category.",
        }
        return {
            "operation_id": operation_id,
            "status": status,
            "data": data,
            "warnings": ["NON_DIAGNOSTIC: Algorithmic health guidelines are informational only and do not constitute medical diagnosis."],
            "error": None,
            "state_version": 0,
            **data,
        }
