"""
Smoke tests: verify every Python module in the app can be imported.

Run via: bench execute procurement_ai.tests.test_module_imports.run_all
"""

from __future__ import annotations

import importlib
import sys

MODULES = [
    # Core app
    "procurement_ai",
    "procurement_ai.hooks",
    # LLM pipeline
    "procurement_ai.llm.sanitizer",
    "procurement_ai.llm.output_guard",
    "procurement_ai.llm.consensus",
    "procurement_ai.llm.schemas",
    "procurement_ai.llm.prompts",
    "procurement_ai.llm.models",
    "procurement_ai.llm.local_trust",
    "procurement_ai.llm.local_health",
    "procurement_ai.llm.graph",
    "procurement_ai.llm.nodes",
    # Extraction
    "procurement_ai.extraction.pdf_parser",
    "procurement_ai.extraction.ocr_engine",
    "procurement_ai.extraction.preprocessor",
    "procurement_ai.extraction.email_parser",
    # Chain builder
    "procurement_ai.chain_builder.retrospective",
    "procurement_ai.chain_builder.supplier",
    "procurement_ai.chain_builder.purchase_order",
    "procurement_ai.chain_builder.purchase_receipt",
    "procurement_ai.chain_builder.purchase_invoice",
    "procurement_ai.chain_builder.document_matcher",
    "procurement_ai.chain_builder.attachments",
    # Validation
    "procurement_ai.validation.field_validator",
    "procurement_ai.validation.amount_checker",
    "procurement_ai.validation.supplier_matcher",
    # Utils
    "procurement_ai.utils.security",
    "procurement_ai.utils.logging",
    # Config
    "procurement_ai.config.desktop",
    # Patches
    "procurement_ai.patches.remove_ai_retrospective_field",
    # DocTypes
    "procurement_ai.procurement_ai.doctype.ai_procurement_job.ai_procurement_job",
    "procurement_ai.procurement_ai.doctype.ai_procurement_settings.ai_procurement_settings",
    "procurement_ai.procurement_ai.doctype.ai_extraction_result.ai_extraction_result",
    "procurement_ai.procurement_ai.doctype.ai_escalation_log.ai_escalation_log",
    # API
    "procurement_ai.procurement_ai.api.ingest",
    "procurement_ai.procurement_ai.api.status",
]


def run_all():
    """Import every module and report failures."""
    failures = []
    for mod_name in MODULES:
        try:
            # Clear from cache to force a fresh import
            if mod_name in sys.modules:
                del sys.modules[mod_name]
            importlib.import_module(mod_name)
            print(f"  OK  {mod_name}")
        except Exception as exc:
            failures.append((mod_name, str(exc)))
            print(f"  FAIL  {mod_name}: {exc}")

    print()
    print(f"Imported {len(MODULES) - len(failures)}/{len(MODULES)} modules successfully.")

    if failures:
        print("\nFailed imports:")
        for mod_name, err in failures:
            print(f"  - {mod_name}: {err}")
        raise SystemExit(1)

    print("All module imports passed.")
