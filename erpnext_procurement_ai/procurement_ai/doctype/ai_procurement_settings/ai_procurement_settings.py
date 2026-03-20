import frappe
from frappe.model.document import Document


class AIProcurementSettings(Document):
    def validate(self):
        self._validate_confidence_threshold()
        self._validate_provider_count()

    def _validate_confidence_threshold(self):
        if self.confidence_threshold is not None:
            if not (0.0 <= self.confidence_threshold <= 1.0):
                frappe.throw("Confidence threshold must be between 0.0 and 1.0")

    def _validate_provider_count(self):
        """Ensure at least 2 providers are configured unless in dev mode."""
        if self.development_mode:
            return

        active_count = 0
        if self.claude_api_key:
            active_count += 1
        if self.openai_api_key:
            active_count += 1
        if self.gemini_api_key:
            active_count += 1
        if self.enable_local_llm and self.local_llm_base_url:
            active_count += 1

        if self.enable_auto_processing and active_count < 2:
            frappe.throw(
                "At least 2 LLM providers must be configured for consensus. "
                f"Currently active: {active_count}. "
                "Enable Development Mode for single-provider operation."
            )

    def get_settings_dict(self):
        """Return settings as a plain dict for use in pipeline."""
        return {
            "enable_auto_processing": self.enable_auto_processing,
            "development_mode": self.development_mode,
            "default_company": self.default_company,
            "ocr_engine": self.ocr_engine,
            "confidence_threshold": self.confidence_threshold,
            "min_llm_consensus": self.min_llm_consensus,
            "max_parallel_llms": self.max_parallel_llms,
            "auto_submit_documents": self.auto_submit_documents,
            "require_document_review": self.require_document_review,
            "amount_tolerance": float(self.amount_tolerance or 0.05),
            "escalation_email": self.escalation_email,
            "claude_api_key": self.get_password("claude_api_key", raise_exception=False),
            "openai_api_key": self.get_password("openai_api_key", raise_exception=False),
            "gemini_api_key": self.get_password("gemini_api_key", raise_exception=False),
            "enable_local_llm": self.enable_local_llm,
            "local_llm_provider": self.local_llm_provider,
            "local_llm_base_url": self.local_llm_base_url,
            "local_llm_model_name": self.local_llm_model_name,
            "local_llm_api_key": self.get_password("local_llm_api_key", raise_exception=False),
            "local_llm_context_length": self.local_llm_context_length,
            "local_llm_gpu_layers": self.local_llm_gpu_layers,
            "local_llm_timeout": self.local_llm_timeout,
            "local_llm_trust_level": self.local_llm_trust_level,
        }
