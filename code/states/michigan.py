"""Michigan filing runner for the Online_Compliance_Bot project."""

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

MI_HOLDER_INFO_URL = "https://unclaimedproperty.michigan.gov/app/holder-info"


class MichiganAutomationError(RuntimeError):
    """Raised when MI automation cannot reliably continue."""


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
    _TextFieldSpec("Holder Address 1", "address_1", required=True),
    _TextFieldSpec("Holder Address 2", "address_2"),
    _TextFieldSpec("Holder City", "city", required=True),
    _TextFieldSpec("Holder Zip Code", "zip", required=True),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run MI workflow through upload/preview and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(MI_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_mi_holder_info_page(page, record, errors)

    if errors:
        raise MichiganAutomationError("MI holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("MI debug -> clicking Next after MI holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_michigan = run
run_michigan_filing = run


async def _fill_mi_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if field.key == "zip" and not value:
            value = _as_string(record.get("zip_code"))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        print(f"MI debug -> field='{field.label}' type='TEXT'")
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: _fill_text_by_label(page, f.label, v))

    holder_state = _as_string(record.get("state"))
    if not holder_state:
        errors.append("state is required for 'Holder State'.")
    else:
        await _guarded(errors, "dropdown 'Holder State'", lambda: _set_dropdown_or_accept_disabled(page, "Holder State", holder_state))

    report_type_raw = _as_string(record.get("report_type"))
    if not report_type_raw:
        errors.append("report_type is required for 'Report Type'.")
    else:
        print(f"MI debug -> raw report_type input='{report_type_raw}'")
        report_type = _normalize_mi_report_type(report_type_raw)
        print(f"MI debug -> normalized MI report_type='{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: _set_dropdown_or_accept_disabled(page, "Report Type", report_type))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_mi_report_year(page, report_year))

    await _handle_negative_report(page, record, errors)

    shares = _as_string(record.get("total_number_of_shares_reported")) or "0"
    if not _as_string(record.get("total_number_of_shares_reported")):
        print("MI debug -> field='Total Number of Shares Reported' mapped_from='total_number_of_shares_reported'")
    else:
        print("MI debug -> field='Total Number of Shares Reported' mapped_from='total_number_of_shares_reported'")
    await _guarded(
        errors,
        "text 'Total Number of Shares Reported'",
        lambda: _fill_text_by_label(page, "Total Number of Shares Reported", shares),
    )

    tangible = _as_string(record.get("total_number_of_tangible_properties_reported")) or "0"
    print("MI debug -> field='Total Number of Tangible Properties Reported' mapped_from='total_number_of_tangible_properties_reported'")
    await _guarded(
        errors,
        "text 'Total Number of Tangible Properties Reported'",
        lambda: _fill_text_by_label(page, "Total Number of Tangible Properties Reported", tangible),
    )

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
    else:
        print("MI debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Total Dollar Amount Remitted'",
            lambda: _fill_text_by_label(page, "Total Dollar Amount Remitted", amount_to_remit),
        )

    funds = _as_string(record.get("funds_remitted_via"))
    if not funds:
        errors.append("funds_remitted_via is required for 'Funds Remitted Via'.")
    else:
        normalized_funds = _normalize_mi_funds_remitted_via(funds)
        await _guarded(
            errors,
            "dropdown 'Funds Remitted Via'",
            lambda: _set_dropdown_or_accept_disabled(page, "Funds Remitted Via", normalized_funds),
        )


async def _handle_negative_report(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    label = "This is a Negative Report"
    raw_value = _as_bool(record.get("negative_report"))
    if raw_value is None:
        raw_value = False

    try:
        row, _ = await locate_strict_row_for_label(page, label, "radio", "MI")
    except FieldResolutionError:
        return

    radios = row.locator("input[type='radio']")
    count = await radios.count()
    if count == 0:
        return

    enabled_count = 0
    for i in range(min(count, 6)):
        if await radios.nth(i).is_enabled():
            enabled_count += 1

    if enabled_count == 0:
        print("MI debug -> Negative Report disabled; accepting site-selected value")
        return

    await _guarded(errors, f"radio '{label}'", lambda: _set_yes_no_radio_by_label(page, label, raw_value))


async def _set_mi_report_year(page: Page, expected_year: str) -> None:
    label_text = "Report Year"
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "MI")
    except FieldResolutionError as exc:
        raise MichiganAutomationError("MI could not locate dropdown 'Report Year'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if enabled:
        await _set_dropdown_or_accept_disabled(page, label_text, expected_year)
        return

    if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
        print("MI debug -> Report Year disabled; accepting selected year")
        return
    if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
        print("MI debug -> Report Year disabled; accepting selected year")
        return

    raise MichiganAutomationError("MI Report Year is disabled but blank/unselected.")


async def _set_dropdown_or_accept_disabled(page: Page, label_text: str, expected_value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "MI")
    except FieldResolutionError as exc:
        raise MichiganAutomationError(f"MI could not locate dropdown '{label_text}'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if not enabled:
        if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
            print(f"MI debug -> field='{label_text}' disabled='yes' accepted='already selected' current_text='{current_text}'")
            return
        if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
            print(f"MI debug -> field='{label_text}' disabled='yes' accepted='already selected' current_value='{current_value}'")
            return
        raise MichiganAutomationError(f"MI dropdown '{label_text}' is disabled and blank/unselected.")

    expected_norm = _normalize(expected_value)
    try:
        await select_dropdown_field(page, label_text, expected_value, "MI")
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(latest_text) == expected_norm:
            print(f"MI debug -> field='{label_text}' accepted='already correct after selection attempt'")
            return
        raise MichiganAutomationError(
            f"MI failed selecting dropdown '{label_text}' with value '{expected_value}'."
        ) from exc


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"MI warning -> NAUPA file does not exist: {file_path}; skipping upload.")
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
            print(f"MI debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"MI debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"MI debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise MichiganAutomationError(
        "Could not find MI upload file input. Attempted selectors: "
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
    raise MichiganAutomationError("Could not find a clickable 'Next' control on MI page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "MI")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "MI")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"MI automation warning: {message}")
        errors.append(message)


def _normalize_mi_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
        "negative": "Negative Report",
        "negative report": "Negative Report",
    }
    normalized = _normalize(raw_value)
    if normalized in mapping:
        return mapping[normalized]
    raise MichiganAutomationError(f"Unsupported MI report_type value: '{raw_value}'")


def _normalize_mi_funds_remitted_via(raw_value: str) -> str:
    normalized = _normalize(raw_value)
    mapping = {
        "check": "Check",
        "ach": "Online",
        "wire": "Online",
        "online": "Online",
        "electronic": "Online",
        "securities": "Securities or Tangible Items Only - no funds remitted",
        "tangible": "Securities or Tangible Items Only - no funds remitted",
        "no funds": "Securities or Tangible Items Only - no funds remitted",
    }
    if normalized in mapping:
        return mapping[normalized]
    return raw_value


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
