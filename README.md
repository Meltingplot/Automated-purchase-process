# ERPNext Procurement AI

A Frappe v15 custom app that automates the purchase process end-to-end. Upload any procurement document — a shopping cart, order confirmation, delivery note, or invoice — and the plugin extracts structured data using multiple LLMs, builds consensus across their outputs, and creates the complete ERPNext document chain (Supplier, Purchase Order, Purchase Receipt, Purchase Invoice) automatically.

**Author:** Tim Schneider | **License:** MIT | **Python:** >=3.11

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [How It Works](#how-it-works)
- [Supported Document Types](#supported-document-types)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Human Review Workflow](#human-review-workflow)
- [API Reference](#api-reference)
- [Architecture Overview](#architecture-overview)
- [Security](#security)
- [Running Tests](#running-tests)
- [Contributing](#contributing)
- [License](#license)

---

## Why This Exists

Procurement in small and mid-sized businesses often looks like this:

1. You receive an invoice PDF from a supplier via email
2. Someone manually reads the PDF and types the data into ERPNext
3. They create a Purchase Order, then a Purchase Receipt, then a Purchase Invoice — all by hand
4. Mistakes happen: wrong amounts, missing items, typos in supplier names

This plugin eliminates steps 2-4. Upload the document, review the AI extraction if you want, and the complete document chain appears in ERPNext — with the original PDF attached.

---

## How It Works

```
  ┌───────────────────────────────────────────────────────────────────┐
  │  1. Upload          PDF, image, or email attachment               │
  │  2. Extract         pdfplumber pulls text + page images from PDF  │
  │  3. Sanitize        Strip invisible chars, detect injection       │
  │  4. OCR Baseline    Tesseract/EasyOCR runs on page images as an  │
  │                     independent cross-check (not primary source)  │
  │  5. Extract (LLM)   Page images + text sent to 2-3 LLM providers │
  │                     in parallel — LLMs read the document directly │
  │  6. Validate        JSON schema + arithmetic plausibility         │
  │  7. Consensus       Field-by-field majority voting + OCR cross-   │
  │                     check against the independent OCR baseline    │
  │  8. Review (opt.)   Human review UI with per-field confidence     │
  │  9. Build Chain     Supplier → PO → PR → PI as needed            │
  │ 10. Attach          Source file linked to all created docs        │
  └───────────────────────────────────────────────────────────────────┘
```

Cloud LLMs (Claude, GPT-4o, Gemini) receive the actual document images via their vision capabilities, so they read the document directly — just like a human would. The extracted text from pdfplumber is included as supplementary context. A separate OCR pass (Tesseract/EasyOCR) runs independently as a cross-check baseline for the consensus engine.

Multiple LLMs extract data independently, then a consensus engine votes field-by-field to produce the most accurate result. No single LLM hallucination can corrupt your data.

---

## Supported Document Types

| Document You Upload     | What Gets Created in ERPNext                          |
|-------------------------|-------------------------------------------------------|
| Shopping Cart            | Purchase Order                                        |
| Order Confirmation       | Purchase Order                                        |
| Delivery Note            | Purchase Order + Purchase Receipt                     |
| Invoice                  | Purchase Order + Purchase Receipt + Purchase Invoice   |

The plugin always works **retrospectively** — it builds the full chain regardless of which document you start with.

---

## Installation

### Prerequisites

- Frappe v15 + ERPNext v15
- Python >= 3.11
- At least one LLM API key (Claude, OpenAI, or Gemini)
- Optional: Tesseract or EasyOCR for scanned document support

### Install on your Frappe bench

```bash
# Get the app
bench get-app https://github.com/meltingplot/automated-purchase-process.git

# Install on your site
bench --site your-site.localhost install-app procurement_ai

# Apply custom fields and fixtures
bench --site your-site.localhost migrate

# Restart workers
bench restart
```

---

## Configuration

After installation, go to **AI Procurement Settings** in ERPNext:

> Search bar → "AI Procurement Settings" → Open

### Minimal Setup

```
Default Company:        Your Company Name
Claude API Key:         sk-ant-...
OpenAI API Key:         sk-...          (recommended for consensus)
Confidence Threshold:   0.7
Require Document Review: ✓              (recommended to start)
```

### LLM Providers

You need **at least one** cloud provider API key. For reliable consensus, use **two or more**:

| Provider | Setting               | Models Used                |
|----------|-----------------------|----------------------------|
| Claude   | `claude_api_key`      | Via langchain-anthropic    |
| OpenAI   | `openai_api_key`      | Via langchain-openai       |
| Gemini   | `gemini_api_key`      | Via langchain-google-genai |

With only one provider, the plugin forces human review (no auto-acceptance without consensus).

### Local LLM Support (Optional)

Run extraction on your own hardware using any OpenAI-compatible API:

```
Enable Local LLM:       ✓
Local LLM Provider:     Ollama          (or vLLM, llama.cpp, LM Studio)
Local LLM Base URL:     http://localhost:11434/v1
Local LLM Model:        llama3:70b
Local LLM Trust Level:  full            (full for 70B+, reduced for 13-70B)
```

### Key Settings

| Setting                    | Default | Description                                           |
|----------------------------|---------|-------------------------------------------------------|
| `enable_auto_processing`   | Off     | Auto-process documents on upload                      |
| `require_document_review`  | On      | Pause for human review before creating documents      |
| `confidence_threshold`     | 0.7     | Minimum confidence to accept extraction               |
| `min_llm_consensus`        | 2       | Minimum LLM providers that must agree                 |
| `auto_submit_documents`    | Off     | Submit (finalize) created documents automatically     |
| `ocr_engine`               | Tesseract | OCR engine for scanned documents                    |
| `amount_tolerance`         | 0.05    | Tolerance for amount verification (5%)                |

---

## Usage

### Example 1: Upload an Invoice via the UI

1. Go to **AI Procurement Job** → **New**
2. Attach your invoice PDF
3. Set Source Type to "Auto-Detect" (or pick "Invoice")
4. Save

The job enters **Processing** status. Within seconds, the LLMs extract all data — supplier name, address, line items, amounts, tax, dates — and run consensus.

If `require_document_review` is enabled, the job moves to **Awaiting Review**. You'll see:

- All extracted header fields (supplier, dates, totals) with confidence badges showing "2/3 LLMs agree"
- A line items table where you can edit quantities, prices, or map items to existing ERPNext Items
- An **Approve & Create Documents** button

Click approve, and the plugin creates:
- A **Supplier** (if new)
- A **Purchase Order** (with all line items, tax, shipping, discounts)
- A **Purchase Receipt** (linked to the PO)
- A **Purchase Invoice** (linked to the PO and PR, with `bill_no` set)

All three documents have the original PDF attached and are linked back to the AI Procurement Job for traceability.

### Example 2: Upload via API

```python
import requests

# Upload a PDF and start processing
url = "https://your-site.com/api/method/procurement_ai.procurement_ai.api.ingest.process"
files = {"file": open("invoice_2024_0042.pdf", "rb")}
data = {"source_type": "Invoice"}

response = requests.post(url, files=files, data=data, headers={
    "Authorization": "token api_key:api_secret"
})

result = response.json()
print(result)
# {
#     "message": {
#         "job_name": "AIPROC-0001",
#         "status": "Processing"
#     }
# }
```

### Example 3: Check Job Status

```python
import requests

url = "https://your-site.com/api/method/procurement_ai.procurement_ai.api.status.get_job_status"
params = {"job_name": "AIPROC-0001"}

response = requests.get(url, params=params, headers={
    "Authorization": "token api_key:api_secret"
})

result = response.json()
print(result)
# {
#     "message": {
#         "name": "AIPROC-0001",
#         "status": "Completed",
#         "detected_type": "Invoice",
#         "confidence_score": 0.92,
#         "created_supplier": "SUP-0012",
#         "created_po": "PO-2024-0042",
#         "created_receipt": "PR-2024-0038",
#         "created_invoice": "PI-2024-0029"
#     }
# }
```

### Example 4: Dashboard Overview

```python
import requests

url = "https://your-site.com/api/method/procurement_ai.procurement_ai.api.status.get_dashboard_stats"

response = requests.get(url, headers={
    "Authorization": "token api_key:api_secret"
})

result = response.json()
print(result)
# {
#     "message": {
#         "total_jobs": 156,
#         "status_counts": {
#             "Completed": 142,
#             "Processing": 2,
#             "Awaiting Review": 5,
#             "Error": 7
#         },
#         "recent_jobs": [...],
#         "open_escalations": [...]
#     }
# }
```

---

## Human Review Workflow

When `require_document_review` is enabled (recommended), the pipeline pauses after LLM extraction:

```
Upload → Extract → Consensus → [ Awaiting Review ] → Approve → Create Documents
```

The review form shows:

- **Header fields** — Supplier, dates, document number, totals — each with a confidence badge (e.g., "3/3" means all LLMs agreed)
- **Line items table** — Item name, quantity, unit price, total, tax rate — all editable
- **Item mapping** — A "Map to Item" dropdown on each row lets you select an existing ERPNext Item. This bypasses automatic fuzzy matching and ensures the correct item is used
- **Stock UOM mapping** — Override the auto-detected unit of measure per item

After review, click **Approve & Create Documents**. The chain builder uses your corrections and mappings.

---

## API Reference

### `POST /api/method/procurement_ai.procurement_ai.api.ingest.process`

Upload a procurement document and start processing.

| Parameter     | Type   | Required | Description                                              |
|---------------|--------|----------|----------------------------------------------------------|
| `file`        | File   | Yes      | PDF or image file                                        |
| `source_type` | String | No       | `Auto-Detect`, `Cart`, `Order Confirmation`, `Delivery Note`, `Invoice` |

**Requires:** `create` permission on AI Procurement Job, Supplier, Purchase Order, Purchase Receipt, Purchase Invoice, Item.

### `GET /api/method/procurement_ai.procurement_ai.api.status.get_job_status`

| Parameter  | Type   | Required | Description          |
|------------|--------|----------|----------------------|
| `job_name` | String | Yes      | e.g., `AIPROC-0001` |

### `GET /api/method/procurement_ai.procurement_ai.api.status.get_dashboard_stats`

Returns aggregated status counts, recent jobs, and open escalations. Requires `read` permission on AI Procurement Job.

---

## Architecture Overview

### Module Layout

```
procurement_ai/
├── chain_builder/          # Creates ERPNext documents (Supplier → PO → PR → PI)
│   ├── retrospective.py    # Orchestrator: decides what to create based on doc type
│   ├── document_matcher.py # "Find before create" — matches existing PO/PR/PI
│   ├── supplier.py         # Supplier creation/matching
│   ├── purchase_order.py   # PO builder + item resolution + UOM mapping
│   ├── purchase_receipt.py # PR builder (linked to PO)
│   ├── purchase_invoice.py # PI builder (linked to PO + PR)
│   └── attachments.py      # Attach source PDF to created documents
│
├── extraction/             # Text extraction from uploaded files
│   ├── pdf_parser.py       # pdfplumber for native PDFs
│   ├── ocr_engine.py       # Tesseract / EasyOCR for scanned docs
│   ├── preprocessor.py     # Image preprocessing for better OCR
│   └── email_parser.py     # Email attachment handling
│
├── llm/                    # Multi-LLM extraction pipeline
│   ├── graph.py            # LangGraph StateGraph (8-node pipeline)
│   ├── nodes.py            # Individual pipeline nodes
│   ├── models.py           # LLM provider factory (Claude/OpenAI/Gemini/Local)
│   ├── prompts.py          # Extraction prompts (NET amounts, structured output)
│   ├── sanitizer.py        # InputSanitizer: injection detection, NFKC normalization
│   ├── output_guard.py     # OutputGuard: JSON validation, plausibility checks
│   ├── consensus.py        # ConsensusEngine: field-by-field majority voting
│   └── schemas.py          # Pydantic v2 models (ExtractedDocument, LineItem, ...)
│
├── validation/             # Post-extraction validation
│   ├── field_validator.py  # Required field checks
│   ├── amount_checker.py   # Arithmetic verification
│   └── supplier_matcher.py # Fuzzy supplier matching
│
└── procurement_ai/         # Frappe module (DocTypes, API, Pages)
    ├── api/
    │   ├── ingest.py       # Upload + pipeline entry point
    │   └── status.py       # Job status + dashboard stats
    ├── doctype/
    │   ├── ai_procurement_job/        # Central job record
    │   ├── ai_extraction_result/      # Per-LLM extraction (child table)
    │   ├── ai_procurement_settings/   # Plugin configuration (single)
    │   └── ai_escalation_log/         # Consensus failure tracking
    └── page/
        └── procurement_ai_dashboard/  # Custom dashboard page
```

### Document Matching: "Find Before Create"

The chain builder doesn't blindly create documents. Before creating a Purchase Order, Receipt, or Invoice, it searches for existing matches:

```
Incoming invoice with PO reference "PO-2024-0042"
  → Exact match on PO name?           ✓ Found PO-2024-0042 (confidence: 1.0)
  → PR linked to that PO?             ✓ Found PR-2024-0038 (confidence: 0.95)
  → PI with same bill_no + supplier?   ✗ Not found → Create new PI
```

This prevents duplicate documents and correctly links to your existing procurement chain.

### Item Resolution

When creating Purchase Orders, items are matched to existing ERPNext Items using a 4-step hierarchy:

1. **Supplier Part Number** — Match via `Item Supplier` child table
2. **Item Code + Keywords** — ERPNext item_code matches + text overlap in name/description
3. **Text Search** — Fuzzy keyword matching across all Items (requires 2+ keyword matches)
4. **Create New** — If no match found, creates a new Item linked to the supplier

### Escalation System

When the pipeline encounters problems, it creates an **AI Escalation Log** entry instead of silently failing:

| Escalation Type    | Trigger                                           |
|--------------------|---------------------------------------------------|
| Low Confidence     | Consensus score below threshold                   |
| Field Dispute      | LLMs disagree on critical fields                  |
| Amount Mismatch    | Extracted total doesn't match item sum            |
| Supplier Unclear   | Can't determine or match supplier                 |
| OCR Mismatch       | OCR text contradicts LLM extraction               |
| Processing Error   | Unexpected error during pipeline                  |

Escalations can trigger email notifications to a configured address.

---

## Security

The plugin implements multiple security layers to prevent prompt injection and data corruption:

1. **InputSanitizer** — Normalizes Unicode (NFKC), strips invisible characters, detects 13 prompt injection patterns before any text reaches an LLM
2. **Prompt Isolation** — Document content is wrapped in `--- BEGIN/END DOCUMENT DATA ---` delimiters, never placed in the instruction portion of prompts
3. **OutputGuard** — Validates LLM responses against Pydantic schemas, runs arithmetic plausibility checks (do the line items actually add up?)
4. **Multi-LLM Consensus** — Even if one LLM is manipulated, majority voting across 2-3 independent providers catches inconsistencies
5. **Local LLM Trust Levels** — Smaller local models get reduced voting weight (`full` for 70B+, `reduced` for 13-70B, `validation_only` for smaller)
6. **File Upload Validation** — MIME type checking, file size limits, and extension allowlisting

---

## Running Tests

Tests are standalone unit tests using pytest + unittest.mock. No Frappe site or database required.

```bash
# Run all tests
pytest tests/

# Run a specific test file
pytest tests/test_sanitizer.py

# Run with verbose output
pytest tests/ -v
```

### Test Coverage

| Test File                  | What It Tests                              |
|----------------------------|--------------------------------------------|
| `test_sanitizer.py`       | Input sanitization, injection detection    |
| `test_consensus.py`       | Multi-LLM consensus voting                 |
| `test_output_guard.py`    | LLM output validation, plausibility checks |
| `test_schemas.py`         | Pydantic data model validation             |
| `test_models.py`          | LLM provider factory                       |
| `test_amount_checker.py`  | Amount verification logic                  |
| `test_field_validator.py` | Required field validation                  |
| `test_security.py`        | File upload security                       |
| `test_supplier_link.py`   | Supplier matching                          |

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Write tests for new functionality
4. Ensure all tests pass: `pytest tests/ -v`
5. Submit a pull request

Code style: formatted with **black**, imports sorted with **isort** (black profile).

---

## License

MIT License - see [LICENSE](LICENSE) for details.

Copyright (c) 2026 Tim Schneider
