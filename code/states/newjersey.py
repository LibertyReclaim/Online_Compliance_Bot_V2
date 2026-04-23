"""New Jersey filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import FieldResolutionError, fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

NJ_HOLDER_INFO_URL = "https://unclaimedfunds.nj.gov/app/holder-info"


class NewJerseyAutomationError(RuntimeError):
    """Raised when NJ automation cannot reliably continue."""


@dataclass(frozen=True)
class _FieldSpec:
    label: str
    key: str


_TEXT_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Holder Name", "holder_name"),
    _FieldSpec("Holder Tax ID", "holder_tax_id"),
    _FieldSpec("Contact Name", "contact_name"),
    _FieldSpec("Contact Phone Number", "contact_phone"),
    _FieldSpec("Phone Extension", "phone_extension"),
    _FieldSpec("Email Address", "email"),
    _FieldSpec("Email Address Confirmation", "email"),
)

_NJ_REPORT_TYPE_OPTIONS: set[str] = {
    "annual report",
    "audit report",
    "reciprocal report",
    "supplemental report",
    "voluntary disclosure agreement",
}


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run NJ workflow through upload/preview steps and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(NJ_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_nj_holder_info_page(page, record, errors)

    if errors:
        raise NewJerseyAutomationError("NJ holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("NJ debug -> clicking Next after NJ holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_newjersey = run
run_newjersey_filing = run


async def _fill_nj_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if field.key == "phone_extension" and not value:
            continue
        if not value:
            continue
        print(f"NJ debug -> field='{field.label}' type='TEXT'")
        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))

    report_type = _as_string(record.get("report_type"))
    if report_type:
        if _normalize(report_type) not in _NJ_REPORT_TYPE_OPTIONS:
            print(f"NJ warning -> Invalid report_type value: '{report_type}'")
        print("NJ debug -> field='Report Type' type='DROPDOWN'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: _select_dropdown_by_label(page, "Report Type", report_type))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_or_accept_disabled_report_year(page, report_year))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        raise NewJerseyAutomationError("negative_report is required for NJ filing.")

    await _guarded(errors, "radio 'This is a Negative Report'", lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative))

    if negative:
        print("NJ debug -> field='This is a Negative Report' type='RADIO' value='Yes' strategy='strict row'")
    else:
        print("NJ debug -> field='This is a Negative Report' type='RADIO' value='No' strategy='strict row'")

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required for NJ filing.")
    else:
        print("NJ debug -> field='Total Dollar Amount Remitted' type='TEXT'")
        await _guarded(
            errors,
            "text 'Total Dollar Amount Remitted'",
            lambda: _fill_text_by_label(page, "Total Dollar Amount Remitted", amount_to_remit),
        )

    payment_type = _as_string(record.get("funds_remitted_via"))
    if not payment_type:
        errors.append("funds_remitted_via is required for NJ Payment Type.")
    else:
        print("NJ debug -> field='Payment Type' mapped_from='funds_remitted_via'")
        await _guarded(errors, "dropdown 'Payment Type'", lambda: _select_dropdown_by_label(page, "Payment Type", payment_type))


async def _set_or_accept_disabled_report_year(page: Page, expected_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "NJ")
    except FieldResolutionError as exc:
        raise NewJerseyAutomationError("NJ could not locate Report Year dropdown row") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()")
    current_value = await control.evaluate("el => (el.value || '').trim()")

    expected_norm = _normalize(expected_year)
    text_norm = _normalize(str(current_text))
    value_norm = _normalize(str(current_value))

    print(
        f"NJ debug -> Report Year enabled={'yes' if enabled else 'no'} "
        f"current_text='{current_text}' current_value='{current_value}' expected='{expected_year}'"
    )

    if not enabled:
        if expected_norm in text_norm or expected_norm == value_norm:
            print("NJ debug -> Report Year disabled but already correct; continuing")
            return
        raise NewJerseyAutomationError("NJ Report Year dropdown stayed disabled after selecting Report Type and did not match expected value")

    await _select_dropdown_by_label(page, "Report Year", expected_year)


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"NJ warning -> NAUPA file does not exist: {file_path}; skipping upload.")
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
            print(f"NJ debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"NJ debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"NJ debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise NewJerseyAutomationError(
        "Could not find NJ upload file input. Attempted selectors: "
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
    raise NewJerseyAutomationError("Could not find a clickable 'Next' control on NJ page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "NJ")


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    await select_dropdown_field(page, label_text, value, "NJ")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "NJ")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"NJ automation warning: {message}")
        errors.append(message)


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
