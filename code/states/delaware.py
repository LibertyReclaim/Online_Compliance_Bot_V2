"""Delaware filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

DE_HOLDER_INFO_URL = "https://unclaimedproperty.delaware.gov/app/holder-info"


class DelawareAutomationError(RuntimeError):
    """Raised when DE automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_REQUIRED_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Contact", "contact_name", required=True),
    _TextFieldSpec("Contact Phone No.", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email", required=True),
    _TextFieldSpec("Address 1", "address_1", required=True),
    _TextFieldSpec("Address 2", "address_2"),
    _TextFieldSpec("Address 3", "address_3"),
    _TextFieldSpec("City", "city", required=True),
    _TextFieldSpec("Contact Fax", "contact_fax"),
    _TextFieldSpec("Previous Business Name", "previous_business_name"),
    _TextFieldSpec("Previous Business Name (if merger or acquisition)", "previous_business_name_merger"),
    _TextFieldSpec("Previous FEIN", "previous_fein"),
)

_OPTIONAL_AGENT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Reporting Agent Organization Name", "reporting_agent_organization_name"),
    _TextFieldSpec("Reporting Agent Contact Name", "reporting_agent_contact_name"),
    _TextFieldSpec("Reporting Agent Contact Phone", "reporting_agent_contact_phone"),
    _TextFieldSpec("Reporting Agent Contact Email", "reporting_agent_contact_email"),
)


async def run(page: Page, holder_row: Dict[str, Any], payment_row: Dict[str, Any], naupa_file_path: str | Path, *, wait_after_navigation_ms: int = 1500) -> None:
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(DE_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_de_holder_info_page(page, record, errors)

    if errors:
        raise DelawareAutomationError("DE holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("DE debug -> clicking Next after DE holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)


async def _fill_de_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    tax_id = _as_string(record.get("holder_tax_id")) or _as_string(record.get("fein"))
    if not tax_id:
        errors.append("holder_tax_id or fein is required for 'Holder Tax ID'.")
    else:
        await _guarded(errors, "text 'Holder Tax ID'", lambda: fill_text_field(page, "Holder Tax ID", tax_id, "DE"))

    for field in _REQUIRED_TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "DE"))

    for field in _OPTIONAL_AGENT_FIELDS:
        value = _as_string(record.get(field.key))
        if value:
            await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "DE"))

    state_value = _as_string(record.get("state"))
    if not state_value:
        errors.append("state is required for 'State'.")
    else:
        await _guarded(errors, "dropdown 'State'", lambda: select_dropdown_field(page, "State", state_value, "DE"))

    zip_code = _as_string(record.get("zip")) or _as_string(record.get("zip_code"))
    if not zip_code:
        errors.append("zip or zip_code is required for 'Postal Code'.")
    else:
        await _guarded(errors, "text 'Postal Code'", lambda: fill_text_field(page, "Postal Code", zip_code, "DE"))

    state_incorp = _as_string(record.get("state_incorporation")) or _as_string(record.get("state_of_incorporation"))
    if not state_incorp:
        errors.append("state_incorporation/state_of_incorporation is required for 'State of Incorporation'.")
    else:
        print("DE debug -> field='State of Incorporation' mapped_from='state_incorporation/state_of_incorporation'")
        await _guarded(errors, "dropdown 'State of Incorporation'", lambda: select_dropdown_field(page, "State of Incorporation", state_incorp, "DE"))

    mm, dd, yyyy = _resolve_incorporation_date_parts(record)
    if mm and dd and yyyy:
        print(f"DE debug -> Date of Incorporation split columns MM='{mm}' DD='{dd}' YYYY='{yyyy}'")
        print("DE debug -> using custom Date of Incorporation dropdown handler")
        try:
            await _set_incorporation_date_parts(page, mm, dd, yyyy)
            print(f"DE debug -> selected Date of Incorporation MM='{mm}' DD='{dd}' YYYY='{yyyy}'")
        except Exception as exc:
            errors.append(f"Failed to set custom Date of Incorporation dropdowns: {exc}")
    else:
        print("DE debug -> Date of Incorporation split columns missing; leaving blank (optional unless required by site)")

    report_type = _normalize_report_type(_as_string(record.get("report_type")))
    if not report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "DE"))

    report_year = _as_string(record.get("report_year"))
    if not report_year:
        errors.append("report_year is required for 'Report Year'.")
    else:
        await _guarded(errors, "dropdown 'Report Year'", lambda: select_dropdown_field(page, "Report Year", report_year, "DE"))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: set_radio_field(page, "This is a Negative Report", negative, "DE"))

    if not negative:
        cash_total = _as_string(record.get("aggregate_cash_total")) or _as_string(record.get("amount_to_remit"))
        if not cash_total:
            errors.append("aggregate_cash_total/amount_to_remit is required for 'Total Amount of Cash Reported'.")
        else:
            print("DE debug -> field='Total Amount of Cash Reported' mapped_from='aggregate_cash_total'")
            await _guarded(errors, "text 'Total Amount of Cash Reported'", lambda: fill_text_field(page, "Total Amount of Cash Reported", cash_total, "DE"))

        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print("DE debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "DE"))

        shares = _as_string(record.get("total_shares")) or "0"
        await _guarded(errors, "text 'Total Number of Shares Reported'", lambda: fill_text_field(page, "Total Number of Shares Reported", shares, "DE"))

        owners = _as_string(record.get("number_of_owners")) or _as_string(record.get("total_number_of_owners_reported")) or "1"
        print("DE debug -> field='Total Number of Owners Reported' mapped_from='number_of_owners'")
        await _guarded(errors, "text 'Total Number of Owners Reported'", lambda: fill_text_field(page, "Total Number of Owners Reported", owners, "DE"))

        props = _as_string(record.get("total_items_reported")) or _as_string(record.get("total_number_of_properties_reported")) or _as_string(record.get("total_number_of_items_reported")) or "1"
        print("DE debug -> field='Total Number of Properties Reported' mapped_from='total_items_reported'")
        await _guarded(errors, "text 'Total Number of Properties Reported'", lambda: fill_text_field(page, "Total Number of Properties Reported", props, "DE"))

        remit = _normalize_remit_method(_as_string(record.get("funds_remitted_via")) or "Check")
        print(f"DE debug -> field='How are you Remitting Property' normalized='{remit}'")
        await _guarded(errors, "dropdown 'How are you Remitting Property'", lambda: select_dropdown_field(page, "How are you Remitting Property", remit, "DE"))

    hipaa = _as_bool(record.get("hipaa_privacy_rule"))
    if hipaa is None:
        hipaa = _as_bool(record.get("includes_hipaa_records"))
    if hipaa is None:
        hipaa = False
    rendered = "Yes" if hipaa else "No"
    print(f"DE debug -> field='HIPAA Privacy Rule' mapped_from='hipaa_privacy_rule' value='{rendered}'")
    await _guarded(
        errors,
        "radio 'Does this report include records that are subject to the HIPAA Privacy Rule?'",
        lambda: set_radio_field(page, "Does this report include records that are subject to the HIPAA Privacy Rule?", hipaa, "DE"),
    )


async def _set_date_part(locator: Any, value: str) -> None:
    try:
        await locator.select_option(value=value)
        return
    except Exception:
        pass
    await locator.select_option(label=value)


async def _set_incorporation_date_parts(page: Page, mm: str, dd: str, yyyy: str) -> None:
    row, _ = await locate_strict_row_for_label(page, "Date of Incorporation", "dropdown", "DE")
    selects = row.locator("select")
    if await selects.count() < 3:
        raise DelawareAutomationError("Could not find all Date of Incorporation dropdowns for DE.")
    await _set_date_part(selects.nth(0), mm)
    await _set_date_part(selects.nth(1), dd)
    await _set_date_part(selects.nth(2), yyyy)


def _resolve_incorporation_date_parts(record: Dict[str, Any]) -> tuple[str, str, str]:
    mm = _as_string(record.get("date_of_incorporation_month"))
    dd = _as_string(record.get("date_of_incorporation_day"))
    yyyy = _as_string(record.get("date_of_incorporation_year"))
    return mm, dd, yyyy


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"DE warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    for selector in ["input[type='file']", "input[type='file']:visible", "input[accept]", "input[type='file'][accept]"]:
        locator = page.locator(selector)
        if await locator.count() <= 0:
            continue
        try:
            await locator.first.set_input_files(str(file_path))
            await page.wait_for_timeout(1500)
            print("DE debug -> NAUPA uploaded; clicking upload-page Next")
            await _click_upload_page_next(page)
            await _wait_for_preview_or_signature(page)
            print("DE debug -> reached holder-preview; waiting for manual signature")
            print("DE finished - waiting for manual signature")
            return
        except Exception:
            continue

    raise DelawareAutomationError("Could not find DE upload file input.")


async def _click_upload_page_next(page: Page) -> None:
    for candidate in (page.get_by_role("button", name="next"), page.locator("button:has-text('NEXT')")):
        count = await candidate.count()
        for i in range(count):
            btn = candidate.nth(i)
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click(timeout=10_000)
                return
    raise DelawareAutomationError("Could not find enabled upload-page NEXT button on DE.")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise DelawareAutomationError("DE upload did not reach holder-preview or signature prompt.")


async def _click_next(page: Page) -> None:
    for candidate in (page.get_by_role("button", name="Next", exact=True), page.locator("button:has-text('Next')"), page.locator("input[type='submit'][value='Next']")):
        if await candidate.count() <= 0:
            continue
        target = candidate.first
        if not await target.is_enabled():
            continue
        await target.click(timeout=10_000)
        await page.wait_for_timeout(1000)
        return
    raise DelawareAutomationError("Could not find a clickable 'Next' control on DE page.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _normalize_report_type(raw: str) -> str:
    mapping = {"annual": "Annual Report", "annual report": "Annual Report"}
    return mapping.get(_normalize(raw), raw)


def _normalize_remit_method(raw: str) -> str:
    mapping = {
        "check": "Check",
        "wire": "Wire",
        "wire transfer": "Wire",
        "ach": "Check",
        "ach direct debit": "Check",
        "online": "Check",
        "dtc": "DTC",
    }
    normalized = _normalize(raw)
    return mapping.get(normalized, raw or "Check")


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
