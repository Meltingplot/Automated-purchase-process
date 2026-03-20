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
  -> [if require_document_review] Human Review UI (edit fields, map items)
  -> RetrospectiveChainBuilder (Supplier -> PO -> PR -> PI)
  -> Attach source file to created documents
```

### Document Review Workflow

When `require_document_review` is enabled in Settings (default: on), the pipeline pauses after LLM extraction and consensus, entering the **Awaiting Review** status. The user sees:

- **Header fields** — editable inputs for all extracted fields (supplier, dates, amounts, etc.) with per-field confidence badges (N/M agreement across LLM providers)
- **Items table** — editable rows for each line item with a "Map to Item" Link control to manually select an existing ERPNext Item (bypasses `_resolve_item` fuzzy matching)
- **Approve & Create Documents** button — saves `reviewed_data` + `item_mapping` JSON, triggers `run_chain_from_review()` background job

The `item_mapping` parameter flows through `RetrospectiveChainBuilder.build_chain()` into all three chain builders (`_build_items`, `_build_receipt_items`, `_build_invoice_items`). When an item index has a mapped item_code, `_resolve_item()` is skipped entirely for that item.

When `require_document_review` is disabled, the pipeline auto-creates documents as before (no pause).

### Module Layout

| Directory | Purpose |
|---|---|
| `procurement_ai/doctype/` | 4 DocTypes: AI Procurement Job, AI Extraction Result (child), AI Procurement Settings (single), AI Escalation Log |
| `procurement_ai/api/` | `ingest.py` (upload + pipeline), `status.py` (dashboard stats) |
| `procurement_ai/page/` | Procurement AI Dashboard (custom Frappe page) |
| `extraction/` | `pdf_parser.py`, `ocr_engine.py` (Tesseract/EasyOCR), `preprocessor.py`, `email_parser.py` |
| `llm/` | `graph.py` (LangGraph StateGraph), `nodes.py`, `models.py` (provider factory), `prompts.py`, `sanitizer.py`, `output_guard.py`, `consensus.py`, `schemas.py` (Pydantic), `local_trust.py`, `local_health.py` |
| `chain_builder/` | `retrospective.py` (orchestrator), `document_matcher.py` (find-before-create), `supplier.py`, `purchase_order.py`, `purchase_receipt.py`, `purchase_invoice.py`, `attachments.py` |
| `validation/` | `field_validator.py`, `amount_checker.py`, `supplier_matcher.py` |
| `utils/` | `security.py` (file upload validation), `logging.py` |
| `fixtures/` | `custom_field.json` (6 custom fields on PO/PR/PI) |
| `public/js/` | `purchase_order_custom.js`, `purchase_receipt_custom.js`, `purchase_invoice_custom.js` |

### DocTypes

- **AI Procurement Job** (`AIPROC-####`): Central job record. Tracks status (Pending/Processing/Awaiting Review/Needs Review/Completed/Error), stores OCR text, extraction results (child table), consensus data, confidence score, reviewed_data (user-corrected JSON), item_mapping (user-selected Item codes), and links to created Supplier/PO/PR/PI. Deletion clears `ai_procurement_job` back-references on linked PO/PR/PI via `on_trash`.
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

Source file is attached to all **newly created** documents via Frappe's File attachment system. Matched (existing) documents do not get a duplicate attachment.

### Document Matching (`document_matcher.py`)

The chain builder uses "find before create" logic. For each document type, it first tries to match an existing document before creating a new one. All queries exclude cancelled documents (`docstatus != 2`).

**Purchase Order matching** (`find_matching_purchase_order`):

| Priority | Method | Confidence | Description |
|---|---|---|---|
| 1 | `po_name_exact` | 1.00 | `order_reference` matches PO `name` exactly |
| 2 | `po_confirmation_no` | 0.95 | `order_reference` matches PO `order_confirmation_no` |
| 3 | `items_supplier_date` | 0.70–0.90 | Supplier + item overlap (Jaccard >= 0.7) + date proximity (±90 days), boosted by amount match and date closeness |

**Purchase Receipt matching** (`find_matching_purchase_receipt`):

| Priority | Method | Confidence | Description |
|---|---|---|---|
| 1 | `pr_linked_to_po` | 0.95 | PR Item has `purchase_order` linking to matched PO |
| 2 | `pr_items_supplier_date` | 0.65–0.85 | Supplier + item overlap + date proximity |

**Purchase Invoice matching** (`find_matching_purchase_invoice`):

| Priority | Method | Confidence | Description |
|---|---|---|---|
| 1 | `bill_no` | 1.00 | `document_number` matches PI `bill_no` + same supplier |
| 2 | `pi_linked_to_po` | 0.90 | PI Item has `purchase_order` linking to matched PO |
| 3 | `pi_amount_date` | 0.60–0.80 | Supplier + total amount (±5%) + date proximity |

**Ambiguity check**: For fuzzy matches (priority 3), if top-2 candidates are within 0.10 confidence, no match is returned (too ambiguous).

**Item linking**: When a match is found, `build_item_links()` maps extracted items to existing doc item rows (by item_code, then keyword overlap, then qty+rate). These links populate `purchase_order_item` on PR items and `po_detail`/`pr_detail` on PI items for ERPNext's partial receipt/invoice tracking.

**Result flags**: `build_chain()` returns `*_matched` booleans (e.g. `purchase_order_matched: True`), `*_match_method`, and `*_match_confidence` for each document type.

### Item Matching (`_resolve_item` / `_try_resolve_item` in `purchase_order.py`)

All three chain builders (PO, PR, PI) use the same 4-step matching hierarchy via `_resolve_item`. The `_try_resolve_item` variant runs steps 1-3 only (no creation) and is used by `document_matcher.py` during matching to avoid side effects:

1. **Supplier + supplier_part_no** — query `Item Supplier` child table for matching `supplier` + `supplier_part_no`, prefer items with `delivered_by_supplier=1`; also accept non-drop-ship if exact match
2. **Item code + text overlap** — find items with `delivered_by_supplier=1` where ERPNext item_code matches the extracted code, AND at least one keyword from item_name/description overlaps
3. **Text match** — search any Item by `item_name` LIKE with extracted keywords, score candidates by keyword overlap (requires 2+ keyword matches to accept)
4. **Create new Item** — creates with `delivered_by_supplier=1`, populates `supplier_items` child table with supplier + `supplier_part_no` from extracted data (only in `_resolve_item`, not `_try_resolve_item`)

Keywords are extracted by tokenizing item_name + description, filtering out words < 3 chars and German/English stopwords, sorted longest-first for specificity.

### ERPNext Default Lookups

All chain builders dynamically query defaults instead of hardcoding names:

| Lookup | Primary Source | Fallback |
|---|---|---|
| Supplier Group | `Buying Settings.supplier_group` | First non-group Supplier Group |
| Item Group | `Stock Settings.item_group` | First non-group Item Group |
| Warehouse | `Company.default_warehouse` | First non-group Warehouse (filtered by company) |
| Expense Account | `Company.default_expense_account` | First non-group Expense account (filtered by company) |
| Tax Account | Default Purchase Taxes and Charges Template | First Tax-type account (filtered by company) |

All throw clear errors if no fallback exists.

### Item Type Classification

The LLM classifies each line item as `item_type: "stock"` (physical goods) or `"service"` (fees, shipping, licenses, consulting, installation). New Items are created with `is_stock_item=0` for services, `is_stock_item=1` for stock. The field is defined in `llm/schemas.py` (LineItem) and sanitized in `retrospective.py` to only allow "stock"/"service"/None.

### Shipping & Discount Line Item Extraction

`sanitize_extracted_data()` in `retrospective.py` detects and extracts special line items before they reach the chain builders:

- **Shipping items** — removed from the items list when `shipping_cost` is already set (avoids double-counting). Detected by keywords (versand, shipping, freight, dhl, dpd, ups, etc.) via `_is_shipping_item()`.
- **Discount items** (Rabatt/Skonto/Vorkasserabatt) — removed from the items list and summed into `discount_amount`. Detected by keyword AND negative `total_price` via `_is_discount_item()`. Applied as document-level `discount_amount` with `apply_discount_on: "Net Total"` on PO/PR/PI, so it reduces the invoiced amount without affecting individual item rates or warehouse stock values.
- **Surcharge items** (Mindermengenaufschlag/Kleinmengenzuschlag) — removed from items list and summed into `surcharge_amount`. Detected by keyword via `_is_surcharge_item()`. Applied as "Actual" charge in `_build_taxes()` (like shipping), so it increases item cost proportionally.

### Item Code Consistency Across Chain

When the chain builder creates PO → PR → PI, downstream builders (PR, PI) must use the same `item_code` as the PO. The `po_item_links` / `pr_item_links` dicts carry `item_code` from upstream documents. PR/PI builders prioritize linked item_code over re-resolution to prevent ERPNext validation errors ("Item Code must be equal to...").

### UOM Mapping

German UOM aliases from LLM output are mapped to ERPNext standard UOMs (`_resolve_uom` in `purchase_order.py`): `Stk/Stück` → system piece UOM (dynamically resolved), `kg` → `Kg`, etc. Falls back to the system piece UOM if no match. UOM Conversion Factor records require a `category` field (Link to UOM Category) — `_get_uom_category()` prefers "Anzahl", falls back to the first available category.

### Tax Handling

PO and PI include `Purchase Taxes and Charges` rows built from the per-item `tax_rate` extracted by the LLM. Tax account is resolved from the company's default Purchase Taxes and Charges Template, falling back to the first Tax-type account.

### LLM Amount Convention

Prompts explicitly instruct LLMs to return **NET amounts** (before tax / Netto) for all monetary fields (`unit_price`, `total_price`, `subtotal`, `total_amount`). This matches ERPNext's expectation where tax is applied separately via Tax Templates. The `OutputGuard` plausibility check accepts both net-style (`items + tax = total`) and gross-style (`items = total`) totals without false-flagging.

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
- **Never hardcode ERPNext master data names** (Supplier Group, Item Group, Warehouse, Account). Always query defaults from Settings/Company first, then fall back to dynamic lookup, then `frappe.throw()` with a clear message
- Item matching logic lives in `purchase_order._resolve_item()` and is reused by all three chain builders via import
- UOM resolution lives in `purchase_order._resolve_uom()` and is reused by all three chain builders via import
- New items are created with `delivered_by_supplier=1` and linked to the supplier via `supplier_items` child table. `is_stock_item` is set based on `item_type` ("service" → 0, else → 1)
- LLM extraction uses NET amounts; tax is added separately via `Purchase Taxes and Charges` rows on PO/PI
- PR/PI builders must use `item_code` from `po_item_links`/`pr_item_links` when available — never re-resolve independently
- Shipping and discount line items are extracted into document-level fields during sanitization, not kept as item rows
- Phone numbers: `_clean_phone()` replaces `/` with space (German area/number format with `/` is rejected by Frappe)
- UOM Conversion Factor requires `category` field — use `_get_uom_category()` (prefers "Anzahl")
- `frm.call("method_name")` (string form) invokes document methods; `frm.call({ method: "name" })` (object form) resolves as module-level function — use string form for `@frappe.whitelist()` methods on the DocType class
- `process_document` on AI Procurement Job allows statuses: Pending, Error, Needs Review
- `frappe.enqueue()` parameter naming: use `procurement_job_name` (not `job_name`) to avoid collision with frappe's reserved `job_name` parameter
- `get_password()` calls must use `raise_exception=False` for optional API key fields
