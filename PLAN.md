# Automatisierung des Einkaufsprozesses in ERPNext

## 1. Übersicht

### Ziel
Automatisierte Erfassung und Abbildung des gesamten Einkaufsprozesses in ERPNext — vom Eingang eines Belegdokuments bis zum Wareneingang und Eingangsrechnung. Zahlungen sind zunächst ausgenommen.

**Architektur-Entscheidung:** Natives ERPNext/Frappe Plugin (Custom App), kein separater Service.

### Eingangsdokumente (eines der folgenden)
| Dokumenttyp | Typischer Auslöser |
|---|---|
| **Einkaufskorb** (z.B. Screenshot/PDF eines Online-Shop-Warenkorbs) | Bestellung soll ausgelöst werden |
| **Bestellbestätigung** (AB) | Lieferant bestätigt Bestellung |
| **Lieferschein** | Ware ist angekommen |
| **Eingangsrechnung** | Rechnung vom Lieferanten eingegangen |

### Rahmenbedingungen
- ~20–40 Prozesse pro Monat
- Cloud-LLM und lokale LLM-Nutzung möglich (konfigurierbar)
- **Dual-Model-Extraktion**: Jedes Dokument wird von mindestens 2 verschiedenen Modellen extrahiert, Ergebnisse werden verglichen
- Prompt-Injection-Schutz by Design

---

## 2. Architektur: Natives Frappe Plugin

### 2.1 Warum Frappe App statt separatem Service?
- Direkter Zugriff auf ERPNext-Datenbank (kein REST-API-Overhead)
- Natürliche Integration in ERPNext-UI (DocTypes, Listenansichten, Dashboards)
- Frappe Background Jobs (RQ) für asynchrone LLM-Verarbeitung
- Berechtigungsmanagement über Frappe Rollen
- Kein separater Server nötig

### 2.2 Komponentenübersicht

```
ERPNext / Frappe
┌────────────────────────────────────────────────────────────────┐
│  Frappe App: "purchase_automation"                             │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Custom DocType: "Purchase Document"                      │  │
│  │ Upload + Status-Tracking + Review-Workflow               │  │
│  └──────────────┬───────────────────────────────────────────┘  │
│                 │                                              │
│                 ▼                                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Dokument-Vorverarbeitung (Tiered Pipeline)              │  │
│  │                                                          │  │
│  │  ┌─ XRechnung/ZUGFeRD erkannt?                          │  │
│  │  │   JA → XML extrahieren, Schema-Validierung           │  │
│  │  │        → Deterministisches Parsing (KEIN LLM)        │  │
│  │  │   NEIN ↓                                              │  │
│  │  ├─ PDF mit extrahierbarem Text?                        │  │
│  │  │   JA → PyMuPDF Sanitisierung (JS, Metadaten,        │  │
│  │  │        Annotationen, Hidden Text entfernen)           │  │
│  │  │        → Sanitisierten Text an LLM                   │  │
│  │  │   NEIN ↓                                              │  │
│  │  └─ Bild-basiertes PDF / reines Bild                    │  │
│  │      → PDF-Seiten als Bild rendern                      │  │
│  │      → Metadaten verwerfen                              │  │
│  │      → Bilder an LLM (Vision)                           │  │
│  └──────────────┬───────────────────────────────────────────┘  │
│                 │                                              │
│                 ▼                                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Dual-Model LLM-Extraktion (Background Job)              │  │
│  │ (nur für Nicht-XRechnung/ZUGFeRD-Dokumente)             │  │
│  │                                                          │  │
│  │  ┌─────────────┐        ┌─────────────┐                 │  │
│  │  │  Modell A    │        │  Modell B    │                │  │
│  │  │  (Primary)   │        │  (Secondary) │                │  │
│  │  └──────┬──────┘        └──────┬──────┘                 │  │
│  │         │                      │                         │  │
│  │         ▼                      ▼                         │  │
│  │  ┌──────────────────────────────────┐                    │  │
│  │  │    Ergebnis-Vergleich            │                    │  │
│  │  │    ├── Felder stimmen überein?   │                    │  │
│  │  │    ├── Confidence-Score          │                    │  │
│  │  │    └── Diskrepanz → Eskalation   │                    │  │
│  │  └──────────────────────────────────┘                    │  │
│  └──────────────┬───────────────────────────────────────────┘  │
│                 │                                              │
│                 ▼                                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Validierung & Stammdaten-Abgleich                        │  │
│  │ Pydantic-Schema + ERPNext Supplier/Item Matching         │  │
│  └──────────────┬───────────────────────────────────────────┘  │
│                 │                                              │
│                 ▼                                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ ERPNext-Orchestrierung                                   │  │
│  │ Purchase Order → Purchase Receipt → Purchase Invoice     │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Settings DocType: "Purchase Automation Settings"         │  │
│  │ LLM-Provider, API-Keys, Modell-Konfiguration            │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

### 2.3 Technologie-Stack

| Komponente | Technologie | Begründung |
|---|---|---|
| **Sprache** | Python 3.11+ | Frappe-Ökosystem, LLM-SDKs |
| **Framework** | Frappe (Custom App) | Native ERPNext-Integration |
| **LLM-Abstraktion** | Eigene Provider-Schicht | Cloud (Anthropic, OpenAI) + Lokal (Ollama, OpenAI-kompatibel) |
| **Datenvalidierung** | Pydantic v2 | Strenge Schema-Validierung der LLM-Ausgabe |
| **PDF-Verarbeitung** | PyMuPDF (fitz) | PDF-Sanitisierung, Text-Extraktion, Bild-Rendering (Fallback) |
| **E-Invoicing** | lxml | XRechnung/ZUGFeRD XML-Parsing & Schema-Validierung |
| **Background Jobs** | Frappe RQ (Redis Queue) | Bereits in Frappe integriert |
| **Persistenz** | MariaDB (via Frappe) | Frappe-Standard, Custom DocTypes |

---

## 3. Dual-Model-Extraktion

### 3.1 Konzept

Jedes Dokument wird unabhängig von **mindestens zwei verschiedenen LLM-Modellen** extrahiert. Die Ergebnisse werden automatisch verglichen.

```
Dokument (sanitisierter Text ODER Bild)
     │
     ├──────────────────────┐
     ▼                      ▼
┌─────────────┐      ┌─────────────┐
│  Modell A    │      │  Modell B    │
│  z.B. Claude │      │  z.B. Ollama │
│  Sonnet      │      │  Llama 3.2   │
└──────┬──────┘      └──────┬──────┘
       │                    │
       ▼                    ▼
   JSON Result A       JSON Result B
       │                    │
       └────────┬───────────┘
                ▼
     ┌────────────────────┐
     │  Vergleichs-Engine  │
     │                    │
     │  Feld-für-Feld:    │
     │  ├── Identisch?    │──▶ Auto-Accept
     │  ├── Abweichung    │──▶ Heuristik / Plausibilität
     │  │   < Schwelle?   │
     │  └── Konflikt?     │──▶ Eskalation → Review
     └────────────────────┘
```

### 3.2 Vergleichsregeln

| Feldtyp | Vergleichslogik | Toleranz |
|---|---|---|
| **Lieferantenname** | Normalisiert (lowercase, trim) | Levenshtein ≤ 3 |
| **Beträge** (Decimal) | Numerischer Vergleich | ±0.01 (Rundungsdifferenzen) |
| **Artikelbezeichnung** | Token-basierter Vergleich | Jaccard-Similarity ≥ 0.8 |
| **Mengen** | Exakter Vergleich | 0 (müssen identisch sein) |
| **Datum** | Exakter Vergleich | 0 |
| **Dokumentnummer** | Normalisiert | Exakt nach Normalisierung |

### 3.3 Eskalationsstufen

```
LEVEL 0: AUTO-ACCEPT
  Beide Modelle liefern identische Ergebnisse
  → Automatische Weiterverarbeitung

LEVEL 1: AUTO-RESOLVE
  Kleine Abweichungen innerhalb Toleranz
  → Verwende Ergebnis mit höherer Plausibilität
  → Logge Abweichung

LEVEL 2: REVIEW
  Signifikante Abweichungen bei nicht-kritischen Feldern
  → Markiere als "Review Required"
  → Zeige Diff-Ansicht im DocType

LEVEL 3: REJECT
  Abweichungen bei kritischen Feldern (Beträge, Mengen)
  → Automatisch abgelehnt
  → Manuelle Eingabe erforderlich
```

---

## 4. LLM-Provider-Abstraktion

### 4.1 Provider-Interface

```python
class LLMProvider(ABC):
    """Abstrakte Basis für alle LLM-Provider."""

    @abstractmethod
    def extract_document(
        self,
        prompt: str,
        text: str | None = None,
        images: list[bytes] | None = None,
    ) -> dict:
        """Extrahiert strukturierte Daten aus Dokumenttext oder -bildern.

        Je nach Preprocessing-Ergebnis wird entweder sanitisierter Text
        ODER gerenderte Bilder übergeben (nie beides gleichzeitig).
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Prüft ob der Provider verfügbar ist."""
        ...
```

### 4.2 Unterstützte Provider

| Provider | Typ | Modelle | Multimodal |
|---|---|---|---|
| **Anthropic** | Cloud | Claude Sonnet, Haiku | Ja |
| **OpenAI** | Cloud | GPT-4o, GPT-4o-mini | Ja |
| **Ollama** | Lokal | Llama 3.2 Vision, LLaVA | Ja |
| **OpenAI-kompatibel** | Lokal/Cloud | vLLM, LocalAI, LM Studio | Ja* |

*abhängig vom Modell

### 4.3 Konfiguration über ERPNext Settings

```
Purchase Automation Settings (Single DocType)
├── Primary LLM Provider: [Anthropic/OpenAI/Ollama/OpenAI-Compatible]
├── Primary Model: [claude-sonnet-4-20250514 / gpt-4o / llama3.2-vision / ...]
├── Primary API Key: ****  (Password-Feld)
├── Primary API URL: https://api.anthropic.com  (für Ollama: http://localhost:11434)
├── Secondary LLM Provider: [...]
├── Secondary Model: [...]
├── Secondary API Key: ****
├── Secondary API URL: [...]
├── Auto-Accept Threshold: 0.95  (Übereinstimmung für Auto-Accept)
├── Review Threshold: 0.70  (unter diesem Wert → Reject)
└── Max Retries: 2
```

---

## 5. Custom DocTypes

### 5.1 Purchase Document (Hauptbeleg)

| Feld | Typ | Beschreibung |
|---|---|---|
| `document_name` | Data | Auto-generierter Name |
| `source_file` | Attach | Hochgeladenes PDF/Bild |
| `document_type` | Select | Einkaufskorb/AB/Lieferschein/Rechnung |
| `status` | Select | Uploaded/Processing/Extracted/Review/Approved/Error |
| `extraction_a_result` | JSON | Ergebnis Modell A |
| `extraction_b_result` | JSON | Ergebnis Modell B |
| `merged_result` | JSON | Zusammengeführtes Ergebnis |
| `comparison_score` | Float | Übereinstimmungsgrad (0–1) |
| `comparison_details` | JSON | Feld-für-Feld Vergleich |
| `review_notes` | Text | Anmerkungen vom Reviewer |
| `linked_purchase_order` | Link → Purchase Order | Erstellter PO |
| `linked_purchase_receipt` | Link → Purchase Receipt | Erstellter Wareneingang |
| `linked_purchase_invoice` | Link → Purchase Invoice | Erstellte Rechnung |
| `error_log` | Text | Fehlermeldungen |
| `processing_time_seconds` | Float | Verarbeitungsdauer |

### 5.2 Purchase Document Item (Child Table)

| Feld | Typ | Beschreibung |
|---|---|---|
| `item_description` | Data | Extrahierte Bezeichnung |
| `supplier_item_code` | Data | Lieferanten-Artikelnr. |
| `quantity` | Float | Menge |
| `unit` | Data | Einheit |
| `unit_price` | Currency | Einzelpreis |
| `total_price` | Currency | Gesamtpreis |
| `matched_item` | Link → Item | Gematchter ERPNext-Artikel |
| `match_confidence` | Percent | Matching-Konfidenz |

### 5.3 LLM Extraction Log (Audit-Trail)

| Feld | Typ | Beschreibung |
|---|---|---|
| `purchase_document` | Link | Zugehöriges Dokument |
| `provider` | Data | LLM-Provider |
| `model` | Data | Modellname |
| `prompt_tokens` | Int | Input-Tokens |
| `completion_tokens` | Int | Output-Tokens |
| `duration_seconds` | Float | Antwortzeit |
| `raw_response` | JSON | Rohe LLM-Antwort |
| `extraction_result` | JSON | Validiertes Ergebnis |
| `error` | Text | Fehler (falls aufgetreten) |

---

## 6. Projektstruktur (Frappe App)

```
purchase_automation/
├── pyproject.toml
├── setup.py
├── requirements.txt
├── README.md
├── license.txt
│
├── purchase_automation/
│   ├── __init__.py
│   ├── hooks.py                          # Frappe Integration Hooks
│   ├── modules.txt                       # Modulnamen
│   ├── patches.txt                       # DB-Migrationen
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   └── desktop.py                    # Desktop/Sidebar-Menü
│   │
│   ├── purchase_automation/              # Hauptmodul
│   │   ├── doctype/
│   │   │   ├── purchase_document/        # Haupt-DocType
│   │   │   │   ├── purchase_document.json
│   │   │   │   ├── purchase_document.py
│   │   │   │   └── purchase_document.js
│   │   │   │
│   │   │   ├── purchase_document_item/   # Child Table
│   │   │   │   ├── purchase_document_item.json
│   │   │   │   └── purchase_document_item.py
│   │   │   │
│   │   │   ├── llm_extraction_log/       # Audit-Log DocType
│   │   │   │   ├── llm_extraction_log.json
│   │   │   │   └── llm_extraction_log.py
│   │   │   │
│   │   │   └── purchase_automation_settings/  # Settings (Single)
│   │   │       ├── purchase_automation_settings.json
│   │   │       └── purchase_automation_settings.py
│   │   │
│   │   └── report/                       # Custom Reports (später)
│   │
│   ├── llm/                              # LLM-Abstraktion
│   │   ├── __init__.py
│   │   ├── base.py                       # ABC Provider Interface
│   │   ├── anthropic_provider.py         # Claude
│   │   ├── openai_provider.py            # OpenAI + kompatible
│   │   ├── ollama_provider.py            # Ollama (lokal)
│   │   └── registry.py                   # Provider-Registry & Factory
│   │
│   ├── extraction/                       # Extraktions-Pipeline
│   │   ├── __init__.py
│   │   ├── preprocessor.py              # Tiered Preprocessing-Router
│   │   ├── pdf_sanitizer.py             # PyMuPDF: JS/Metadaten/Hidden-Text entfernen
│   │   ├── einvoice_parser.py           # XRechnung/ZUGFeRD XML-Extraktion (kein LLM)
│   │   ├── extractor.py                 # Dual-Model Extraktion
│   │   ├── comparator.py               # Ergebnis-Vergleich
│   │   ├── prompt_templates.py          # Prompt-Verwaltung
│   │   └── schemas.py                   # Pydantic-Modelle
│   │
│   ├── matching/                         # Stammdaten-Abgleich
│   │   ├── __init__.py
│   │   ├── supplier_matcher.py
│   │   └── item_matcher.py
│   │
│   ├── orchestrator/                     # Prozess-Steuerung
│   │   ├── __init__.py
│   │   └── workflow.py                  # DocType → ERPNext Belege
│   │
│   ├── templates/                        # Jinja2 Templates
│   │   └── includes/
│   │
│   └── public/                           # Statische Assets
│       ├── css/
│       └── js/
│
└── tests/
    ├── conftest.py
    ├── test_llm_providers.py
    ├── test_extraction.py
    ├── test_comparator.py
    ├── test_matching.py
    └── fixtures/
        └── sample_documents/
```

---

## 7. Prompt-Injection-Schutz (Security by Design)

### 7.1 Angriffsvektoren in PDFs

| Vektor | Beschreibung | Risiko |
|---|---|---|
| **Versteckter Text** | Weiß-auf-weiß, Schriftgröße 0, Opacity 0 | Hoch — von LLMs gelesen, für Menschen unsichtbar |
| **Annotationen/Kommentare** | Bösartige Anweisungen in PDF-Annotationen | Mittel |
| **Metadaten** | Author/Subject/Keywords-Felder mit Injections | Mittel |
| **JavaScript** | Eingebetteter ausführbarer Code | Hoch |
| **Unicode-Obfuskation** | Zero-Width-Spaces, unsichtbare Zeichen | Mittel |
| **Font-Encoding-Angriffe** | Fonts die Zeichen visuell anders darstellen als intern kodiert | Niedrig |

### 7.2 Tiered Preprocessing Pipeline

Statt pauschalem PDF→Bild-Rendering (teuer, Strukturverlust, OCR-Fehler) wird ein
gestufter Ansatz verwendet, der das Dokument nach Format und Inhalt klassifiziert:

```
Eingehendes Dokument
        │
        ▼
┌───────────────────────────┐
│  Tier 0: Format-Erkennung │
│                           │
│  XRechnung/ZUGFeRD?  ────────▶  Deterministisches XML-Parsing
│  (XML im PDF eingebettet  │     Schema-Validierung (Schematron)
│   oder reines XML)        │     KEIN LLM nötig → direkt in ERPNext
│                           │
│  Reines Bild (PNG/JPG)?  ─────▶  Direkt an LLM Vision (kein PDF-Risiko)
│                           │
│  PDF mit Text?  ──────────────▶  Weiter zu Tier 1
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│  Tier 1: PDF-Sanitisierung│
│  (PyMuPDF document.scrub) │
│                           │
│  Entfernt:                │
│  ├── JavaScript           │
│  ├── Metadaten (Author,   │
│  │   Subject, Keywords)   │
│  ├── Annotationen &       │
│  │   Kommentare           │
│  ├── Hidden Text          │
│  │   (Rendering Mode 3)   │
│  ├── Eingebettete Dateien │
│  └── Zero-Width Unicode   │
│      Zeichen              │
└─────────────┬─────────────┘
              │
              ▼
┌───────────────────────────┐
│  Tier 2: Inhalts-Analyse  │
│                           │
│  Ist das PDF text-basiert │
│  (maschinenlesbar)?       │
│                           │
│  JA  → Sanitisierten Text │
│        extrahieren, an    │
│        LLM als Text       │
│        (kostengünstig,    │
│        strukturerhaltend) │
│                           │
│  NEIN → PDF ist im Kern   │
│         ein Bild (Scan)   │
│         → Seiten als Bild │
│           rendern          │
│         → Metadaten       │
│           verwerfen       │
│         → An LLM Vision   │
└───────────────────────────┘
```

### 7.3 Entscheidungslogik: Text vs. Bild

```python
def classify_pdf(pdf_path: str) -> Literal["text", "image"]:
    """
    Heuristik: Wenn >80% der Seiten extrahierbaren Text haben
    UND der Text >50 Zeichen pro Seite enthält → text-basiert.
    Sonst → bild-basiert (gescanntes Dokument).
    """
```

### 7.4 XRechnung/ZUGFeRD: LLM-Bypass

Maschinenlesbare E-Rechnungsformate werden **ohne LLM** verarbeitet:

- **XRechnung** (reines XML): Direktes XML-Parsing mit lxml, Validierung gegen
  UBL/CII-Schema
- **ZUGFeRD** (PDF + eingebettetes XML): XML-Anhang aus PDF extrahieren,
  PDF-Anteil ignorieren, XML deterministisch parsen
- **Vorteil**: Keine Prompt-Injection möglich, deterministisch, schnell, kostenlos
- **Hinweis**: Ab 2025/2028 sind in Deutschland alle B2B-Rechnungen als
  E-Rechnungen Pflicht → dieser Pfad wird zunehmend der Normalfall

### 7.5 Weitere Kernprinzipien

1. **LLM hat KEINE Handlungsfähigkeit** — kein Tool-Use, kein Function-Calling.
   LLM liefert nur JSON-Daten zurück, alle Aktionen führt deterministischer
   Python-Code aus.
2. **Schema-erzwungene JSON-Ausgabe** — Pydantic v2 validiert jedes Feld der
   LLM-Antwort. Unerwartete Felder werden verworfen.
3. **Deterministische Nachverarbeitung** — alle ERPNext-Aktionen (PO/PI/PR
   erstellen) werden ausschließlich durch Python-Code ausgeführt, niemals durch
   LLM-generierte Befehle.
4. **Dual-Model-Vergleich** — Zwei verschiedene Modelle extrahieren unabhängig
   voneinander. Da verschiedene Modelle unterschiedlich auf Injection-Versuche
   reagieren, führt ein erfolgreicher Angriff auf ein Modell zu einer Abweichung
   → automatische Eskalation.
5. **Ausgabe-Validierung** — Extrahierte Beträge werden gegen Autorisierungs-
   limits geprüft, Lieferanten gegen die Stammdaten validiert.

---

## 8. Implementierungsphasen

### Phase 1: Fundament (MVP) — Eingangsrechnung + Dual-Model

| # | Aufgabe |
|---|---|
| 1.1 | Frappe App Grundstruktur (pyproject.toml, hooks.py, modules.txt) |
| 1.2 | LLM Provider Abstraktion (base.py + Anthropic + Ollama + OpenAI-kompatibel) |
| 1.3 | Pydantic Schemas für extrahierte Daten |
| 1.4 | Dual-Model Extraktions-Pipeline mit Vergleichslogik |
| 1.5 | Tiered Preprocessing (Format-Erkennung, PDF-Sanitisierung, XRechnung/ZUGFeRD-Parser) |
| 1.6 | Custom DocTypes (Purchase Document, Settings, Extraction Log) |
| 1.7 | Stammdaten-Matching (Supplier + Item) |
| 1.8 | ERPNext-Orchestrierung (PO + PI aus Eingangsrechnung) |
| 1.9 | Background Job Integration |
| 1.10 | Tests |

### Phase 2–4: *(wie zuvor)*

---

## 9. Kosten-Abschätzung (LLM)

Bei Dual-Model-Extraktion mit ~40 Prozessen/Monat:

| Szenario | Kosten/Monat |
|---|---|
| **Beide Cloud** (Claude + GPT-4o) | ~8–12 EUR |
| **Cloud + Lokal** (Claude + Ollama) | ~4–6 EUR |
| **Beide Lokal** (2x Ollama-Modelle) | ~0 EUR (nur Strom/Hardware) |

---

## 10. Erfolgskriterien Phase 1

- [ ] Eingangsrechnung (PDF) wird von 2 Modellen korrekt extrahiert
- [ ] Dual-Model-Vergleich erkennt Abweichungen zuverlässig
- [ ] Diskrepanzen werden korrekt eskaliert (Review-Status)
- [ ] Bestehende Lieferanten werden in >95% der Fälle korrekt zugeordnet
- [ ] Purchase Order + Purchase Invoice werden korrekt in ERPNext erstellt
- [ ] Beträge und MwSt. sind konsistent
- [ ] Prompt-Injection-Tests: Manipulation durch ein Modell führt zu Eskalation
- [ ] XRechnung/ZUGFeRD werden ohne LLM korrekt verarbeitet
- [ ] PDF-Sanitisierung entfernt versteckten Text, JS, Metadaten nachweislich
- [ ] Bild-basierte PDFs (Scans) werden korrekt erkannt und per Vision verarbeitet
- [ ] Verarbeitungszeit < 60 Sekunden pro Dokument (mit 2 Modellen)
