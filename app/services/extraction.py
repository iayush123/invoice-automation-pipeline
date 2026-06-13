"""
Invoice extraction service — LLM-powered PDF → structured data.

Architecture decisions to know for an interview:
──────────────────────────────────────────────────
1. WHY VISION INSTEAD OF TEXT-EXTRACTION?
   PDFs can be scanned images with no extractable text layer. A vision-capable
   LLM handles both structured PDFs and scanned images uniformly, at the cost
   of slightly higher latency and token usage vs. extracting text first with
   pdfplumber/pypdf.

2. WHY RETRY + VALIDATION LOOP?
   LLMs are non-deterministic. On ~5-10% of calls the JSON will be
   malformed, a required field will be missing, or line-item totals won't
   add up. We retry up to MAX_RETRIES times, feeding the Pydantic error
   back to the model so it can self-correct. After MAX_RETRIES we raise
   and the caller can dead-letter the invoice.

3. WHY NOT FUNCTION-CALLING / TOOL USE?
   We could use Anthropic's tool use or OpenAI function calling to get
   structured output. We use a JSON-in-prompt approach here for clarity —
   in production, switch to structured output / tool use for reliability.
"""

import base64
import json
import re
from pathlib import Path

import structlog
from pydantic import ValidationError

from app.core.config import settings
from app.core.llm import get_llm_client
from app.schemas.invoice import InvoiceExtracted

log = structlog.get_logger()

MAX_RETRIES = 3

EXTRACTION_SYSTEM_PROMPT = """
You are a precise invoice data extractor. Given an invoice image or PDF page,
extract the following fields and return ONLY a valid JSON object — no markdown,
no explanation, no code fences. Return only the raw JSON.

Required JSON structure:
{
  "vendor_name": "string",
  "vendor_address": "string or null",
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD or null",
  "currency": "3-letter ISO code, e.g. USD",
  "subtotal": number,
  "tax": number,
  "total": number,
  "line_items": [
    {
      "description": "string",
      "quantity": number,
      "unit_price": number,
      "total": number
    }
  ],
  "purchase_order_number": "string or null",
  "notes": "string or null"
}

Rules:
- All monetary amounts must be plain numbers (no currency symbols).
- Dates must be ISO 8601: YYYY-MM-DD.
- If a field is not present on the invoice, use null.
- total = subtotal + tax (within 0.05 tolerance).
- Each line_item.total = quantity × unit_price (within 0.02 tolerance).
""".strip()

CORRECTION_PROMPT_TEMPLATE = """
Your previous response failed validation with this error:
{error}

Original invoice is the same. Please fix the JSON and return ONLY the corrected
JSON object. Do not include any explanation or code fences.
"""


def _pdf_to_base64_images(file_path: str) -> list[str]:
    """Convert a PDF file to a list of base64-encoded PNG images (one per page)."""
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(file_path, dpi=150, fmt="PNG")
        results = []
        for img in images[:3]:  # cap at 3 pages — invoices are rarely longer
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            results.append(base64.standard_b64encode(buf.getvalue()).decode())
        return results
    except Exception as e:
        log.warning("pdf_to_image_failed", error=str(e), path=file_path)
        return []


def _extract_json_from_response(text: str) -> dict:
    """
    Parse JSON from LLM response text, handling common LLM quirks:
    - Wrapped in ```json ... ``` code fences
    - Leading/trailing whitespace or explanation text
    """
    text = text.strip()
    # Strip code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def extract_invoice(file_path: str) -> InvoiceExtracted:
    """
    Extract structured data from an invoice file.

    Raises:
        ValueError: if extraction fails after MAX_RETRIES attempts.
    """
    client = get_llm_client()
    path = Path(file_path)

    # Prepare the image content for the vision call
    if path.suffix.lower() == ".pdf":
        b64_images = _pdf_to_base64_images(file_path)
        if not b64_images:
            # Fall back: read raw bytes as a single image attempt
            b64_images = [base64.standard_b64encode(path.read_bytes()).decode()]
    else:
        # Direct image file
        b64_images = [base64.standard_b64encode(path.read_bytes()).decode()]

    last_error: str | None = None
    messages: list[dict] = []

    for attempt in range(1, MAX_RETRIES + 1):
        log.info("extraction_attempt", attempt=attempt, path=file_path)

        if attempt == 1:
            # First attempt: full prompt with image
            if settings.llm_provider == "anthropic":
                content = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_images[0],
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract all invoice data from this image and return only valid JSON as specified.",
                    },
                ]
                messages = [{"role": "user", "content": content}]
            else:
                # OpenAI format
                content = [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_images[0]}"},
                    },
                    {"type": "text", "text": "Extract all invoice data and return only valid JSON."},
                ]
                messages = [{"role": "user", "content": content}]
        else:
            # Retry: append the correction prompt (no need to re-send the image)
            messages.append({
                "role": "assistant",
                "content": raw_response,
            })
            messages.append({
                "role": "user",
                "content": CORRECTION_PROMPT_TEMPLATE.format(error=last_error),
            })

        try:
            if settings.llm_provider == "anthropic":
                response = client.messages.create(
                    model=settings.llm_model,
                    max_tokens=2048,
                    system=EXTRACTION_SYSTEM_PROMPT,
                    messages=messages,
                )
                raw_response = response.content[0].text
            else:
                from openai.types.chat import ChatCompletionSystemMessageParam
                all_messages = [
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    *messages,
                ]
                response = client.chat.completions.create(
                    model=settings.llm_model,
                    messages=all_messages,
                    max_tokens=2048,
                )
                raw_response = response.choices[0].message.content

            parsed_dict = _extract_json_from_response(raw_response)
            extracted = InvoiceExtracted(**parsed_dict)

            log.info("extraction_success", attempt=attempt, vendor=extracted.vendor_name)
            return extracted

        except (json.JSONDecodeError, ValidationError, ValueError, KeyError) as e:
            last_error = str(e)
            log.warning("extraction_attempt_failed", attempt=attempt, error=last_error)

    raise ValueError(
        f"Invoice extraction failed after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )
