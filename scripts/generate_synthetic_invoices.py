"""
Generate 30 synthetic labeled invoice PDFs + JSON ground-truth labels for the eval harness.

Usage:
    pip install fpdf2 --break-system-packages
    python scripts/generate_synthetic_invoices.py

Outputs:
    tests/fixtures/invoice_001.pdf  ...  tests/fixtures/invoice_030.pdf
    tests/fixtures/labels.json
"""

import json
import random
from decimal import Decimal
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

random.seed(42)

VENDORS = [
    ("Acme Supplies Inc.", "123 Main St, Springfield, IL 62701"),
    ("Global Tech Ltd.", "45 Innovation Way, San Jose, CA 95110"),
    ("Office Essentials Co.", "78 Commerce Blvd, Austin, TX 73301"),
    ("Blue Ridge Consulting", "9 Summit Rd, Denver, CO 80201"),
    ("Pacific Rim Imports", "200 Harbor Dr, Seattle, WA 98101"),
    ("DataStream Analytics", "1 Data Centre Pl, NYC, NY 10001"),
    ("Bright Future Solar", "55 Green Ave, Phoenix, AZ 85001"),
    ("North Star Logistics", "33 Freight Ln, Chicago, IL 60601"),
    ("Summit Legal Group", "12 Courthouse Sq, Boston, MA 02101"),
    ("Apex Manufacturing", "77 Industrial Park, Detroit, MI 48201"),
]

LINE_ITEMS_POOL = [
    ("Software License", 1, 2500.00),
    ("Consulting Hours", 8, 150.00),
    ("Server Hardware", 2, 4200.00),
    ("Cloud Storage (annual)", 1, 1200.00),
    ("Training Materials", 5, 89.99),
    ("Network Equipment", 3, 750.00),
    ("Support Contract", 1, 3600.00),
    ("Office Supplies", 10, 45.00),
    ("Shipping & Handling", 1, 125.00),
    ("Installation Service", 4, 200.00),
]

def make_invoice(idx: int) -> dict:
    vendor_name, vendor_addr = random.choice(VENDORS)
    year = 2025
    month = random.randint(1, 6)
    day = random.randint(1, 28)
    invoice_date = f"{year}-{month:02d}-{day:02d}"
    due_day = min(day + 30, 28)
    due_date = f"{year}-{month:02d}-{due_day:02d}" if random.random() > 0.1 else None

    num_items = random.randint(1, 3)
    items = random.sample(LINE_ITEMS_POOL, num_items)
    line_items = []
    subtotal = Decimal("0")
    for desc, qty, unit_price in items:
        qty_v = random.randint(1, qty)
        up = Decimal(str(unit_price))
        total = (Decimal(str(qty_v)) * up).quantize(Decimal("0.01"))
        subtotal += total
        line_items.append({
            "description": desc,
            "quantity": qty_v,
            "unit_price": float(up),
            "total": float(total),
        })

    tax_rate = Decimal("0.08") if random.random() > 0.3 else Decimal("0")
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = subtotal + tax

    has_po = random.random() > 0.25
    po = f"PO-{random.randint(1000,9999)}" if has_po else None
    inv_num = f"INV-{year}-{idx:03d}"

    return {
        "vendor_name": vendor_name,
        "vendor_address": vendor_addr,
        "invoice_number": inv_num,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "currency": "USD",
        "subtotal": float(subtotal),
        "tax": float(tax),
        "total": float(total),
        "line_items": line_items,
        "purchase_order_number": po,
        "notes": None,
    }


def render_pdf(invoice: dict, path: Path) -> None:
    """Render a simple invoice PDF using fpdf2."""
    try:
        from fpdf import FPDF
    except ImportError:
        print("fpdf2 not installed. Run: pip install fpdf2 --break-system-packages")
        # Write a minimal fake PDF so path exists
        path.write_bytes(b"%PDF-1.4 synthetic invoice " + invoice["invoice_number"].encode())
        return

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "INVOICE", ln=True, align="C")
    pdf.ln(5)

    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Invoice #: {invoice['invoice_number']}", ln=True)
    pdf.cell(0, 7, f"Date: {invoice['invoice_date']}", ln=True)
    if invoice["due_date"]:
        pdf.cell(0, 7, f"Due Date: {invoice['due_date']}", ln=True)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, f"Vendor: {invoice['vendor_name']}", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, invoice.get("vendor_address", ""), ln=True)
    pdf.ln(3)

    if invoice.get("purchase_order_number"):
        pdf.cell(0, 7, f"PO #: {invoice['purchase_order_number']}", ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(80, 7, "Description", border=1)
    pdf.cell(25, 7, "Qty", border=1, align="C")
    pdf.cell(35, 7, "Unit Price", border=1, align="R")
    pdf.cell(35, 7, "Total", border=1, align="R", ln=True)

    pdf.set_font("Helvetica", "", 10)
    for item in invoice["line_items"]:
        pdf.cell(80, 6, item["description"][:40], border=1)
        pdf.cell(25, 6, str(item["quantity"]), border=1, align="C")
        pdf.cell(35, 6, f"${item['unit_price']:,.2f}", border=1, align="R")
        pdf.cell(35, 6, f"${item['total']:,.2f}", border=1, align="R", ln=True)

    pdf.ln(3)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(145, 7, "Subtotal:", align="R")
    pdf.cell(30, 7, f"${invoice['subtotal']:,.2f}", align="R", ln=True)
    pdf.cell(145, 7, "Tax:", align="R")
    pdf.cell(30, 7, f"${invoice['tax']:,.2f}", align="R", ln=True)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(145, 8, "TOTAL (USD):", align="R")
    pdf.cell(30, 8, f"${invoice['total']:,.2f}", align="R", ln=True)

    pdf.output(str(path))


def main():
    labels = []
    for i in range(1, 31):
        inv = make_invoice(i)
        pdf_path = FIXTURES_DIR / f"invoice_{i:03d}.pdf"
        render_pdf(inv, pdf_path)
        labels.append({"pdf_path": str(pdf_path), "label": inv})
        print(f"Generated invoice_{i:03d}.pdf — {inv['vendor_name']} ${inv['total']:,.2f}")

    labels_path = FIXTURES_DIR / "labels.json"
    labels_path.write_text(json.dumps(labels, indent=2))
    print(f"\n✓ Labels written to {labels_path}")
    print(f"✓ {len(labels)} synthetic invoices ready for eval")


if __name__ == "__main__":
    main()
