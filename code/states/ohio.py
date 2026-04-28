"""Ohio filing runner for the Online_Compliance_Bot project."""

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
)

OH_HOLDER_INFO_URL = "https://unclaimedfunds.ohio.gov/app/holder-info"


class OhioAutomationError(RuntimeError):
    """Raised when OH automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Address 1", "address_1", required=True),
    _TextFieldSpec("Address 2", "address_2"),
    _TextFieldSpec("City", "city", required=True),
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
    """Run OH workflow through upload/preview and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(OH_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_oh_holder_info_page(page, record, errors)

    if errors:
        raise OhioAutomationError("OH holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("OH debug -> clicking Next after OH holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_ohio = run
run_ohio_filing = run


async def _fill_oh_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        print(f"OH debug -> field='{field.label}' type='TEXT'")
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: _fill_text_by_label(page, f.label, v))

    postal_code = _as_string(record.get("zip")) or _as_string(record.get("zip_code"))
    if not postal_code:
        errors.append("zip/zip_code is required for 'Postal Code'.")
    else:
        print("OH debug -> field='Postal Code' mapped_from='zip/zip_code'")
        await _guarded(errors, "text 'Postal Code'", lambda: _fill_text_by_label(page, "Postal Code", postal_code))

    await _set_optional_dropdown(page, errors, "State", _as_string(record.get("state")), required=True, key_name="state")
    await _set_optional_dropdown(
        page,
        errors,
        "State of Incorporation",
        _as_string(record.get("state_of_incorporation")),
        required=False,
        key_name="state_of_incorporation",
    )

    await _fill_date_triplet(
        page,
        "Date of Incorporation",
        _as_string(record.get("date_of_incorporation_month")),
        _as_string(record.get("date_of_incorporation_day")),
        _as_string(record.get("date_of_incorporation_year")),
        errors,
    )

    report_year = _as_string(record.get("report_year"))
    await _set_optional_dropdown(page, errors, "Report Year", report_year, required=True, key_name="report_year")

    raw_report_type = _as_string(record.get("report_type"))
    if not raw_report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        print(f"OH debug -> raw report_type input='{raw_report_type}'")
        normalized_report_type = _normalize_oh_report_type(raw_report_type)
        print(f"OH debug -> normalized OH report_type='{normalized_report_type}'")
        await _guarded(
            errors,
            "dropdown 'Report Type'",
            lambda: _set_dropdown_or_accept_disabled(page, "Report Type", normalized_report_type),
        )

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
    else:
        print("OH debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Total Dollar Amount Remitted'",
            lambda: _fill_text_by_label(page, "Total Dollar Amount Remitted", amount_to_remit),
        )

    funds_remitted_via = _as_string(record.get("funds_remitted_via"))
    await _set_optional_dropdown(
        page,
        errors,
        "Funds Remitted Via",
        funds_remitted_via,
        required=True,
        key_name="funds_remitted_via",
    )


async def _set_optional_dropdown(
    page: Page,
    errors: list[str],
    label_text: str,
    value: str,
    *,
    required: bool,
    key_name: str,
) -> None:
    if not value:
        if required:
            errors.append(f"{key_name} is required for '{label_text}'.")
        return

    print(f"OH debug -> field='{label_text}' type='DROPDOWN'")
    await _guarded(errors, f"dropdown '{label_text}'", lambda: _set_dropdown_or_accept_disabled(page, label_text, value))


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

    print(f"OH debug -> field='{base_label}' type='DROPDOWN_TRIPLET'")
    parts = (
        (f"{base_label} month", month_value),
        (f"{base_label} day", day_value),
        (f"{base_label} year", year_value),
    )
    for label_text, raw_value in parts:
        value = _normalize_date_part(raw_value)
        if not value:
            continue
        await _set_optional_date_dropdown_part(page, base_label, label_text, value)


async def _set_optional_date_dropdown_part(page: Page, base_label: str, label_text: str, value: str) -> None:
    part_name = label_text.replace(f"{base_label} ", "")
    try:
        await _set_dropdown_or_accept_disabled(page, label_text, value)
    except Exception:
        print(f"OH debug -> optional {base_label} {part_name} dropdown not found; skipping")


async def _set_dropdown_or_accept_disabled(page: Page, label_text: str, expected_value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "OH")
    except FieldResolutionError as exc:
        raise OhioAutomationError(f"OH could not locate dropdown '{label_text}'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if not enabled:
        if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
            print(f"OH debug -> field='{label_text}' disabled='yes' accepted='already selected' current_text='{current_text}'")
            return
        if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
            print(f"OH debug -> field='{label_text}' disabled='yes' accepted='already selected' current_value='{current_value}'")
            return
        raise OhioAutomationError(f"OH dropdown '{label_text}' is disabled and blank/unselected.")

    expected_norm = _normalize(expected_value)
    try:
        await select_dropdown_field(page, label_text, expected_value, "OH")
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(latest_text) == expected_norm:
            print(f"OH debug -> field='{label_text}' accepted='already correct after selection attempt'")
            return
        raise OhioAutomationError(
            f"OH failed selecting dropdown '{label_text}' with value '{expected_value}'."
        ) from exc


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"OH warning -> NAUPA file does not exist: {file_path}; skipping upload.")
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
            print(f"OH debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"OH debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"OH debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise OhioAutomationError(
        "Could not find OH upload file input. Attempted selectors: "
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
    raise OhioAutomationError("Could not find a clickable 'Next' control on OH page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "OH")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"OH automation warning: {message}")
        errors.append(message)


def _normalize_oh_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
        "audit": "Audit Report",
        "audit report": "Audit Report",
    }
    normalized = _normalize(raw_value)
    if normalized in mapping:
        return mapping[normalized]
    raise OhioAutomationError(f"Unsupported OH report_type value: '{raw_value}'")


def _normalize_date_part(value: str) -> str:
    text = _as_string(value)
    if not text:
        return ""
    if text.isdigit() and len(text) in {1, 2}:
        return str(int(text))
    return text


def _merge_records(holder_row: Dict[str, Any], payment_row: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(holder_row)
    merged.update(payment_row)
    return merged


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    rendered = str(value).strip()
    return "" if rendered.lower() == "nan" else rendered


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace(":", "").strip().lower().split())
