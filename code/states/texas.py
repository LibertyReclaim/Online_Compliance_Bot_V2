"""Texas filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import (
    FieldResolutionError,
    fill_text_field,
    locate_strict_row_for_label,
    select_dropdown_field,
    set_radio_field,
)

TX_HOLDER_INFO_URL = "https://claimittexas.gov/app/holder-info"


class TexasAutomationError(RuntimeError):
    """Raised when TX automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


@dataclass(frozen=True)
class _DropdownFieldSpec:
    label: str
    key: str
    required: bool = False


@dataclass(frozen=True)
class _RadioFieldSpec:
    label: str
    key: str


_PRIMARY_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Holder Contact", "contact_name", required=True),
    _TextFieldSpec("Contact Phone No.", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Previous Business Name", "previous_business_name"),
    _TextFieldSpec("Previous FEIN", "previous_FEIN"),
    _TextFieldSpec("Primary Business Activity", "primary_business_activity"),
)

_LOCATION_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email", required=True),
    _TextFieldSpec("Address 1", "address_1", required=True),
    _TextFieldSpec("Address 2", "address_2"),
    _TextFieldSpec("City", "city", required=True),
    _TextFieldSpec("ZIP Code", "zip", required=True),
)

_PRIMARY_DROPDOWNS: tuple[_DropdownFieldSpec, ...] = (
    _DropdownFieldSpec("State of Incorporation", "state_of_incorporation"),
    _DropdownFieldSpec("State", "state", required=True),
)

_REPORT_DROPDOWNS: tuple[_DropdownFieldSpec, ...] = (
    _DropdownFieldSpec("Report Year", "report_year", required=True),
)

_TOTAL_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Total Number of Items Reported", "total_items_reported", required=False),
    _TextFieldSpec("Total Number of Safekeeping Items", "total_safekeeping_items", required=False),
    _TextFieldSpec("Shares of Stocks or Mutual Funds Remitted", "shares_remitted", required=False),
)

_FIRST_TIME_RADIO = "Is this the first time this business entity has filed an Unclaimed Property Report?"
_HIPAA_RADIO = "Does this report include records that are subject to the HIPAA Privacy Rule?"
_COMBINED_FILE_RADIO = "Is this a combined file containing multiple reports for related entities under the same parent company?"
_NEGATIVE_REPORT_RADIO = "This is a Negative Report"


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run TX workflow through upload/preview and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(TX_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_tx_holder_info_page(page, record, errors)

    if errors:
        raise TexasAutomationError("TX holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("TX debug -> clicking Next after TX holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_texas = run
run_texas_filing = run


async def _fill_tx_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    print("TX debug -> field='Report ID' skipped='disabled/read-only'")

    for field in _PRIMARY_TEXT_FIELDS:
        await _fill_required_text_field(page, field, record, errors)

    state_of_incorp = _as_string(record.get("state_of_incorporation"))
    if state_of_incorp:
        print("TX debug -> field='State of Incorporation' type='DROPDOWN'")
        await _guarded(
            errors,
            "dropdown 'State of Incorporation'",
            lambda: _set_dropdown_or_accept_disabled(page, "State of Incorporation", state_of_incorp),
        )

    await _fill_date_triplet(
        page,
        "Date of Incorporation",
        _as_string(record.get("date_of_incorporation_month")),
        _as_string(record.get("date_of_incorporation_day")),
        _as_string(record.get("date_of_incorporation_year")),
        errors,
    )
    await _fill_date_triplet(
        page,
        "Date of Dissolution",
        _as_string(record.get("date_of_dissolution_month")),
        _as_string(record.get("date_of_dissolution_day")),
        _as_string(record.get("date_of_dissolution_year")),
        errors,
    )

    first_time = _as_bool(record.get("first_time_report"))
    if first_time is None:
        errors.append("first_time_report is required for TX filing.")
    else:
        print(f"TX debug -> field='{_FIRST_TIME_RADIO}' type='RADIO'")
        await _guarded(
            errors,
            f"radio '{_FIRST_TIME_RADIO}'",
            lambda: _set_yes_no_radio_by_label(page, _FIRST_TIME_RADIO, first_time),
        )

    for field in _LOCATION_TEXT_FIELDS:
        await _fill_required_text_field(page, field, record, errors)

    state_value = _as_string(record.get("state"))
    if state_value:
        print("TX debug -> field='State' type='DROPDOWN'")
        await _guarded(errors, "dropdown 'State'", lambda: _set_dropdown_or_accept_disabled(page, "State", state_value))
    else:
        errors.append("state is required for 'State'.")

    raw_report_type = _as_string(record.get("report_type"))
    if not raw_report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        print(f"TX debug -> raw report_type input='{raw_report_type}'")
        normalized_report_type = _normalize_tx_report_type(raw_report_type)
        print(f"TX debug -> normalized TX report_type='{normalized_report_type}'")
        await _guarded(
            errors,
            "dropdown 'Report Type'",
            lambda: _set_dropdown_or_accept_disabled(page, "Report Type", normalized_report_type),
        )

    for field in _REPORT_DROPDOWNS:
        await _set_dropdown_with_disabled_acceptance(page, field, record, errors)

    hipaa = _as_bool_or_default_no(record.get("includes_hipaa_records"))
    print(f"TX debug -> field='{_HIPAA_RADIO}' type='RADIO'")
    await _guarded(errors, f"radio '{_HIPAA_RADIO}'", lambda: _set_yes_no_radio_by_label(page, _HIPAA_RADIO, hipaa))

    combined_file = _as_bool(record.get("combined_file"))
    if combined_file is None:
        errors.append("combined_file is required for TX filing.")
    else:
        print(f"TX debug -> field='{_COMBINED_FILE_RADIO}' type='RADIO'")
        await _guarded(
            errors,
            f"radio '{_COMBINED_FILE_RADIO}'",
            lambda: _set_yes_no_radio_by_label(page, _COMBINED_FILE_RADIO, combined_file),
        )

    await _fill_parent_company_fein(page, record, combined_file, errors)

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        errors.append("negative_report is required for TX filing.")
    else:
        print(f"TX debug -> field='{_NEGATIVE_REPORT_RADIO}' type='RADIO'")
        await _guarded(
            errors,
            f"radio '{_NEGATIVE_REPORT_RADIO}'",
            lambda: _set_yes_no_radio_by_label(page, _NEGATIVE_REPORT_RADIO, negative),
        )

    total_amount_of_report = _as_string(record.get("total_amount_of_report"))
    if not total_amount_of_report:
        errors.append("total_amount_of_report is required for Total Amount of the Report.")
    else:
        print("TX debug -> field='Total Amount of the Report' mapped_from='total_amount_of_report'")
        await _guarded(
            errors,
            "text 'Total Amount of the Report'",
            lambda: _fill_text_by_label(page, "Total Amount of the Report", total_amount_of_report),
        )

    total_items = _as_string(record.get("total_items_reported"))
    if not total_items:
        print("TX debug -> total_items_reported blank; defaulting to 1")
        total_items = "1"
    await _guarded(
        errors,
        "text 'Total Number of Items Reported'",
        lambda: _fill_text_by_label(page, "Total Number of Items Reported", total_items),
    )

    total_safekeeping = _as_string(record.get("total_safekeeping_items"))
    if not total_safekeeping:
        print("TX debug -> total_safekeeping_items blank; defaulting to 0")
        total_safekeeping = "0"
    await _guarded(
        errors,
        "text 'Total Number of Safekeeping Items'",
        lambda: _fill_text_by_label(page, "Total Number of Safekeeping Items", total_safekeeping),
    )

    shares_remitted = _as_string(record.get("shares_remitted"))
    if not shares_remitted:
        print("TX debug -> shares_remitted blank; defaulting to 0")
        shares_remitted = "0"
    await _guarded(
        errors,
        "text 'Shares of Stocks or Mutual Funds Remitted'",
        lambda: _fill_text_by_label(page, "Shares of Stocks or Mutual Funds Remitted", shares_remitted),
    )

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required for Total Payment Amount.")
    else:
        print("TX debug -> field='Total Payment Amount' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Total Payment Amount'",
            lambda: _fill_text_by_label(page, "Total Payment Amount", amount_to_remit),
        )

    funds_remitted_via = _as_string(record.get("funds_remitted_via"))
    if not funds_remitted_via:
        errors.append("funds_remitted_via is required for TX filing.")
    else:
        print("TX debug -> field='Funds Remitted Via' type='DROPDOWN'")
        await _guarded(
            errors,
            "dropdown 'Funds Remitted Via'",
            lambda: _set_dropdown_or_accept_disabled(page, "Funds Remitted Via", funds_remitted_via),
        )


async def _fill_date_triplet(
    page: Page,
    base_label: str,
    month_value: str,
    day_value: str,
    year_value: str,
    errors: list[str],
) -> None:
    if not any([month_value, day_value, year_value]):
        return

    print(f"TX debug -> field='{base_label}' type='DROPDOWN_TRIPLET'")
    parts = (
        (f"{base_label} month", month_value),
        (f"{base_label} day", day_value),
        (f"{base_label} year", year_value),
    )
    for label_text, raw_value in parts:
        value = _normalize_date_part(raw_value)
        if not value:
            continue
        await _guarded(
            errors,
            f"dropdown '{label_text}'",
            lambda l=label_text, v=value: _set_dropdown_or_accept_disabled(page, l, v),
        )


def _normalize_date_part(value: str) -> str:
    text = _as_string(value)
    if not text:
        return ""
    if text.isdigit():
        stripped = str(int(text))
        if len(text) in {1, 2}:
            return stripped
    return text


async def _fill_parent_company_fein(
    page: Page,
    record: Dict[str, Any],
    combined_file: Optional[bool],
    errors: list[str],
) -> None:
    parent_fein = _as_string(record.get("parent_company_fein"))
    label = "Parent Company FEIN"

    if combined_file is False:
        print("TX debug -> field='Parent Company FEIN' skipped='combined_file=No'")
        return

    try:
        row, _ = await locate_strict_row_for_label(page, label, "text", "TX")
    except FieldResolutionError as exc:
        raise TexasAutomationError("TX could not locate Parent Company FEIN field") from exc

    control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
    enabled = await control.is_enabled()

    if not enabled:
        print("TX debug -> field='Parent Company FEIN' skipped='disabled/read-only'")
        return

    if not parent_fein:
        errors.append("parent_company_fein is required when combined_file=Yes.")
        return

    print("TX debug -> field='Parent Company FEIN' type='TEXT'")
    await _guarded(errors, "text 'Parent Company FEIN'", lambda: _fill_text_by_label(page, label, parent_fein))


async def _fill_required_text_field(page: Page, field: _TextFieldSpec, record: Dict[str, Any], errors: list[str]) -> None:
    value = _as_string(record.get(field.key))
    if not value:
        if field.required:
            errors.append(f"{field.key} is required for '{field.label}'.")
        return

    print(f"TX debug -> field='{field.label}' type='TEXT'")
    await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))


async def _set_dropdown_with_disabled_acceptance(
    page: Page,
    field: _DropdownFieldSpec,
    record: Dict[str, Any],
    errors: list[str],
) -> None:
    value = _as_string(record.get(field.key))
    if not value:
        if field.required:
            errors.append(f"{field.key} is required for '{field.label}'.")
        return

    print(f"TX debug -> field='{field.label}' type='DROPDOWN'")
    await _guarded(
        errors,
        f"dropdown '{field.label}'",
        lambda f=field, v=value: _set_dropdown_or_accept_disabled(page, f.label, v),
    )


async def _set_dropdown_or_accept_disabled(page: Page, label_text: str, expected_value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "TX")
    except FieldResolutionError as exc:
        raise TexasAutomationError(f"TX could not locate dropdown '{label_text}'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    expected_norm = _normalize(expected_value)
    current_text_norm = _normalize(current_text)
    current_value_norm = _normalize(current_value)

    if not enabled:
        if expected_norm and (expected_norm in current_text_norm or expected_norm == current_value_norm):
            print(f"TX debug -> field='{label_text}' disabled='yes' accepted='already correct'")
            return
        raise TexasAutomationError(
            f"TX dropdown '{label_text}' is disabled and does not match expected value '{expected_value}'."
        )

    try:
        await select_dropdown_field(page, label_text, expected_value, "TX")
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(latest_text) == expected_norm:
            print(f"TX debug -> field='{label_text}' accepted='already correct after selection attempt'")
            return
        raise TexasAutomationError(
            f"TX failed selecting dropdown '{label_text}' with value '{expected_value}'."
        ) from exc


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"TX warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    selectors = [
        "input[type='file']",
        "input[type=file]",
        "input[type='file']:visible",
        "input[accept]",
        "input[type='file'][accept]",
    ]

    for _ in range(3):
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            print(f"TX debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"TX debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"TX debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise TexasAutomationError(
        "Could not find TX upload file input. Attempted selectors: "
        "input[type='file'], input[type=file], input[type='file']:visible, input[accept], input[type='file'][accept]."
    )


async def _click_next(page: Page) -> None:
    candidates = (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
    )
    for candidate in candidates:
        if await candidate.count() <= 0:
            continue
        target = candidate.first
        if not await target.is_enabled():
            continue
        await target.click(timeout=10_000)
        await page.wait_for_timeout(1000)
        return
    raise TexasAutomationError("Could not find a clickable 'Next' control on TX page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "TX")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "TX")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"TX automation warning: {message}")
        errors.append(message)




def _normalize_tx_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
        "audit": "Audit Report",
        "audit report": "Audit Report",
        "supplemental": "Supplemental Report",
        "supplemental report": "Supplemental Report",
        "reciprocal": "Reciprocal Report",
        "reciprocal report": "Reciprocal Report",
    }
    normalized = _normalize(raw_value)
    if normalized in mapping:
        return mapping[normalized]
    raise TexasAutomationError(f"Unsupported TX report_type value: '{raw_value}'")


def _as_bool_or_default_no(value: Any) -> bool:
    parsed = _as_bool(value)
    if parsed is None:
        return False
    return parsed

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
