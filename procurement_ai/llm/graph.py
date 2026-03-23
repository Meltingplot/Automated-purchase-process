"""
LangGraph StateGraph definition for the extraction pipeline.

Dynamically builds the graph based on active LLM providers.
Supports fan-out to multiple LLMs and fan-in for consensus.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from .models import LLMProviderFactory
from .nodes import (
    build_consensus_node,
    classify_document_node,
    escalation_node,
    llm_extraction_node_factory,
    ocr_extraction_node,
    sanitize_input_node,
    validation_node,
)


class ExtractionState(TypedDict):
    """State that flows through the extraction pipeline."""

    # Input
    raw_text: str
    document_images: list[bytes]
    source_type_hint: str | None
    source_file_url: str
    job_name: str

    # Pipeline stages
    ocr_result: dict | None
    llm_results: Annotated[list, operator.add]  # Fan-in: collects from all LLMs
    consensus: dict | None
    confidence: float
    needs_escalation: bool
    escalation_reasons: list[str]
    validated_data: dict | None

    # Settings (passed through state)
    settings: dict


def build_extraction_graph(settings: dict) -> Any:
    """
    Build the LangGraph extraction pipeline dynamically.

    Only includes nodes for active (configured) LLM providers.
    Requires at least 1 provider. Single-provider operation forces
    human review (no auto-acceptance).

    Args:
        settings: dict from AIProcurementSettings.get_settings_dict()

    Returns:
        Compiled LangGraph application

    Raises:
        ValueError: If no providers are active
    """
    workflow = StateGraph(ExtractionState)

    # Fixed nodes (always present)
    workflow.add_node("sanitize_input", sanitize_input_node)
    workflow.add_node("conventional_ocr", ocr_extraction_node)
    workflow.add_node("classify_document", classify_document_node)
    workflow.add_node("build_consensus", build_consensus_node)
    workflow.add_node("validate_results", validation_node)
    workflow.add_node("escalate", escalation_node)

    # Dynamic LLM nodes based on active providers
    active_providers = LLMProviderFactory.get_active_providers(settings)

    if len(active_providers) < 1:
        raise ValueError("At least 1 LLM provider must be configured.")

    for provider in active_providers:
        node_name = f"llm_{provider}"
        workflow.add_node(node_name, llm_extraction_node_factory(provider))
        workflow.add_edge("classify_document", node_name)
        workflow.add_edge(node_name, "build_consensus")

    # Fixed edges
    workflow.set_entry_point("sanitize_input")
    workflow.add_edge("sanitize_input", "conventional_ocr")
    workflow.add_edge("conventional_ocr", "classify_document")
    workflow.add_edge("build_consensus", "validate_results")

    # Conditional: escalate or finish
    workflow.add_conditional_edges(
        "validate_results",
        lambda state: "escalate" if state["needs_escalation"] else END,
    )
    workflow.add_edge("escalate", END)

    return workflow.compile()
