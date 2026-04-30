"""Indiana filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import FieldResolutionError, fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

IN_HOLDER_INFO_URL = "https://www.indianaunclaimed.gov/app/holder-info"


class IndianaAutomationError(RuntimeError):
    """Raised when IN automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder FEIN", "holder_tax_id", required=True),
    _TextFieldSpec("State Tax ID", "state_tax_id"),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Contact Name", "contact_name", required=True),
    _TextFieldSpec("Contact Phone Number", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email Address", "email", required=True),
    _TextFieldSpec("Email Address Confirmation", "email", required=True),
    _TextFieldSpec("Address 1", "address_1", required=True),
    _TextFieldSpec("Address 2", "address_2"),
    _TextFieldSpec("Address 3", "address_3"),
    _TextFieldSpec("City", "city", required=True),
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

    print(f"IN debug -> navigating to {IN_HOLDER_INFO_URL}")
    await page.goto(IN_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await _wait_for_indiana_holder_form_ready(page)

    errors: list[str] = []
    await _fill_in_holder_info_page(page, record, errors)

    if errors:
        raise IndianaAutomationError("IN holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("IN debug -> clicking Next after IN holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)




async def _wait_for_indiana_holder_form_ready(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass

    await page.wait_for_selector("text=Enter Holder Information", timeout=30_000)
    await page.wait_for_selector("label:has-text('Holder Name'), text=Holder Name", timeout=30_000)
    await page.wait_for_selector("input:visible, select:visible, textarea:visible", timeout=30_000)
    print("IN debug -> holder form ready; starting fill")

async def _fill_in_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "IN"))

    holder_state = _as_string(record.get("state"))
    if not holder_state:
        errors.append("state is required for 'Holder section State'.")
    else:
        print("IN debug -> field='Holder section State' mapped_from='holder_information.state'")
        await _guarded(errors, "dropdown 'Holder section State'", lambda: _set_state_dropdown_in_section(page, "holder", holder_state))

    postal_code = _as_string(record.get("zip")) or _as_string(record.get("zip_code"))
    if not postal_code:
        errors.append("zip/zip_code is required for 'Postal Code'.")
    else:
        await _guarded(errors, "text 'Postal Code'", lambda: fill_text_field(page, "Postal Code", postal_code, "IN"))

    report_type_raw = _as_string(record.get("report_type"))
    if not report_type_raw:
        errors.append("report_type is required for 'Report Type'.")
    else:
        report_type = _normalize_in_report_type(report_type_raw)
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "IN"))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_or_accept_disabled_report_year(page, report_year))

    report_info_state = _as_string(record.get("state")) or holder_state
    if report_info_state:
        print("IN debug -> field='Report Info State' mapped_from='payment_file.state or holder_information.state'")
        await _guarded(errors, "dropdown 'Report Info State'", lambda: _set_state_dropdown_in_section(page, "report", report_info_state))

    negative_report = _as_bool(record.get("negative_report"))
    if negative_report is None:
        negative_report = False
    await _guarded(errors, "radio 'This is a Negative (Zero) Report'", lambda: set_radio_field(page, "This is a Negative (Zero) Report", negative_report, "IN"))

    aggregate_cash_total = _as_string(record.get("aggregate_cash_total")) or _as_string(record.get("amount_to_remit"))
    total_shares = _as_string(record.get("total_shares")) or "0"
    total_props = _as_string(record.get("total_number_of_items_reported")) or "1"
    boxes = _as_string(record.get("safe_deposit_boxes_reported")) or "0"
    amount_to_remit = _as_string(record.get("amount_to_remit"))

    if not aggregate_cash_total:
        errors.append("aggregate_cash_total/amount_to_remit is required for 'Total Amount of Cash Reported'.")
    else:
        print("IN debug -> field='Total Amount of Cash Reported' mapped_from='aggregate_cash_total'")
        await _guarded(errors, "text 'Total Amount of Cash Reported'", lambda: fill_text_field(page, "Total Amount of Cash Reported", aggregate_cash_total, "IN"))

    print("IN debug -> field='Total Number of Shares Reported' mapped_from='total_shares'")
    await _guarded(errors, "text 'Total Number of Shares Reported'", lambda: fill_text_field(page, "Total Number of Shares Reported", total_shares, "IN"))

    print("IN debug -> field='Total Number of Properties Reported' mapped_from='total_number_of_items_reported'")
    await _guarded(errors, "text 'Total Number of Properties Reported'", lambda: fill_text_field(page, "Total Number of Properties Reported", total_props, "IN"))

    print("IN debug -> field='Total Number of Safe Deposit Boxes Reported' mapped_from='safe_deposit_boxes_reported'")
    await _guarded(errors, "text 'Total Number of Safe Deposit Boxes Reported'", lambda: fill_text_field(page, "Total Number of Safe Deposit Boxes Reported", boxes, "IN"))

    if not amount_to_remit:
        errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
    else:
        print("IN debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
        await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount_to_remit, "IN"))

    funds = _as_string(record.get("funds_remitted_via"))
    if not funds:
        errors.append("funds_remitted_via is required for 'Funds Remitted Via'.")
    else:
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", _normalize_funds_remitted_via(funds), "IN"))

    hipaa = _as_bool(record.get("includes_hipaa_records"))
    if hipaa is None:
        hipaa = False
    await _guarded(errors, "radio 'Does this report include records that are subject to the HIPAA Privacy Rule?'", lambda: set_radio_field(page, "Does this report include records that are subject to the HIPAA Privacy Rule?", hipaa, "IN"))


async def _set_or_accept_disabled_report_year(page: Page, expected_year: str) -> None:
    row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "IN")
    control = row.locator("select").first
    if await control.is_enabled():
        await select_dropdown_field(page, "Report Year", expected_year, "IN")
        return

    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))
    if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
        return
    if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
        return
    raise IndianaAutomationError("IN Report Year is disabled but blank/unselected.")


async def _set_state_dropdown_in_section(page: Page, section: str, value: str) -> None:
    rows = page.locator("xpath=//*[self::div or self::section or self::td or self::tr][contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), 'state')]")
    count = await rows.count()
    matched = None
    for i in range(min(count, 20)):
        row = rows.nth(i)
        try:
            if not await row.is_visible():
                continue
        except Exception:
            continue
        text = _normalize(await row.inner_text())
        selects = row.locator("select")
        if await selects.count() != 1:
            continue
        if section == "holder" and ("city" in text or "postal" in text or "address" in text):
            matched = row
            break
        if section == "report" and ("report type" in text or "report year" in text or "report info" in text):
            matched = row
            break
    if matched is None:
        # fallback: first or second state dropdown by page order
        state_selects = page.locator("xpath=//select[ancestor::*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), 'state')]]")
        if await state_selects.count() < 2:
            raise IndianaAutomationError(f"IN could not locate {section} State dropdown.")
        target = state_selects.nth(0 if section == "holder" else 1)
        await _select_control_with_fallback(target, value)
        return

    control = matched.locator("select").first
    await _select_control_with_fallback(control, value)


async def _select_control_with_fallback(control: Any, expected_value: str) -> None:
    try:
        await control.select_option(label=expected_value)
        return
    except Exception:
        pass
    options = await control.evaluate("el => Array.from(el.options).map(o => ({text:(o.textContent||'').trim(), value:(o.value||'').trim()}))")
    target = _normalize(expected_value)
    for option in options:
        t = _normalize(str(option.get("text", "")))
        v = _normalize(str(option.get("value", "")))
        if t == target or v == target or (target and target in t):
            await control.select_option(value=str(option.get("value", "")))
            return
    raise IndianaAutomationError(f"IN failed selecting dropdown value '{expected_value}'.")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"IN warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    for selector in ["input[type='file']", "input[type='file']:visible", "input[accept]", "input[type='file'][accept]"]:
        locator = page.locator(selector)
        if await locator.count() <= 0:
            continue
        try:
            await locator.first.set_input_files(str(file_path))
            await page.wait_for_timeout(1000)
            return
        except Exception:
            continue

    raise IndianaAutomationError("Could not find IN upload file input.")


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
    raise IndianaAutomationError("Could not find a clickable 'Next' control on IN page.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _normalize_in_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
        "negative": "Negative Report",
        "negative report": "Negative Report",
    }
    return mapping.get(_normalize(raw_value), raw_value)


def _normalize_funds_remitted_via(raw_value: str) -> str:
    mapping = {"wire": "Wire", "check": "Check", "ach": "ACH", "online": "Online", "electronic": "Online"}
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
