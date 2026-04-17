"""California filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

CA_HOLDER_INFO_URL = "https://claimit.ca.gov/app/holder-info"


class CaliforniaAutomationError(RuntimeError):
    """Raised when CA automation cannot reliably continue."""


@dataclass(frozen=True)
class _FieldSpec:
    label: str
    key: str


_TEXT_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Holder Name", "holder_name"),
    _FieldSpec("Holder Tax ID", "holder_tax_id"),
    _FieldSpec("Holder ID", "holder_id"),
    _FieldSpec("Contact Name", "contact_name"),
    _FieldSpec("Contact Phone Number", "contact_phone"),
    _FieldSpec("Phone Extension", "phone_extension"),
)

_EMAIL_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Email Address", "email"),
    _FieldSpec("Email Address Confirmation", "email"),
)

_DROPDOWN_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Report Type", "ca_report_type"),
    _FieldSpec("Submission Type", "ca_submission_type"),
    _FieldSpec("Report Year", "ca_report_year"),
    _FieldSpec("Fiscal Year End", "ca_fiscal_year_end_month"),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run CA workflow through upload step and stop before signature/submit."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    print(f"Starting CA navigation to {CA_HOLDER_INFO_URL}")
    print(f"CA NAUPA path: {naupa_path}")
    print(f"CA NAUPA exists: {'yes' if naupa_path.exists() else 'no'}")

    await page.goto(CA_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    print("CA form filling begins")
    errors: list[str] = []
    await _fill_ca_holder_info_page(page, record, errors)

    if errors:
        raise CaliforniaAutomationError("CA holder-info form completed with errors:\n- " + "\n- ".join(errors))

    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)

    if naupa_path.exists():
        await _click_next(page)


run_california = run
run_california_filing = run


async def _fill_ca_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get("holder_id")) if field.key == "holder_id" else _as_string(record.get(field.key))
        if not value:
            continue
        _debug_field(field.label, value)
        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_row_label(page, field.label, value))

    email_value = record.get("email")
    print("Filling Email fields with:", email_value)
    if email_value:
        for field in _EMAIL_FIELDS:
            await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_row_label(page, field.label, email_value))

    for field in _DROPDOWN_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            continue
        _debug_field(field.label, value)
        await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_row_label(page, field.label, value))

    submission_type = _as_string(record.get("ca_submission_type"))
    is_remit_report = submission_type.lower() == "remit report"

    if is_remit_report:
        remit_report_id = _as_string(record.get("ca_remit_report_id"))
        funds_remitted_via = _as_string(record.get("ca_funds_remitted_via"))

        if remit_report_id:
            _debug_field("Remit Report ID", remit_report_id)
            await _guarded(errors, "text 'Remit Report ID'", lambda: _fill_text_by_row_label(page, "Remit Report ID", remit_report_id))

        if funds_remitted_via:
            _debug_field("Funds Remitted Via", funds_remitted_via)
            await _guarded(
                errors,
                "dropdown 'Funds Remitted Via'",
                lambda: _select_dropdown_by_row_label(page, "Funds Remitted Via", funds_remitted_via),
            )

    negative_report = _as_bool(record.get("ca_negative_report"))
    if negative_report is not None:
        _debug_field("This is a Negative Report", str(negative_report))
        await _guarded(
            errors,
            "radio 'This is a Negative Report'",
            lambda: _set_yes_no_radio_by_row_label(page, "This is a Negative Report", negative_report),
        )

    safe_deposit = _as_bool(record.get("ca_safe_deposit_box"))
    if safe_deposit is not None:
        _debug_field("Includes Safe Deposit Box", str(safe_deposit))
        await _guarded(
            errors,
            "radio 'Includes Safe Deposit Box'",
            lambda: _set_yes_no_radio_by_row_label(page, "Includes Safe Deposit Box", safe_deposit),
        )

    total_cash = _as_string(record.get("ca_total_cash"))
    total_shares = _as_string(record.get("ca_total_shares"))

    if negative_report is False and not total_cash:
        errors.append("ca_total_cash is required when ca_negative_report is No.")

    cash_label = "Total Cash Remitted" if is_remit_report else "Total Cash Reported"
    shares_label = "Total Shares Remitted" if is_remit_report else "Total Shares Reported"

    if total_cash:
        _debug_field(cash_label, total_cash)
        await _guarded(errors, f"text '{cash_label}'", lambda: _fill_text_by_row_label(page, cash_label, total_cash))

    if total_shares:
        _debug_field(shares_label, total_shares)
        await _guarded(errors, f"text '{shares_label}'", lambda: _fill_text_by_row_label(page, shares_label, total_shares))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    print(f"Using NAUPA file: {file_path}")

    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    print("CA reached upload page")
    print(f"CA NAUPA exists: {'yes' if file_path.exists() else 'no'}")

    if not file_path.exists():
        print("CA warning: NAUPA file not found, skipping upload and leaving tab open for manual action.")
        print("CA upload skipped due to missing file")
        return

    file_inputs = page.locator("input[type='file']")
    found_before_click = (await file_inputs.count()) > 0
    print(f"Found CA upload input before clicking ADD DOCUMENT: {'yes' if found_before_click else 'no'}")

    if found_before_click:
        print("Using direct set_input_files without opening file picker.")
        await file_inputs.first.set_input_files(str(file_path))
        print("Upload complete.")
        await page.wait_for_timeout(1200)
        return

    clicked_add_document = await _click_add_document_if_present(page)
    print(f"Clicked ADD DOCUMENT as fallback: {'yes' if clicked_add_document else 'no'}")

    file_inputs = page.locator("input[type='file']")
    found_after_click = (await file_inputs.count()) > 0
    print(f"Found CA upload input after fallback click: {'yes' if found_after_click else 'no'}")

    if not found_after_click:
        raise CaliforniaAutomationError("Could not find CA upload file input (input[type='file']).")

    print("Uploading with fallback flow.")
    await file_inputs.first.set_input_files(str(file_path))
    print("Upload complete.")
    await page.wait_for_timeout(1200)


async def _click_add_document_if_present(page: Page) -> bool:
    candidates = (
        page.get_by_role("button", name="ADD DOCUMENT", exact=False),
        page.locator("button:has-text('ADD DOCUMENT')"),
        page.locator("text=ADD DOCUMENT").locator("xpath=ancestor::button[1]"),
    )

    for candidate in candidates:
        if await candidate.count() <= 0:
            continue

        button = candidate.first
        if not await button.is_visible() or not await button.is_enabled():
            continue

        await button.click(timeout=10_000)
        await page.wait_for_timeout(500)
        return True

    return False


async def _click_next(page: Page) -> None:
    candidates = (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("text=Next").locator("xpath=ancestor::button[1]"),
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
    raise CaliforniaAutomationError("Could not find a clickable 'Next' control on CA page.")


async def _fill_text_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = await _find_field_row(page, label_text)
    input_locator = row.locator(
        "input[type='text'], input[type='tel'], input[type='email'], input[type='number'], input:not([type]), textarea"
    ).first
    if await input_locator.count() == 0:
        raise CaliforniaAutomationError(f"Text input not found for label '{label_text}'.")

    await input_locator.scroll_into_view_if_needed()
    await input_locator.fill(value)


async def _select_dropdown_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = await _find_field_row(page, label_text)
    select_locator = row.locator("select").first
    if await select_locator.count() == 0:
        raise CaliforniaAutomationError(f"Dropdown/select not found for label '{label_text}'.")

    await select_locator.scroll_into_view_if_needed()
    try:
        await select_locator.select_option(label=value)
    except Exception as exc:
        raise CaliforniaAutomationError(f"Unable to select option label '{value}' for dropdown '{label_text}'.") from exc


async def _set_yes_no_radio_by_row_label(page: Page, label_text: str, yes_value: bool) -> None:
    row = await _find_field_row(page, label_text)
    radios = row.locator("input[type='radio']")
    if await radios.count() == 0:
        raise CaliforniaAutomationError(f"Radio inputs not found for label '{label_text}'.")

    target = await _pick_radio_by_semantics(radios, yes_value)
    if await _click_radio_label_for_input(row, target):
        return
    await target.set_checked(True, force=True)


async def _find_field_row(page: Page, label_text: str) -> Locator:
    label = await _find_label_anchor(page, label_text)

    row_candidates = (
        label.locator("xpath=ancestor::*[contains(@class,'row') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::*[contains(@class,'form-group') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::div[.//input or .//select or .//textarea][1]").first,
        label.locator("xpath=ancestor::*[.//input or .//select or .//textarea][1]").first,
    )

    for candidate in row_candidates:
        if await candidate.count() > 0 and await candidate.is_visible():
            return candidate

    raise CaliforniaAutomationError(f"Could not find row/container for label '{label_text}'.")


async def _find_label_anchor(page: Page, label_text: str) -> Locator:
    target = _normalize_label(label_text)
    token = target.split()[0] if target else ""

    full_xpath = (
        "xpath=//*[normalize-space(string(.))!='' and "
        "contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), "
        f"'{target}')]"
    )
    full_matches = page.locator(full_xpath)
    full_count = await full_matches.count()
    for i in range(full_count):
        node = full_matches.nth(i)
        if await node.is_visible():
            return node

    if token:
        token_xpath = (
            "xpath=//*[normalize-space(string(.))!='' and "
            "contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), "
            f"'{token}')]"
        )
        token_matches = page.locator(token_xpath)
        token_count = await token_matches.count()
        for i in range(token_count):
            node = token_matches.nth(i)
            if await node.is_visible():
                return node

    raise CaliforniaAutomationError(f"Could not find visible label anchor for '{label_text}'.")


async def _pick_radio_by_semantics(radios: Locator, yes_value: bool) -> Locator:
    desired = ("yes", "true", "1") if yes_value else ("no", "false", "0")
    count = await radios.count()
    for i in range(count):
        radio = radios.nth(i)
        tokens = " ".join([
            await _safe_attr(radio, "value"),
            await _safe_attr(radio, "id"),
            await _safe_attr(radio, "name"),
            await _safe_attr(radio, "aria-label"),
        ]).lower()
        if any(token in tokens for token in desired):
            return radio

    index = 0 if yes_value else min(1, count - 1)
    return radios.nth(index)


async def _click_radio_label_for_input(row: Locator, radio: Locator) -> bool:
    radio_id = await _safe_attr(radio, "id")
    if radio_id:
        by_for = row.locator(f"label[for='{radio_id}']").first
        if await by_for.count() > 0 and await by_for.is_visible():
            await by_for.click(force=True)
            return True

    sibling_label = radio.locator("xpath=following-sibling::label[1]").first
    if await sibling_label.count() > 0 and await sibling_label.is_visible():
        await sibling_label.click(force=True)
        return True

    parent_label = radio.locator("xpath=ancestor::label[1]").first
    if await parent_label.count() > 0 and await parent_label.is_visible():
        await parent_label.click(force=True)
        return True

    return False


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


def _debug_field(field_name: str, value: str) -> None:
    print(f"CA debug -> field='{field_name}' value='{value}'")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"CA automation warning: {message}")
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


async def _safe_attr(locator: Locator, attr_name: str) -> str:
    value = await locator.get_attribute(attr_name)
    return "" if value is None else value
