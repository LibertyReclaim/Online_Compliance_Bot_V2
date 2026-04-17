"""California filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

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

# Email + confirmation both from email column per spec.
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

_RADIO_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("This is a Negative Report", "ca_negative_report"),
    _FieldSpec("Includes Safe Deposit Box", "ca_safe_deposit_box"),
)


def run(
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

    if not naupa_path.exists():
        raise FileNotFoundError(f"NAUPA file not found: {naupa_path}")

    page.goto(CA_HOLDER_INFO_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    _fill_ca_holder_info_page(page, record, errors)

    if errors:
        raise CaliforniaAutomationError("CA holder-info form completed with errors:\n- " + "\n- ".join(errors))

    _click_next(page)
    _upload_naupa_file(page, naupa_path)
    _click_next(page)


run_california = run
run_california_filing = run


def _fill_ca_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        # Never use internal id for Holder ID field.
        value = _as_string(record.get("holder_id")) if field.key == "holder_id" else _as_string(record.get(field.key))
        if not value:
            continue
        _debug_field(field.label, value)
        _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_row_label(page, field.label, value))

    email_value = record.get("email")
    print("Filling Email fields with:", email_value)
    if email_value:
        for field in _EMAIL_FIELDS:
            _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_row_label(page, field.label, email_value))

    for field in _DROPDOWN_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            continue
        _debug_field(field.label, value)
        _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_row_label(page, field.label, value))

    submission_type = _as_string(record.get("ca_submission_type"))
    is_remit_report = submission_type.lower() == "remit report"

    # Conditional fields based on submission type.
    if is_remit_report:
        remit_report_id = _as_string(record.get("ca_remit_report_id"))
        funds_remitted_via = _as_string(record.get("ca_funds_remitted_via"))

        if remit_report_id:
            _debug_field("Remit Report ID", remit_report_id)
            _guarded(errors, "text 'Remit Report ID'", lambda: _fill_text_by_row_label(page, "Remit Report ID", remit_report_id))

        if funds_remitted_via:
            _debug_field("Funds Remitted Via", funds_remitted_via)
            _guarded(
                errors,
                "dropdown 'Funds Remitted Via'",
                lambda: _select_dropdown_by_row_label(page, "Funds Remitted Via", funds_remitted_via),
            )

    negative_report = _as_bool(record.get("ca_negative_report"))
    if negative_report is not None:
        _debug_field("This is a Negative Report", str(negative_report))
        _guarded(
            errors,
            "radio 'This is a Negative Report'",
            lambda: _set_yes_no_radio_by_row_label(page, "This is a Negative Report", negative_report),
        )

    safe_deposit = _as_bool(record.get("ca_safe_deposit_box"))
    if safe_deposit is not None:
        _debug_field("Includes Safe Deposit Box", str(safe_deposit))
        _guarded(
            errors,
            "radio 'Includes Safe Deposit Box'",
            lambda: _set_yes_no_radio_by_row_label(page, "Includes Safe Deposit Box", safe_deposit),
        )

    total_cash = _as_string(record.get("ca_total_cash"))
    total_shares = _as_string(record.get("ca_total_shares"))

    # Validation rule: if negative report is explicitly No, cash is required.
    if negative_report is False and not total_cash:
        errors.append("ca_total_cash is required when ca_negative_report is No.")

    cash_label = "Total Cash Remitted" if is_remit_report else "Total Cash Reported"
    shares_label = "Total Shares Remitted" if is_remit_report else "Total Shares Reported"

    if total_cash:
        _debug_field(cash_label, total_cash)
        _guarded(errors, f"text '{cash_label}'", lambda: _fill_text_by_row_label(page, cash_label, total_cash))

    if total_shares:
        _debug_field(shares_label, total_shares)
        _guarded(errors, f"text '{shares_label}'", lambda: _fill_text_by_row_label(page, shares_label, total_shares))


def _upload_naupa_file(page: Page, file_path: Path) -> None:
    print(f"Using NAUPA file: {file_path}")

    try:
        page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(1500)

    print("CA reached upload page.")

    if not file_path.exists():
        print("CA warning: NAUPA file not found, skipping upload and leaving tab open for manual action.")
        print("CA upload skipped due to missing file.")
        return

    file_inputs = page.locator("input[type='file']")
    found_before_click = file_inputs.count() > 0
    print(f"Found CA upload input before clicking ADD DOCUMENT: {'yes' if found_before_click else 'no'}")

    if found_before_click:
        print("Using direct set_input_files without opening file picker.")
        file_inputs.first.set_input_files(str(file_path))
        print("Upload complete.")
        page.wait_for_timeout(1200)
        return

    clicked_add_document = _click_add_document_if_present(page)
    print(f"Clicked ADD DOCUMENT as fallback: {'yes' if clicked_add_document else 'no'}")

    file_inputs = page.locator("input[type='file']")
    found_after_click = file_inputs.count() > 0
    print(f"Found CA upload input after fallback click: {'yes' if found_after_click else 'no'}")

    if not found_after_click:
        raise CaliforniaAutomationError("Could not find CA upload file input (input[type='file']).")

    print("Uploading with fallback flow.")
    file_inputs.first.set_input_files(str(file_path))
    print("Upload complete.")
    page.wait_for_timeout(1200)


def _click_add_document_if_present(page: Page) -> bool:
    candidates = (
        page.get_by_role("button", name="ADD DOCUMENT", exact=False),
        page.locator("button:has-text('ADD DOCUMENT')"),
        page.locator("text=ADD DOCUMENT").locator("xpath=ancestor::button[1]"),
    )

    for candidate in candidates:
        if candidate.count() <= 0:
            continue

        button = candidate.first
        if not button.is_visible() or not button.is_enabled():
            continue

        button.click(timeout=10_000)
        page.wait_for_timeout(500)
        return True

    return False


def _click_next(page: Page) -> None:
    candidates = (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("text=Next").locator("xpath=ancestor::button[1]"),
    )
    for candidate in candidates:
        if candidate.count() <= 0:
            continue
        target = candidate.first
        if not target.is_enabled():
            continue
        target.click(timeout=10_000)
        page.wait_for_timeout(1000)
        return
    raise CaliforniaAutomationError("Could not find a clickable 'Next' control on CA page.")


def _fill_text_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_field_row(page, label_text)
    input_locator = row.locator(
        "input[type='text'], input[type='tel'], input[type='email'], input[type='number'], input:not([type]), textarea"
    ).first
    if input_locator.count() == 0:
        raise CaliforniaAutomationError(f"Text input not found for exact label '{label_text}'.")

    input_locator.scroll_into_view_if_needed()
    input_locator.fill(value)


def _select_dropdown_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_field_row(page, label_text)
    select_locator = row.locator("select").first
    if select_locator.count() == 0:
        raise CaliforniaAutomationError(f"Dropdown/select not found for exact label '{label_text}'.")

    select_locator.scroll_into_view_if_needed()
    try:
        select_locator.select_option(label=value)
    except Exception as exc:
        raise CaliforniaAutomationError(f"Unable to select option label '{value}' for dropdown '{label_text}'.") from exc


def _set_yes_no_radio_by_row_label(page: Page, label_text: str, yes_value: bool) -> None:
    row = _find_field_row(page, label_text)
    radios = row.locator("input[type='radio']")
    if radios.count() == 0:
        raise CaliforniaAutomationError(f"Radio inputs not found for exact label '{label_text}'.")

    target = _pick_radio_by_semantics(radios, yes_value)
    if _click_radio_label_for_input(row, target):
        return
    target.set_checked(True, force=True)


def _find_field_row(page: Page, label_text: str) -> Locator:
    label = _find_exact_label_node(page, label_text)

    row_candidates = (
        label.locator("xpath=ancestor::*[contains(@class,'row') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::*[contains(@class,'form-group') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::div[.//input or .//select or .//textarea][1]").first,
    )

    for candidate in row_candidates:
        if candidate.count() > 0 and candidate.is_visible():
            return candidate

    raise CaliforniaAutomationError(f"Could not find strict row container for exact label '{label_text}'.")


def _find_exact_label_node(page: Page, label_text: str) -> Locator:
    target = _normalize_label(label_text)
    xpath = (
        "xpath=//*[normalize-space(text())!='' and "
        "translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  ')="
        f"'{target}']"
    )

    matches = page.locator(xpath)
    for i in range(matches.count()):
        node = matches.nth(i)
        if node.is_visible():
            return node

    wrapper_xpath = (
        "xpath=//*[normalize-space(string(.))!='' and "
        "translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  ')="
        f"'{target}' and not(*)]"
    )
    wrappers = page.locator(wrapper_xpath)
    for i in range(wrappers.count()):
        node = wrappers.nth(i)
        if node.is_visible():
            return node

    raise CaliforniaAutomationError(f"Could not find visible exact label node for '{label_text}'.")


def _pick_radio_by_semantics(radios: Locator, yes_value: bool) -> Locator:
    desired = ("yes", "true", "1") if yes_value else ("no", "false", "0")
    for i in range(radios.count()):
        radio = radios.nth(i)
        tokens = " ".join([
            _safe_attr(radio, "value"),
            _safe_attr(radio, "id"),
            _safe_attr(radio, "name"),
            _safe_attr(radio, "aria-label"),
        ]).lower()
        if any(token in tokens for token in desired):
            return radio

    index = 0 if yes_value else min(1, radios.count() - 1)
    return radios.nth(index)


def _click_radio_label_for_input(row: Locator, radio: Locator) -> bool:
    radio_id = _safe_attr(radio, "id")
    if radio_id:
        by_for = row.locator(f"label[for='{radio_id}']").first
        if by_for.count() > 0 and by_for.is_visible():
            by_for.click(force=True)
            return True

    sibling_label = radio.locator("xpath=following-sibling::label[1]").first
    if sibling_label.count() > 0 and sibling_label.is_visible():
        sibling_label.click(force=True)
        return True

    parent_label = radio.locator("xpath=ancestor::label[1]").first
    if parent_label.count() > 0 and parent_label.is_visible():
        parent_label.click(force=True)
        return True

    return False


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


def _debug_field(field_name: str, value: str) -> None:
    print(f"CA debug -> field='{field_name}' value='{value}'")


def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        action()
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


def _safe_attr(locator: Locator, attr_name: str) -> str:
    value = locator.get_attribute(attr_name)
    return "" if value is None else value
