"""New York filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

NY_HOLDER_INFO_URL = "https://ouf.osc.ny.gov/app/holder-info"


class NewYorkAutomationError(RuntimeError):
    """Raised when NY automation cannot reliably continue."""


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
    _FieldSpec("Previous Business Name", "previous_business_name"),
    _FieldSpec("Previous Business FEIN", "previous_business_fein"),
    _FieldSpec("Email Address", "email"),
    _FieldSpec("Email Address Confirmation", "email_confirmation"),
    _FieldSpec("Address 1", "address_1"),
    _FieldSpec("Address 2", "address_2"),
    _FieldSpec("City", "city"),
    _FieldSpec("ZIP Code", "zip"),
    _FieldSpec("Parent Company FEIN", "parent_company_fein"),
    _FieldSpec("Total Dollar Amount Remitted", "amount_to_remit"),
)

_SELECT_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("State", "state"),
    _FieldSpec("Country", "country"),
    _FieldSpec("Report Type", "report_type"),
    _FieldSpec("Report Year", "report_year"),
    _FieldSpec("Funds Remitted Via", "funds_remitted_via"),
)

_RADIO_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Business is active", "business_is_active"),
    _FieldSpec("on behalf of another organization", "on_behalf_of_another_org"),
    _FieldSpec("first time this business entity has filed", "first_time_filing"),
    _FieldSpec("combined file containing multiple reports", "combined_file"),
)

_FOREIGN_ADDRESS_LABEL = "Check for Foreign Address"

# US states + territories / armed forces mail regions.
STATE_MAP: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming", "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands", "PR": "Puerto Rico",
    "VI": "U.S. Virgin Islands", "UM": "U.S. Minor Outlying Islands", "FM": "Federated States of Micronesia",
    "MH": "Marshall Islands", "PW": "Palau", "AA": "Armed Forces Americas", "AE": "Armed Forces Europe",
    "AP": "Armed Forces Pacific",
}

_COUNTRY_MAP: dict[str, str] = {
    "": "United States of America",
    "US": "United States of America",
    "USA": "United States of America",
    "UNITED STATES": "United States of America",
    "UNITED STATES OF AMERICA": "United States of America",
}

_FUNDS_REMITTED_MAP: dict[str, str] = {
    "EFT": "Electronic Funds Transfer",
    "ELECTRONIC FUNDS TRANSFER": "Electronic Funds Transfer",
    "CHECK": "Check",
    "CHK": "Check",
    "NYS JOURNAL ENTRY": "NYS Journal Entry",
    "JOURNAL ENTRY": "NYS Journal Entry",
}


def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run NY workflow through upload step, then stop before signature/submit."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    if not naupa_path.exists():
        raise FileNotFoundError(f"NAUPA file not found: {naupa_path}")

    page.goto(NY_HOLDER_INFO_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    _fill_holder_info_page(page, record, errors)

    if errors:
        raise NewYorkAutomationError("NY holder-info form completed with errors:\n- " + "\n- ".join(errors))

    _click_next(page)
    _upload_naupa_file(page, naupa_path)
    _click_next(page)


run_newyork = run
run_newyork_filing = run


def _fill_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _resolve_holder_id_value(record) if field.key == "holder_id" else _as_string(record.get(field.key))
        if not value:
            continue
        _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_row_label(page, field.label, value))

    for field in _SELECT_FIELDS:
        value = _as_string(record.get(field.key))
        mapped = _map_select_value(field.key, value)
        if field.key == "country" and not mapped:
            mapped = "United States of America"
        if not mapped:
            continue
        _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_row_label(page, field.label, mapped))

    for field in _RADIO_FIELDS:
        bool_value = _as_bool(record.get(field.key))
        if bool_value is None:
            continue
        _guarded(errors, f"radio '{field.label}'", lambda: _set_yes_no_radio_by_row_label(page, field.label, bool_value))

    foreign_address = _as_bool(record.get("foreign_address"))
    if foreign_address:
        _guarded(
            errors,
            f"checkbox '{_FOREIGN_ADDRESS_LABEL}'",
            lambda: _set_checkbox_by_row_label(page, _FOREIGN_ADDRESS_LABEL, should_check=True),
        )


def _resolve_holder_id_value(record: Dict[str, Any]) -> str:
    return _as_string(record.get("holder_id"))


def _upload_naupa_file(page: Page, file_path: Path) -> None:
    file_input = page.locator("input[type='file']").first
    try:
        file_input.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        raise NewYorkAutomationError("Could not find visible file upload input on NY upload page.") from exc

    _debug_action("Upload File", str(file_path), "input[type='file']")
    file_input.set_input_files(str(file_path))


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
        _debug_action("Click Next", "Next", "button/input:Next")
        target.click(timeout=10_000)
        page.wait_for_timeout(1000)
        return
    raise NewYorkAutomationError("Could not find a clickable 'Next' control on NY page.")


def _fill_text_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_field_row(page, label_text)
    input_locator = row.locator(
        "input[type='text'], input[type='tel'], input[type='email'], input:not([type]), textarea"
    ).first
    if input_locator.count() == 0:
        raise NewYorkAutomationError(f"Text input not found for exact label '{label_text}'.")

    row_info = _row_debug_descriptor(row)
    _debug_action(label_text, value, f"row={row_info} -> text_input")
    input_locator.scroll_into_view_if_needed()
    input_locator.fill(value)


def _select_dropdown_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_field_row(page, label_text)
    select_locator = row.locator("select").first
    if select_locator.count() == 0:
        raise NewYorkAutomationError(f"Dropdown/select not found for exact label '{label_text}'.")

    row_info = _row_debug_descriptor(row)
    _debug_action(label_text, value, f"row={row_info} -> select")
    select_locator.scroll_into_view_if_needed()
    try:
        select_locator.select_option(label=value)
    except Exception as exc:
        raise NewYorkAutomationError(f"Unable to select option label '{value}' for dropdown '{label_text}'.") from exc


def _set_yes_no_radio_by_row_label(page: Page, label_text: str, yes_value: bool) -> None:
    row = _find_field_row(page, label_text)
    radios = row.locator("input[type='radio']")
    if radios.count() == 0:
        raise NewYorkAutomationError(f"Radio inputs not found for exact label '{label_text}'.")

    target = _pick_radio_by_semantics(radios, yes_value)
    row_info = _row_debug_descriptor(row)
    _debug_action(label_text, str(yes_value), f"row={row_info} -> radio")

    if _click_radio_label_for_input(row, target):
        return
    target.set_checked(True, force=True)


def _set_checkbox_by_row_label(page: Page, label_text: str, should_check: bool) -> None:
    row = _find_field_row(page, label_text)
    checkbox = row.locator("input[type='checkbox']").first
    if checkbox.count() == 0:
        raise NewYorkAutomationError(f"Checkbox not found for exact label '{label_text}'.")

    row_info = _row_debug_descriptor(row)
    _debug_action(label_text, str(should_check), f"row={row_info} -> checkbox")
    checkbox.set_checked(should_check, force=True)


def _find_field_row(page: Page, label_text: str) -> Locator:
    label = _find_exact_label_node(page, label_text)

    # Strict nearest row wrappers; stop at first visible ancestor with controls.
    row_candidates = (
        label.locator("xpath=ancestor::*[contains(@class,'row') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::*[contains(@class,'form-group') and (.//input or .//select or .//textarea)][1]").first,
        label.locator("xpath=ancestor::div[.//input or .//select or .//textarea][1]").first,
    )

    for candidate in row_candidates:
        if candidate.count() > 0 and candidate.is_visible():
            return candidate

    raise NewYorkAutomationError(f"Could not find strict row container for exact label '{label_text}'.")


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

    # Fallback for labels with parent wrappers but exact normalized full text on wrapper.
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

    raise NewYorkAutomationError(f"Could not find visible exact label node for '{label_text}'.")


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


def _row_debug_descriptor(row: Locator) -> str:
    try:
        html = row.evaluate("el => el.outerHTML.slice(0, 180)")
        return str(html).replace("\n", " ")
    except Exception:
        return "<row_html_unavailable>"


def _debug_action(field_name: str, value: str, selector_info: str) -> None:
    print(f"NY debug -> field='{field_name}' value='{value}' target='{selector_info}'")


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


def _map_select_value(field_key: str, value: str) -> str:
    if not value:
        return ""
    raw = value.strip()
    upper = raw.upper()
    if field_key == "state":
        return STATE_MAP.get(upper, raw)
    if field_key == "country":
        return _COUNTRY_MAP.get(upper, raw)
    if field_key == "funds_remitted_via":
        return _FUNDS_REMITTED_MAP.get(upper, raw)
    return raw


def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        action()
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"NY automation warning: {message}")
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
