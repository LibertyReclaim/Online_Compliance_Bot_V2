"""Virginia filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

VA_HOLDER_INFO_URL = "https://vamoneysearch.gov/app/holder-info"


class VirginiaAutomationError(RuntimeError):
    """Raised when VA automation cannot reliably continue."""


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

    await page.goto(VA_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_va_holder_info_page(page, record, errors)

    if errors:
        raise VirginiaAutomationError("VA holder info completed with errors:\n- " + "\n- ".join(errors))

    print("VA debug -> clicking Next after VA holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)


async def _fill_va_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    # Holder ID explicitly left blank for VA.
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "VA"))

    report_type = _as_string(record.get("report_type"))
    if not report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        await _guarded(errors, "dropdown 'Report Type'", lambda: _set_or_accept_report_type(page, report_type))

    report_year = _as_string(record.get("report_year"))
    if not report_year:
        errors.append("report_year is required for 'Report Year'.")
    else:
        await _guarded(errors, "dropdown 'Report Year'", lambda: select_dropdown_field(page, "Report Year", report_year, "VA"))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False

    await _guarded(
        errors,
        "radio 'This is a Negative Report'",
        lambda: set_radio_field(page, "This is a Negative Report", negative, "VA"),
    )

    if negative:
        return

    month, day, year, source = _resolve_due_diligence_parts(record)
    print(f"VA debug -> due diligence source='{source}'")
    await _guarded(errors, "dropdown '#dueDiligenceDate-month'", lambda: _set_due_diligence_part(page, "#dueDiligenceDate-month", month))
    await _guarded(errors, "dropdown '#dueDiligenceDate-day'", lambda: _set_due_diligence_part(page, "#dueDiligenceDate-day", day))
    await _guarded(errors, "dropdown '#dueDiligenceDate-year'", lambda: _set_due_diligence_part(page, "#dueDiligenceDate-year", year))
    print(f"VA debug -> selected MM='{month}' DD='{day}' YYYY='{year}'")

    amount = _as_string(record.get("amount_to_remit"))
    if not amount:
        errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
    else:
        print("VA debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
        await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "VA"))

    funds = _as_string(record.get("funds_remitted_via"))
    if not funds:
        errors.append("funds_remitted_via is required for 'Funds Remitted Via'.")
    else:
        normalized = _normalize_funds(funds)
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", normalized, "VA"))


async def _set_or_accept_report_type(page: Page, expected: str) -> None:
    row, _ = await locate_strict_row_for_label(page, "Report Type", "dropdown", "VA")
    control = row.locator("select").first

    expected_norm = _normalize(expected)
    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if _normalize(current_text) == expected_norm or _normalize(current_value) == expected_norm:
        print(f"VA debug -> Report Type already selected as {expected}; accepting")
        return

    try:
        await control.select_option(label=expected)
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        latest_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))
        if _normalize(latest_text) == expected_norm or _normalize(latest_value) == expected_norm:
            print(f"VA debug -> Report Type already selected as {expected}; accepting")
            return
        raise VirginiaAutomationError(f"VA failed selecting Report Type '{expected}'.") from exc


async def _set_due_diligence_part(page: Page, selector: str, value: str) -> None:
    locator = page.locator(selector).first
    if await locator.count() <= 0:
        raise VirginiaAutomationError(f"VA due-diligence dropdown not found: {selector}")

    # value first
    try:
        await locator.select_option(value=value)
        return
    except Exception:
        pass

    try:
        await locator.select_option(label=value)
        return
    except Exception:
        pass

    options = await locator.evaluate(
        "el => Array.from(el.options).map(o => ({text:(o.textContent||'').trim(), value:(o.value||'').trim()}))"
    )
    target = _normalize(value)
    for option in options:
        text = _normalize(str(option.get("text", "")))
        val = _normalize(str(option.get("value", "")))
        if text == target or val == target:
            await locator.select_option(value=str(option.get("value", "")))
            return

    raise VirginiaAutomationError(f"VA could not select due-diligence value '{value}' for {selector}")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"VA warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    selectors = ["input[type='file']", "input[type='file']:visible", "input[accept]", "input[type='file'][accept]"]
    for _ in range(3):
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                await page.wait_for_timeout(1000)
                return
            except Exception:
                continue
        await page.wait_for_timeout(800)

    raise VirginiaAutomationError("Could not find VA upload file input.")


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
    raise VirginiaAutomationError("Could not find a clickable 'Next' control on VA page.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _parse_date_triplet(date_text: str) -> tuple[str, str, str]:
    raw = _as_string(date_text)
    if not raw:
        return "", "", ""
    for sep in ["/", "-"]:
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep)]
            if len(parts) == 3:
                mm, dd, yyyy = parts
                mm = str(int(mm)) if mm.isdigit() else mm
                dd = str(int(dd)) if dd.isdigit() else dd
                return mm, dd, yyyy
    return "", "", ""


def _resolve_due_diligence_parts(record: Dict[str, Any]) -> tuple[str, str, str, str]:
    mm = _as_string(record.get("due_diligance_month"))
    dd = _as_string(record.get("due_diligance_day"))
    yyyy = _as_string(record.get("due_diligance_year"))
    if mm and dd and yyyy:
        return mm, dd, yyyy, "split misspelled columns"

    mm = _as_string(record.get("due_diligence_month"))
    dd = _as_string(record.get("due_diligence_day"))
    yyyy = _as_string(record.get("due_diligence_year"))
    if mm and dd and yyyy:
        return mm, dd, yyyy, "split corrected columns"

    due_date = _as_string(record.get("due_diligence_date"))
    if due_date:
        month, day, year = _parse_date_triplet(due_date)
        if month and day and year:
            return month, day, year, "single due_diligence_date"

    return "01", "01", "2026", "default 01/01/2026"


def _normalize_funds(raw_value: str) -> str:
    mapping = {
        "ach": "ACH",
        "check": "Check",
        "wire": "Wire",
        "electronic": "Online",
        "online": "Online",
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
