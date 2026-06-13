"""
Extraction eval harness — Milestone 6.

Measures extraction accuracy against 30 synthetic labeled invoices.
Reports field-level accuracy so you can track model regressions.

Generate fixtures first:
    python scripts/generate_synthetic_invoices.py

Run full eval (requires API key + fixtures):
    pytest tests/evals/test_extraction_eval.py -v --tb=short

Run decision-only eval (no LLM, always runs in CI):
    pytest tests/evals/test_extraction_eval.py::test_risk_decision_accuracy -v
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
LABELS_FILE = FIXTURES_DIR / "labels.json"
ACCURACY_THRESHOLD = 0.80

EVAL_FIELDS = [
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "total",
    "currency",
    "purchase_order_number",
]


def load_labels() -> list[dict]:
    if not LABELS_FILE.exists():
        return []
    return json.loads(LABELS_FILE.read_text())


def _compare_field(predicted, expected) -> bool:
    if predicted is None and expected is None:
        return True
    if predicted is None or expected is None:
        return False
    return str(predicted).strip().lower() == str(expected).strip().lower()


@pytest.mark.parametrize("field", EVAL_FIELDS)
def test_extraction_field_accuracy(field: str):
    """
    For each field, measure what fraction of the 30 labeled invoices
    the extractor gets right. Requires the API key + fixture PDFs.
    """
    labels = load_labels()
    if not labels:
        pytest.skip("No fixtures found. Run: python scripts/generate_synthetic_invoices.py")

    from app.services.extraction import extract_invoice

    correct = 0
    total = 0
    failures = []

    for item in labels:
        pdf_path = item.get("pdf_path", "")
        label = item["label"]
        if not Path(pdf_path).exists():
            continue
        total += 1
        try:
            extracted = extract_invoice(pdf_path)
            predicted = getattr(extracted, field, None)
            if isinstance(predicted, Decimal):
                predicted = str(predicted)
            expected = label.get(field)
            match = _compare_field(predicted, expected)
            if match:
                correct += 1
            else:
                failures.append({
                    "file": Path(pdf_path).name,
                    "expected": expected,
                    "got": predicted,
                })
        except Exception as e:
            failures.append({"file": Path(pdf_path).name, "error": str(e)})

    if total == 0:
        pytest.skip(f"No valid PDF fixtures found in {FIXTURES_DIR}")

    accuracy = correct / total
    if failures:
        print(f"\n  Failures for '{field}':")
        for f in failures[:5]:
            print(f"    {f}")

    print(f"\n  {field}: {correct}/{total} = {accuracy:.1%}")
    assert accuracy >= ACCURACY_THRESHOLD, (
        f"Field '{field}' accuracy {accuracy:.1%} below threshold {ACCURACY_THRESHOLD:.0%}"
    )


def test_risk_decision_accuracy():
    """
    Decision policy accuracy — no LLM needed, runs in CI.

    Tests that the risk policy engine correctly classifies invoices into
    auto_approve / needs_review / reject based on known inputs.
    """
    from app.graph.nodes import decide_node

    # (state, expected_decision, description)
    test_cases = [
        (
            {
                "extracted_data": {
                    "total": 450,
                    "vendor_name": "Acme Supplies Inc.",
                    "purchase_order_number": "PO-001",
                },
                "extraction_error": None,
            },
            "auto_approve",
            "Low amount + known-ish vendor + PO present → auto approve",
        ),
        (
            {
                "extracted_data": {
                    "total": 50000,
                    "vendor_name": "BigCorp",
                    "purchase_order_number": "PO-002",
                },
                "extraction_error": None,
            },
            "needs_review",
            "Amount above $10k threshold → needs review",
        ),
        (
            {
                "extracted_data": {
                    "total": 1500,
                    "vendor_name": "Totally New Vendor",
                    "purchase_order_number": None,
                },
                "extraction_error": None,
            },
            "needs_review",
            "Missing PO + new vendor → needs review",
        ),
        (
            {
                "extracted_data": None,
                "extraction_error": "LLM returned invalid JSON",
            },
            "reject",
            "Extraction failed → reject",
        ),
        (
            {
                "extracted_data": {
                    "total": 200,
                    "vendor_name": "Small Vendor",
                    "purchase_order_number": "PO-999",
                },
                "extraction_error": None,
            },
            "auto_approve",
            "Small amount + PO present → auto approve",
        ),
    ]

    correct = 0
    for state, expected, desc in test_cases:
        result = decide_node(state)
        actual = result["decision"]
        match = actual == expected
        status = "✓" if match else "✗"
        print(f"\n  {status} {desc}")
        print(f"    expected={expected}, got={actual}")
        if match:
            correct += 1

    accuracy = correct / len(test_cases)
    print(f"\n  Decision accuracy: {correct}/{len(test_cases)} = {accuracy:.0%}")
    assert accuracy >= ACCURACY_THRESHOLD, (
        f"Decision accuracy {accuracy:.0%} below threshold {ACCURACY_THRESHOLD:.0%}"
    )


def test_pydantic_extraction_schema_validation():
    """
    Unit test: InvoiceExtracted validates correct data and rejects bad data.
    No LLM or DB needed.
    """
    from app.schemas.invoice import InvoiceExtracted, LineItem
    from pydantic import ValidationError
    import pytest

    # Valid invoice
    valid = InvoiceExtracted(
        vendor_name="Test Vendor",
        invoice_number="INV-001",
        invoice_date="2025-01-15",
        currency="USD",
        subtotal=100.00,
        tax=8.00,
        total=108.00,
        line_items=[
            LineItem(description="Widget", quantity=2, unit_price=50.00, total=100.00)
        ],
    )
    assert valid.total == pytest.approx(108.00, abs=0.01)

    # Total mismatch should raise
    with pytest.raises(ValidationError):
        InvoiceExtracted(
            vendor_name="Test Vendor",
            invoice_number="INV-002",
            invoice_date="2025-01-15",
            subtotal=100.00,
            tax=0.00,
            total=999.00,  # wrong
        )

    # Line item total mismatch should raise
    with pytest.raises(ValidationError):
        LineItem(description="Widget", quantity=2, unit_price=50.00, total=999.00)
