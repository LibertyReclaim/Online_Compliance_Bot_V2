"""North Carolina filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, select_dropdown_field, set_radio_field

NC_HOLDER_INFO_URL = "https://unclaimed.nccash.gov/app/holder-info"
NC_FORCED_REPORT_TYPE = "Annual Cash Report"
NC_HIPAA_LABEL = "Does this report include records that are subject to the HIPAA Privacy Rule"


class NorthCarolinaAutomationError(RuntimeError):
    """Raised when NC automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
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

    await page.goto(NC_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_nc_holder_info_page(page, record, errors)

    if errors:
        raise NorthCarolinaAutomationError("NC holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("NC debug -> clicking Next after NC holder info completed")
    await click_next(page, "after NC holder info")
    await _upload_naupa_file(page, naupa_path)


async def _fill_nc_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if field.key == "holder_tax_id" and not value:
            value = _as_string(record.get("fein"))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "NC"))

    print("NC debug -> Report Type forced to 'Annual Cash Report'")
    await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", NC_FORCED_REPORT_TYPE, "NC"))

    report_year = _as_string(record.get("report_year"))
    if not report_year:
        errors.append("report_year is required for 'Report Year'.")
    else:
        await _guarded(errors, "dropdown 'Report Year'", lambda: select_dropdown_field(page, "Report Year", report_year, "NC"))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    await _set_negative_report(page, negative, errors)

    if not negative:
        amount = _as_string(record.get("total_dollar_amount_remitted"))
        if not amount:
            errors.append("total_dollar_amount_remitted is required for 'Total Dollar Amount Remitted'.")
        else:
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "NC"))

        funds = _normalize_funds(_as_string(record.get("funds_remitted_via")) or "Check")
        print(f"NC debug -> Funds Remitted Via normalized to '{funds}'")
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", funds, "NC"))

    hipaa = _as_bool(record.get("includes_hipaa_records"))
    if hipaa is None:
        hipaa = False
    await _guarded(errors, f"radio '{NC_HIPAA_LABEL}'", lambda: set_radio_field(page, NC_HIPAA_LABEL, hipaa, "NC"))


async def _set_negative_report(page: Page, value: bool, errors: list[str]) -> None:
    try:
        await set_radio_field(page, "Is this a negative report?", value, "NC")
        return
    except Exception:
        pass

    await _guarded(
        errors,
        "radio 'This is a Negative Report'",
        lambda: set_radio_field(page, "This is a Negative Report", value, "NC"),
    )


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"NC warning -> NAUPA file does not exist: {file_path}; skipping upload.")
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
                print("NC debug -> NAUPA uploaded; clicking upload-page Next")
                await click_next(page, "after NC upload")
                await _wait_for_preview_or_signature(page)
                print("NC debug -> reached holder-preview; waiting for manual signature")
                print("NC finished - waiting for manual signature")
                return
            except Exception:
                continue
        await page.wait_for_timeout(800)

    raise NorthCarolinaAutomationError("Could not find NC upload file input.")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise NorthCarolinaAutomationError("NC upload did not reach holder-preview or signature prompt.")


async def click_next(page: Page, context: str) -> None:
    for candidate in (
        page.get_by_role("button", name="Next", exact=True),
        page.get_by_role("button", name="next"),
        page.locator("button:has-text('Next')"),
        page.locator("button:has-text('NEXT')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("input[type='submit'][value='NEXT']"),
    ):
        count = await candidate.count()
        for i in range(count):
            target = candidate.nth(i)
            if not await target.is_visible():
                continue
            if not await target.is_enabled():
                continue
            await target.click(timeout=10_000)
            await page.wait_for_timeout(1000)
            return
    raise NorthCarolinaAutomationError(f"Could not find a clickable Next control {context}.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _normalize_funds(raw_value: str) -> str:
    mapping = {
        "check": "Check",
        "ach": "ACH",
        "wire": "Wire",
        "online": "State Payment Portal",
    }
    normalized = _normalize(raw_value)
    return mapping.get(normalized, raw_value or "Check")


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
