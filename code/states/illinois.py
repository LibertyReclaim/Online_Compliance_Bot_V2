"""Illinois filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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

IL_HOLDER_INFO_URL = "https://icash.illinoistreasurer.gov/app/holder-info"


class IllinoisAutomationError(RuntimeError):
    """Raised when IL automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Holder Address 1", "address_1", required=True),
    _TextFieldSpec("Holder Address 2", "address_2"),
    _TextFieldSpec("Holder Address 3", "address_3"),
    _TextFieldSpec("Holder City", "city", required=True),
    _TextFieldSpec("Holder Contact", "contact_name", required=True),
    _TextFieldSpec("Contact Phone No.", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email", required=True),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run IL workflow through upload/preview and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(IL_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_il_holder_info_page(page, record, errors)

    if errors:
        raise IllinoisAutomationError("IL holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("IL debug -> clicking Next after IL holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_illinois = run
run_illinois_filing = run


async def _fill_il_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        await _fill_text_field_with_rules(page, field, record, errors)

    zip_value = _as_string(record.get("zip_code")) or _as_string(record.get("zip"))
    if not zip_value:
        errors.append("zip_code/zip is required for 'Holder Zip'.")
    else:
        print("IL debug -> field='Holder Zip' type='TEXT'")
        await _guarded(errors, "text 'Holder Zip'", lambda: _fill_text_by_label(page, "Holder Zip", zip_value))

    holder_state = _as_string(record.get("state"))
    if not holder_state:
        errors.append("state is required for 'Holder State'.")
    else:
        print("IL debug -> field='Holder State' type='DROPDOWN'")
        await _guarded(errors, "dropdown 'Holder State'", lambda: _set_dropdown_or_accept_disabled(page, "Holder State", holder_state))

    report_type_raw = _as_string(record.get("report_type"))
    if not report_type_raw:
        errors.append("report_type is required for 'Report Type'.")
    else:
        report_type = _normalize_il_report_type(report_type_raw)
        print(f"IL debug -> raw report_type input='{report_type_raw}'")
        print(f"IL debug -> normalized IL report_type='{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: _set_dropdown_or_accept_disabled(page, "Report Type", report_type))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_il_report_year(page, report_year))

    negative_report = _as_bool(record.get("negative_report"))
    if negative_report is None:
        negative_report = False

    await _guarded(
        errors,
        "radio 'This is a Negative Report'",
        lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative_report),
    )

    await _fill_statistics_fields(page, record, errors)
    await _fill_amount_fields(page, record, negative_report, errors)


async def _fill_statistics_fields(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    number_of_employees = _as_string(record.get("number_of_employees")) or "0"
    annual_sales = _as_string(record.get("annual_sales/premiums")) or _as_string(record.get("annual_sales_premiums")) or "0"
    total_assets = _as_string(record.get("total_assets")) or "0"

    await _guarded(
        errors,
        "text 'Number of Employees'",
        lambda: _fill_text_by_label(page, "Number of Employees", number_of_employees),
    )
    await _guarded(
        errors,
        "text 'Annual Sales/Premiums'",
        lambda: _fill_currency_text_by_label(page, "Annual Sales/Premiums", annual_sales),
    )
    await _guarded(
        errors,
        "text 'Total Assets'",
        lambda: _fill_text_by_label(page, "Total Assets", total_assets),
    )


async def _fill_amount_fields(page: Page, record: Dict[str, Any], negative_report: bool, errors: list[str]) -> None:
    reported_amount = _as_string(record.get("total_amount_of_report"))
    amount_to_remit = _as_string(record.get("amount_to_remit"))

    if negative_report:
        skip_reported = await _is_text_field_disabled(page, "Reported Amount")
        skip_remit = await _is_text_field_disabled(page, "Amount To Be Remitted")
        if skip_reported or skip_remit:
            return

    if not reported_amount and not negative_report:
        errors.append("total_amount_of_report is required for 'Reported Amount'.")
    elif reported_amount:
        print("IL debug -> field='Reported Amount' mapped_from='total_amount_of_report'")
        await _guarded(errors, "text 'Reported Amount'", lambda: _fill_text_by_label(page, "Reported Amount", reported_amount))

    if not amount_to_remit and not negative_report:
        errors.append("amount_to_remit is required for 'Amount To Be Remitted'.")
    elif amount_to_remit:
        print("IL debug -> field='Amount To Be Remitted' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Amount To Be Remitted'",
            lambda: _fill_text_by_label(page, "Amount To Be Remitted", amount_to_remit),
        )


async def _fill_currency_text_by_label(page: Page, label_text: str, value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "text", "IL")
    except FieldResolutionError as exc:
        raise IllinoisAutomationError(f"IL could not locate text field '{label_text}'.") from exc

    control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
    await control.fill("")
    await control.type(value)
    await control.blur()

    actual = _as_string(await control.input_value())
    expected_num = _parse_currency_decimal(value)
    actual_num = _parse_currency_decimal(actual)

    if expected_num is not None and actual_num is not None and expected_num == actual_num:
        return

    expected_norm = _normalize(value)
    actual_norm = _normalize(actual)
    if expected_norm != actual_norm:
        raise IllinoisAutomationError(
            f"IL currency verification failed for '{label_text}'. raw_expected='{value}' raw_actual='{actual}'."
        )


def _parse_currency_decimal(value: str) -> Optional[Decimal]:
    raw = _as_string(value).replace("$", "").replace(",", "").replace(" ", "")
    if not raw:
        return None
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


async def _set_il_report_year(page: Page, expected_year: str) -> None:
    label_text = "Report Year"
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "IL")
    except FieldResolutionError as exc:
        raise IllinoisAutomationError("IL could not locate dropdown 'Report Year'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if enabled:
        await _set_dropdown_or_accept_disabled(page, label_text, expected_year)
        return

    current_norm = _normalize(current_text)
    value_norm = _normalize(current_value)
    if (
        current_norm and current_norm not in {"select an option", "select option", "please select"}
    ) or (
        value_norm and value_norm not in {"", "0", "-1", "select", "select an option"}
    ):
        print("IL debug -> Report Year disabled; accepting selected year")
        return

    raise IllinoisAutomationError("IL Report Year is disabled but blank/unselected.")


async def _fill_text_field_with_rules(page: Page, field: _TextFieldSpec, record: Dict[str, Any], errors: list[str]) -> None:
    value = _as_string(record.get(field.key))
    if not value:
        if field.required:
            errors.append(f"{field.key} is required for '{field.label}'.")
        return

    print(f"IL debug -> field='{field.label}' type='TEXT'")
    await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))


async def _set_dropdown_or_accept_disabled(page: Page, label_text: str, expected_value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "IL")
    except FieldResolutionError as exc:
        raise IllinoisAutomationError(f"IL could not locate dropdown '{label_text}'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    expected_norm = _normalize(expected_value)
    current_text_norm = _normalize(current_text)
    current_value_norm = _normalize(current_value)

    if not enabled:
        if expected_norm and (expected_norm in current_text_norm or expected_norm == current_value_norm):
            print(f"IL debug -> field='{label_text}' disabled='yes' accepted='already correct'")
            return
        raise IllinoisAutomationError(
            f"IL dropdown '{label_text}' is disabled and does not match expected value '{expected_value}'."
        )

    try:
        await select_dropdown_field(page, label_text, expected_value, "IL")
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(latest_text) == expected_norm:
            print(f"IL debug -> field='{label_text}' accepted='already correct after selection attempt'")
            return
        raise IllinoisAutomationError(
            f"IL failed selecting dropdown '{label_text}' with value '{expected_value}'."
        ) from exc


async def _is_text_field_disabled(page: Page, label_text: str) -> bool:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "text", "IL")
    except FieldResolutionError:
        return False
    control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
    return not await control.is_enabled()


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"IL warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    selectors = [
        "input[type='file']",
        "input[type='file']:visible",
        "input[accept]",
        "input[type='file'][accept]",
    ]

    for _ in range(3):
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            print(f"IL debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"IL debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"IL debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise IllinoisAutomationError(
        "Could not find IL upload file input. Attempted selectors: "
        "input[type='file'], input[type='file']:visible, input[accept], input[type='file'][accept]."
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
    raise IllinoisAutomationError("Could not find a clickable 'Next' control on IL page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "IL")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "IL")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"IL automation warning: {message}")
        errors.append(message)


def _normalize_il_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
        "audit": "Audit Report",
        "audit report": "Audit Report",
        "supplemental": "Supplemental Report",
        "supplemental report": "Supplemental Report",
    }
    normalized = _normalize(raw_value)
    if normalized in mapping:
        return mapping[normalized]
    raise IllinoisAutomationError(f"Unsupported IL report_type value: '{raw_value}'")


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
