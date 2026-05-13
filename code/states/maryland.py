"""Maryland filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, select_dropdown_field, set_radio_field

MD_HOLDER_INFO_URL = "https://claimitmd.gov/app/holder-info"


class MarylandAutomationError(RuntimeError):
    """Raised when MD automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Contact Name", "contact_name", required=True),
    _TextFieldSpec("Contact Phone Number", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email Address", "email", required=True),
    _TextFieldSpec("Email Address Confirmation", "email", required=True),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(MD_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_md_holder_info_page(page, record, errors)

    if errors:
        raise MarylandAutomationError("MD holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("MD debug -> clicking Next after MD holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)


async def _fill_md_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    fein_value = _as_string(record.get("fein")) or _as_string(record.get("holder_tax_id"))
    if not fein_value:
        errors.append("fein or holder_tax_id is required for 'Holder Tax ID or FEIN'.")
    else:
        print("MD debug -> field='Holder Tax ID or FEIN' mapped_from='fein or holder_tax_id'")
        await _guarded(errors, "text 'Holder Tax ID or FEIN'", lambda: fill_text_field(page, "Holder Tax ID or FEIN", fein_value, "MD"))

    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "MD"))

    report_type = _normalize_report_type(_as_string(record.get("report_type")))
    if not report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "MD"))

    report_year = _as_string(record.get("report_year"))
    if not report_year:
        errors.append("report_year is required for 'Report Year'.")
    else:
        await _guarded(errors, "dropdown 'Report Year'", lambda: select_dropdown_field(page, "Report Year", report_year, "MD"))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    await _guarded(errors, "radio 'Is this a negative report?'", lambda: set_radio_field(page, "Is this a negative report?", negative, "MD"))

    if negative:
        await _set_hipaa_if_available(page, record, errors)
        return

    amount = _as_string(record.get("amount_to_remit"))
    if not amount:
        errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
    else:
        print("MD debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
        await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "MD"))

    shares = _as_string(record.get("total_shares")) or "0"
    print("MD debug -> field='Total Number of Shares Remitted' mapped_from='total_shares'")
    await _guarded(errors, "text 'Total Number of Shares Remitted'", lambda: fill_text_field(page, "Total Number of Shares Remitted", shares, "MD"))

    funds_normalized = _normalize_funds(_as_string(record.get("funds_remitted_via")) or "Check")
    print(f"MD debug -> field='Funds Remitted Via' normalized='{funds_normalized}'")
    await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", funds_normalized, "MD"))

    await _set_hipaa_if_available(page, record, errors)


async def _set_hipaa_if_available(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    hipaa = _as_bool(record.get("includes_hipaa_records"))
    if hipaa is None:
        hipaa = False
    rendered = "Yes" if hipaa else "No"
    print(f"MD debug -> field='HIPAA Privacy Rule' value='{rendered}'")
    await _guarded(
        errors,
        "radio 'Does this report include records that are subject to the HIPAA Privacy Rule?'",
        lambda: set_radio_field(page, "Does this report include records that are subject to the HIPAA Privacy Rule?", hipaa, "MD"),
    )


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"MD warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    selectors = ["input[type='file']", "input[type='file']:visible", "input[accept]", "input[type='file'][accept]"]
    for _ in range(3):
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            try:
                target = locator.first
                await target.set_input_files(str(file_path))
                await page.wait_for_timeout(1500)
                print("MD debug -> NAUPA uploaded; clicking upload-page Next")
                await _click_upload_page_next(page, target)
                await _wait_for_preview_or_signature(page)
                print("MD debug -> reached holder-preview; waiting for manual signature")
                print("MD finished - waiting for manual signature")
                return
            except Exception:
                continue
        await page.wait_for_timeout(800)

    raise MarylandAutomationError("Could not find MD upload file input.")


async def _click_upload_page_next(page: Page, upload_input: Any) -> None:
    next_candidates: list[Any] = []

    for candidate in (
        page.get_by_role("button", name="next"),
        page.locator("button:has-text('NEXT')"),
    ):
        count = await candidate.count()
        for i in range(count):
            btn = candidate.nth(i)
            if not await btn.is_visible() or not await btn.is_enabled():
                continue
            next_candidates.append(btn)

    if not next_candidates:
        raise MarylandAutomationError("Could not find enabled visible upload-page NEXT button.")

    upload_box = await upload_input.bounding_box()
    if upload_box is not None:
        upload_center_y = float(upload_box["y"]) + float(upload_box["height"]) / 2.0
        best_btn = None
        best_distance = None
        for btn in next_candidates:
            btn_box = await btn.bounding_box()
            if btn_box is None:
                continue
            btn_center_y = float(btn_box["y"]) + float(btn_box["height"]) / 2.0
            distance = abs(btn_center_y - upload_center_y)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_btn = btn
        if best_btn is not None:
            await best_btn.click(timeout=10_000)
            return

    await next_candidates[-1].click(timeout=10_000)


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise MarylandAutomationError("MD upload did not reach holder-preview or signature prompt.")


async def _click_next(page: Page) -> None:
    for candidate in (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
    ):
        if await candidate.count() <= 0:
            continue
        target = candidate.first
        if not await target.is_enabled():
            continue
        await target.click(timeout=10_000)
        await page.wait_for_timeout(1000)
        return
    raise MarylandAutomationError("Could not find a clickable 'Next' control on MD page.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _normalize_funds(raw_value: str) -> str:
    mapping = {
        "wire": "Wire Transfer",
        "wire transfer": "Wire Transfer",
        "ach": "ACH Direct Debit",
        "ach direct debit": "ACH Direct Debit",
        "check": "Check",
        "electronic": "ACH Direct Debit",
        "online": "ACH Direct Debit",
    }
    normalized = _normalize(raw_value)
    if not normalized:
        return "Check"
    return mapping.get(normalized, raw_value)


def _normalize_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
    }
    return mapping.get(_normalize(raw_value), raw_value)


def _merge_records(holder_row: Dict[str, Any], payment_row: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(holder_row)
    merged.update(payment_row)
    return merged


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    rendered = str(value).strip()
    return "" if rendered.lower() == "nan" else rendered


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"", "nan"}:
        return None
    if normalized in {"yes", "y", "true", "1"}:
        return True
    if normalized in {"no", "n", "false", "0"}:
        return False
    return None


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace(":", "").strip().lower().split())
