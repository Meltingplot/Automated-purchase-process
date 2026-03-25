"""Tests for LLM pipeline graph construction in graph.py and node functions in nodes.py."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock all external dependencies before importing graph module
langgraph_mock = MagicMock()
sys.modules.setdefault("langgraph", langgraph_mock)
sys.modules.setdefault("langgraph.graph", langgraph_mock.graph)
langgraph_mock.graph.END = "END"

# Mock pydantic
pydantic_mock = MagicMock()
sys.modules.setdefault("pydantic", pydantic_mock)

for mod in (
    "langchain_anthropic",
    "langchain_openai",
    "langchain_google_genai",
    "langchain_core",
    "langchain_core.messages",
    "pytesseract",
    "easyocr",
    "pdfplumber",
    "PIL",
    "PIL.Image",
    "PIL.ImageEnhance",
    "PIL.ImageFilter",
    "magic",
):
    sys.modules.setdefault(mod, MagicMock())

# Provide a fake frappe module
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-25"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: round(x, 2)
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.llm.graph import build_extraction_graph
from procurement_ai.llm.models import LLMProviderFactory


# ============================================================
# build_extraction_graph
# ============================================================


class TestBuildExtractionGraph:
    def setup_method(self):
        langgraph_mock.reset_mock()
        # Make StateGraph().compile() return a mock
        self.mock_workflow = MagicMock()
        langgraph_mock.graph.StateGraph.return_value = self.mock_workflow

    def test_raises_with_no_providers(self):
        """0 active providers → ValueError."""
        settings = {
            "claude_api_key": None,
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
        }
        with pytest.raises(ValueError, match="At least 1 LLM provider"):
            build_extraction_graph(settings)

    def test_builds_with_single_provider(self):
        """Single provider → graph compiles successfully."""
        settings = {
            "claude_api_key": "test-key",
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
        }
        result = build_extraction_graph(settings)
        # Should have called compile
        self.mock_workflow.compile.assert_called_once()
        # Should have added the LLM node for claude
        node_names = [
            call[0][0] for call in self.mock_workflow.add_node.call_args_list
        ]
        assert "llm_claude" in node_names

    def test_builds_with_multiple_providers(self):
        """Multiple providers → one node per provider."""
        settings = {
            "claude_api_key": "test-key",
            "openai_api_key": "test-key",
            "gemini_api_key": "test-key",
            "enable_local_llm": False,
        }
        build_extraction_graph(settings)
        node_names = [
            call[0][0] for call in self.mock_workflow.add_node.call_args_list
        ]
        assert "llm_claude" in node_names
        assert "llm_openai" in node_names
        assert "llm_gemini" in node_names

    def test_fixed_nodes_always_present(self):
        """Sanitize, OCR, classify, consensus, validate, escalate are always added."""
        settings = {
            "claude_api_key": "test-key",
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
        }
        build_extraction_graph(settings)
        node_names = [
            call[0][0] for call in self.mock_workflow.add_node.call_args_list
        ]
        for expected in ("sanitize_input", "conventional_ocr", "classify_document",
                         "build_consensus", "validate_results", "escalate"):
            assert expected in node_names

    def test_entry_point_is_sanitize_input(self):
        settings = {
            "claude_api_key": "test-key",
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
        }
        build_extraction_graph(settings)
        self.mock_workflow.set_entry_point.assert_called_once_with("sanitize_input")

    def test_local_llm_provider(self):
        """Local LLM when enabled adds llm_local node."""
        settings = {
            "claude_api_key": None,
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": True,
            "local_llm_provider": "Ollama",
            "local_llm_base_url": "http://localhost:11434",
            "local_llm_model_name": "llama3.1:8b",
            "local_llm_api_key": None,
        }
        build_extraction_graph(settings)
        node_names = [
            call[0][0] for call in self.mock_workflow.add_node.call_args_list
        ]
        assert "llm_local" in node_names


# ============================================================
# LLMProviderFactory.get_active_providers (tested here for graph context)
# ============================================================


class TestGetActiveProviders:
    def test_all_providers(self):
        settings = {
            "claude_api_key": "key",
            "openai_api_key": "key",
            "gemini_api_key": "key",
            "enable_local_llm": True,
            "local_llm_provider": "Ollama",
            "local_llm_base_url": "http://localhost:11434",
            "local_llm_model_name": "model",
        }
        result = LLMProviderFactory.get_active_providers(settings)
        assert "claude" in result
        assert "openai" in result
        assert "gemini" in result
        assert "local" in result

    def test_no_providers(self):
        settings = {
            "claude_api_key": None,
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
        }
        result = LLMProviderFactory.get_active_providers(settings)
        assert result == []

    def test_partial_providers(self):
        settings = {
            "claude_api_key": "key",
            "openai_api_key": None,
            "gemini_api_key": "key",
            "enable_local_llm": False,
        }
        result = LLMProviderFactory.get_active_providers(settings)
        assert result == ["claude", "gemini"]
