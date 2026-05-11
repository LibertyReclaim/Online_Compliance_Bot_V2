"""South Carolina filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

SC_HOLDER_INFO_URL = "https://southcarolina.findyourunclaimedproperty.com/app/holder-info"
SC_HIPAA_LABEL = "Does this report include records that are subject to the HIPAA Privacy Rule"


class SouthCarolinaAutomationError(RuntimeError):
    """Raised when SC automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Contact Person", "contact_name", required=True),
    _TextFieldSpec("Contact Phone Number", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email_confirmation", required=True),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    del naupa_file_path  # SC currently stops after reaching the upload page.

    record = _merge_records(holder_row, payment_row)

    await page.goto(SC_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_sc_holder_info_page(page, record, errors)

    if errors:
        raise SouthCarolinaAutomationError("SC holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("SC debug -> clicking Next after SC holder info completed")
    await click_next(page, "after SC holder info")
    await _wait_for_upload_page(page)


async def _fill_sc_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _resolve_text_field_value(record, field)
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "SC"))

    report_type = _as_string(record.get("report_type"))
    if report_type:
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "SC"))

    await _set_report_year_if_enabled(page, _as_string(record.get("report_year")))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: set_radio_field(page, "This is a Negative Report", negative, "SC"))

    if not negative:
        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print("SC debug -> field='Total Dollar Amount Remitted' mapped_from='amount_to_remit'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "SC"))

        funds = _normalize_funds(_as_string(record.get("funds_remitted_via")))
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", funds, "SC"))

    hipaa = _as_bool(record.get("hipaa_privacy_rule"))
    if hipaa is None:
        hipaa = _as_bool(record.get("includes_hipaa_records"))
    if hipaa is None:
        hipaa = False
    await _guarded(errors, f"radio '{SC_HIPAA_LABEL}'", lambda: set_radio_field(page, SC_HIPAA_LABEL, hipaa, "SC"))


def _resolve_text_field_value(record: Dict[str, Any], field: _TextFieldSpec) -> str:
    value = _as_string(record.get(field.key))
    if field.key == "holder_tax_id" and not value:
        return _as_string(record.get("fein"))
    if field.key == "email_confirmation" and not value:
        fallback_email = _as_string(record.get("email"))
        if fallback_email:
            print("SC debug -> Email Confirmation missing; using Email value")
            return fallback_email
    return value


async def _set_report_year_if_enabled(page: Page, report_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "SC")
        locator = row.locator("select").first
        if await locator.count() <= 0:
            return

        disabled_or_readonly = await locator.evaluate(
            """
            el => Boolean(
                el.disabled ||
                el.readOnly ||
                el.getAttribute('readonly') !== null ||
                el.getAttribute('disabled') !== null ||
                el.getAttribute('aria-disabled') === 'true'
            )
            """
        )
        if disabled_or_readonly:
            print("SC debug -> skipping Report Year (disabled field)")
            return

        if not report_year:
            return

        try:
            await locator.select_option(value=report_year)
            return
        except Exception:
            pass

        try:
            await locator.select_option(label=report_year)
        except Exception:
            return
    except Exception:
        print("SC debug -> skipping Report Year (disabled field)")


async def _wait_for_upload_page(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        raise SouthCarolinaAutomationError("SC did not reach holder-upload after holder info Next.") from exc


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
    raise SouthCarolinaAutomationError(f"Could not find a clickable Next control {context}.")


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
        "online": "Online",
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
