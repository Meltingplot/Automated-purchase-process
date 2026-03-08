# CLAUDE.md

## Project Overview

**ERPNext Procurement AI** (`erpnext_procurement_ai`) is a Frappe v15 custom app that automates the purchase process. Upload any procurement document (cart, order confirmation, delivery note, invoice) and the plugin extracts structured data via a multi-LLM consensus pipeline, then creates the complete ERPNext document chain retrospectively.

**Publisher:** Meltingplot GmbH | **License:** MIT | **Python:** >=3.11

## Running Tests

```bash
cd /home/tim/python/frappe_docker/development/apps/Automated-purchase-process
pytest tests/                    # all 8 test files, pure Python (no Frappe instance needed)
pytest tests/test_sanitizer.py   # single file
```

Tests are standalone unit tests using pytest + unittest.mock. No Frappe site required.

## Key Commands (inside dev container)

```bash
bench migrate                    # imports custom field fixtures
bench build                      # compiles JS (purchase_order/receipt/invoice_custom.js)
bench restart                    # restart workers after code changes
```

## Architecture

### Processing Pipeline

```
Upload (PDF/Image/Email)
  -> InputSanitizer (NFKC normalize, strip invisible chars, injection scan)
  -> OCR (pdfplumber + Tesseract/EasyOCR)
  -> Document Classification (LLM)
  -> Parallel LLM Extraction (Claude / OpenAI / Gemini / Local)
  -> OutputGuard (JSON extraction, Pydantic validation, plausibility checks)
  -> ConsensusEngine (field-by-field majority voting, OCR cross-check)
  -> Validation (confidence threshold, required fields)
  -> RetrospectiveChainBuilder (Supplier -> PO -> PR -> PI)
  -> Attach source file to created documents
```

### Module Layout

| Directory | Purpose |
|---|---|
| `procurement_ai/doctype/` | 4 DocTypes: AI Procurement Job, AI Extraction Result (child), AI Procurement Settings (single), AI Escalation Log |
| `procurement_ai/api/` | `ingest.py` (upload + pipeline), `status.py` (dashboard stats) |
| `procurement_ai/page/` | Procurement AI Dashboard (custom Frappe page) |
| `extraction/` | `pdf_parser.py`, `ocr_engine.py` (Tesseract/EasyOCR), `preprocessor.py`, `email_parser.py` |
| `llm/` | `graph.py` (LangGraph StateGraph), `nodes.py`, `models.py` (provider factory), `prompts.py`, `sanitizer.py`, `output_guard.py`, `consensus.py`, `schemas.py` (Pydantic), `local_trust.py`, `local_health.py` |
| `chain_builder/` | `retrospective.py` (orchestrator), `supplier.py`, `purchase_order.py`, `purchase_receipt.py`, `purchase_invoice.py`, `attachments.py` |
| `validation/` | `field_validator.py`, `amount_checker.py`, `supplier_matcher.py` |
| `utils/` | `security.py` (file upload validation), `logging.py` |
| `fixtures/` | `custom_field.json` (6 custom fields on PO/PR/PI) |
| `public/js/` | `purchase_order_custom.js`, `purchase_receipt_custom.js`, `purchase_invoice_custom.js` |

### DocTypes

- **AI Procurement Job** (`AIPROC-####`): Central job record. Tracks status (Pending/Processing/Needs Review/Completed/Error), stores OCR text, extraction results (child table), consensus data, confidence score, and links to created Supplier/PO/PR/PI.
- **AI Extraction Result**: Child table of Job. One row per LLM provider with extracted_data JSON, confidence, timing, token count, deviation fields.
- **AI Procurement Settings** (Single): All configuration -- API keys (Password fields), LLM provider settings, OCR engine choice, confidence threshold, auto-submit toggle, escalation email, local LLM config (provider/URL/model/trust level).
- **AI Escalation Log** (`ESC-####`): Created when consensus fails. Types: Low Confidence, Field Dispute, Amount Mismatch, Supplier Unclear, OCR Mismatch, Processing Error.

### Custom Fields (via fixtures)

6 hidden fields on Purchase Order, Purchase Receipt, Purchase Invoice:
- `ai_retrospective` (Check) -- marks document as AI-created
- `ai_procurement_job` (Link -> AI Procurement Job) -- traceability

All are `read_only=1`, `no_copy=1`, `hidden=1`, `print_hide=1`.

### Retrospective Chain Logic

| Source Document | Creates |
|---|---|
| Cart / Order Confirmation | Purchase Order |
| Delivery Note | Purchase Order + Purchase Receipt |
| Invoice | Purchase Order + Purchase Receipt + Purchase Invoice |

Source file is attached to all created documents via Frappe's File attachment system.

### LLM Provider Support

Cloud providers via LangChain: Claude (`langchain-anthropic`), OpenAI (`langchain-openai`), Gemini (`langchain-google-genai`).

Local LLMs via `ChatOpenAI` with custom `base_url`: Ollama, vLLM, llama.cpp, LM Studio. All implement OpenAI-compatible API.

Trust levels for local LLMs: `full` (1.0 weight, 70B+), `reduced` (0.5, 13B-70B), `validation_only` (0.0, <13B).

Minimum 2 active providers required for consensus (unless `development_mode` is enabled).

### Security Layers

1. **InputSanitizer** -- NFKC normalization, invisible char removal, injection pattern detection (13 patterns)
2. **Prompt isolation** -- document content in `--- BEGIN/END DOCUMENT DATA ---` block, never in instructions
3. **OutputGuard** -- JSON extraction, Pydantic schema validation, arithmetic plausibility checks
4. **ConsensusEngine** -- multi-LLM voting makes single-provider manipulation ineffective
5. **Local LLM Trust** -- configurable weight reduction for smaller/more vulnerable models

### API Endpoints

- `POST /api/method/erpnext_procurement_ai.procurement_ai.api.ingest.process` -- upload file, create job, enqueue processing (requires `create` permission on AI Procurement Job)
- `GET /api/method/erpnext_procurement_ai.procurement_ai.api.status.get_job_status` -- job status + created doc links
- `GET /api/method/erpnext_procurement_ai.procurement_ai.api.status.get_dashboard_stats` -- status counts, recent jobs, open escalations (requires `read` permission on AI Procurement Job)

### Scheduler

`process_pending_jobs()` runs on every scheduler tick (`all`), picks up to 10 pending jobs and enqueues them via `frappe.enqueue()` on the `long` queue.

### hooks.py

- `scheduler_events.all` -- process pending jobs
- `doctype_js` -- injects "AI Retrospective" badge into PO, PR, PI forms
- `fixtures` -- Custom Field filtered by `module = "Procurement AI"`

## Dependencies

LangGraph, LangChain (anthropic/openai/google-genai), pdfplumber, pytesseract, easyocr, Pillow, pydantic v2, python-magic, requests.

## Conventions

- Python: formatted with black, imports sorted with isort (black profile), minimum Python 3.11+
- All LLM-facing text passes through `InputSanitizer.sanitize()` before reaching any prompt
- All LLM outputs pass through `OutputGuard.validate_extraction()` before being trusted
- Chain builder functions accept `settings: dict` (from `get_settings_dict()`) and use `settings.get("default_company")` for company-scoped queries (warehouse, expense account)
- Custom fields on standard DocTypes are managed via `fixtures/custom_field.json`, never created programmatically
- File attachments use Frappe's built-in `File` DocType with `attached_to_doctype`/`attached_to_name`
