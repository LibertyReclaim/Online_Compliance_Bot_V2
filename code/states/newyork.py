"""New York filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

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


async def run(
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

    await page.goto(NY_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_holder_info_page(page, record, errors)

    if errors:
        raise NewYorkAutomationError("NY holder-info form completed with errors:\n- " + "\n- ".join(errors))

    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_newyork = run
run_newyork_filing = run


async def _fill_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        if field.key == "holder_id":
            value = _resolve_holder_id_value(record)
            print(f"internal id: {_as_string(record.get('id'))}")
            print(f"holder_id: {_as_string(record.get('holder_id'))}")
            print(f"filling Holder ID with: {value}")
        elif field.key in {"email", "email_confirmation"}:
            value = record.get("email")
            print("Filling Email fields with:", value)
        else:
            value = _as_string(record.get(field.key))

        if not value:
            continue

        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))

    for field in _SELECT_FIELDS:
        value = _as_string(record.get(field.key))
        mapped = _map_select_value(field.key, value)
        if field.key == "country" and not mapped:
            mapped = "United States of America"
        if not mapped:
            continue
        await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_label(page, field.label, mapped))

    for field in _RADIO_FIELDS:
        bool_value = _as_bool(record.get(field.key))
        if bool_value is None:
            continue
        await _guarded(errors, f"radio '{field.label}'", lambda: _set_yes_no_radio_by_label(page, field.label, bool_value))

    foreign_address = _as_bool(record.get("foreign_address"))
    if foreign_address:
        await _guarded(
            errors,
            f"checkbox '{_FOREIGN_ADDRESS_LABEL}'",
            lambda: _set_checkbox_by_label(page, _FOREIGN_ADDRESS_LABEL, should_check=True),
        )


def _resolve_holder_id_value(record: Dict[str, Any]) -> str:
    return _as_string(record.get("holder_id"))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    print(f"Using NAUPA file: {file_path}")

    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        # Fallback: upload page may still render controls before URL settles.
        await page.wait_for_timeout(1500)

    file_inputs = page.locator("input[type='file']")
    found_before_click = (await file_inputs.count()) > 0
    print(f"Found NY upload input before clicking ADD DOCUMENT: {'yes' if found_before_click else 'no'}")

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
    print(f"Found NY upload input after fallback click: {'yes' if found_after_click else 'no'}")

    if not found_after_click:
        raise NewYorkAutomationError("Could not find NY upload file input (input[type='file']).")

    print("Uploading with fallback flow.")
    await file_inputs.first.set_input_files(str(file_path))
    print("Upload complete.")

    # Let the page register the uploaded document before continuing.
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
        _debug_action("Click Next", "Next", "button/input:Next")
        await target.click(timeout=10_000)
        await page.wait_for_timeout(1000)
        return
    raise NewYorkAutomationError("Could not find a clickable 'Next' control on NY page.")


_TEXT_CONTROL_SELECTOR = "input[type='text'], input:not([type='hidden']), textarea"
_SELECT_CONTROL_SELECTOR = "select"
_RADIO_CONTROL_SELECTOR = "input[type='radio']"
_CHECKBOX_CONTROL_SELECTOR = "input[type='checkbox']"


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    locator, strategy = await _resolve_control(page, label_text, _TEXT_CONTROL_SELECTOR, "text")
    await locator.scroll_into_view_if_needed()
    await locator.fill(value)
    _log_field_success(label_text, strategy)


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    locator, strategy = await _resolve_control(page, label_text, _SELECT_CONTROL_SELECTOR, "select")
    await locator.scroll_into_view_if_needed()
    try:
        await locator.select_option(label=value)
    except Exception as exc:
        raise NewYorkAutomationError(f"Unable to select option label '{value}' for dropdown '{label_text}'.") from exc
    _log_field_success(label_text, strategy)


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    radios, strategy = await _resolve_control_collection(page, label_text, _RADIO_CONTROL_SELECTOR, "radio")
    target = await _pick_radio_by_semantics(radios, yes_value)
    row = target.locator("xpath=ancestor::*[self::div or self::tr or self::td][1]")
    if await _click_radio_label_for_input(row, target):
        _log_field_success(label_text, strategy)
        return
    await target.set_checked(True, force=True)
    _log_field_success(label_text, strategy)


async def _set_checkbox_by_label(page: Page, label_text: str, should_check: bool) -> None:
    locator, strategy = await _resolve_control(page, label_text, _CHECKBOX_CONTROL_SELECTOR, "checkbox")
    await locator.set_checked(should_check, force=True)
    _log_field_success(label_text, strategy)


async def _resolve_control(page: Page, label_text: str, control_selector: str, field_kind: str) -> tuple[Locator, str]:
    controls, strategy = await _resolve_control_collection(page, label_text, control_selector, field_kind)
    return controls.first, strategy


async def _resolve_control_collection(
    page: Page,
    label_text: str,
    control_selector: str,
    field_kind: str,
) -> tuple[Locator, str]:
    normalized = _normalize_label(label_text)

    row_controls = await _try_row_strategy(page, label_text, normalized, control_selector)
    if row_controls is not None:
        return row_controls, "xpath row"

    by_label_controls = await _try_get_by_label_strategy(page, label_text, normalized, control_selector)
    if by_label_controls is not None:
        return by_label_controls, "get_by_label fallback"

    xpath_controls = await _try_following_xpath_strategy(page, label_text, normalized, control_selector)
    if xpath_controls is not None:
        return xpath_controls, "xpath fallback"

    reason = f"Unable to locate {field_kind} control for label '{label_text}' with all strategies."
    _log_field_failure(label_text, normalized, reason)
    raise NewYorkAutomationError(reason)


async def _try_row_strategy(page: Page, label_text: str, normalized: str, control_selector: str) -> Optional[Locator]:
    anchors = await _find_label_anchors(page, normalized)
    if not anchors:
        _log_field_failure(label_text, normalized, "row strategy: no matching label anchor found")
        return None

    for anchor in anchors:
        row_candidates = (
            anchor.locator("xpath=ancestor::*[contains(@class,'row')][1]").first,
            anchor.locator("xpath=ancestor::*[contains(@class,'form-group')][1]").first,
            anchor.locator("xpath=ancestor::tr[1]").first,
            anchor.locator("xpath=ancestor::td[1]").first,
            anchor.locator("xpath=ancestor::div[1]").first,
        )

        for row in row_candidates:
            if await row.count() == 0:
                continue
            controls = row.locator(control_selector)
            if await controls.count() > 0:
                return controls

    _log_field_failure(label_text, normalized, "row strategy: anchor found but no control in nearby row/container")
    return None


async def _try_get_by_label_strategy(
    page: Page,
    label_text: str,
    normalized: str,
    control_selector: str,
) -> Optional[Locator]:
    candidates = (label_text, normalized)
    for candidate in candidates:
        label_match = page.get_by_label(candidate, exact=False)
        if await label_match.count() <= 0:
            continue
        control_match = label_match.locator("xpath=self::input|self::textarea|self::select").first
        if await control_match.count() > 0:
            return control_match
        descendants = label_match.locator(control_selector)
        if await descendants.count() > 0:
            return descendants

    _log_field_failure(label_text, normalized, "get_by_label fallback: no matching controls")
    return None


async def _try_following_xpath_strategy(
    page: Page,
    label_text: str,
    normalized: str,
    control_selector: str,
) -> Optional[Locator]:
    escaped = _xpath_literal(normalized)
    anchor_xpath = (
        "//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{escaped})]"
    )

    controls_xpath = _selector_to_xpath_for_following(control_selector)
    if not controls_xpath:
        return None

    locator = page.locator(f"xpath=({anchor_xpath})[1]/following::{controls_xpath}[1]")
    if await locator.count() > 0:
        return locator

    _log_field_failure(label_text, normalized, "xpath fallback: no following control found")
    return None


async def _find_label_anchors(page: Page, normalized_label: str) -> list[Locator]:
    escaped = _xpath_literal(normalized_label)
    xpath = (
        "xpath=//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{escaped})]"
    )
    candidates = page.locator(xpath)
    count = await candidates.count()
    visible: list[Locator] = []
    for index in range(count):
        node = candidates.nth(index)
        if await node.is_visible():
            visible.append(node)
    return visible


def _selector_to_xpath_for_following(control_selector: str) -> str:
    if control_selector == _TEXT_CONTROL_SELECTOR:
        return "input[not(@type='hidden')] | textarea"
    if control_selector == _SELECT_CONTROL_SELECTOR:
        return "select"
    if control_selector == _RADIO_CONTROL_SELECTOR:
        return "input[@type='radio']"
    if control_selector == _CHECKBOX_CONTROL_SELECTOR:
        return "input[@type='checkbox']"
    return ""


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


def _log_field_success(label_text: str, strategy: str) -> None:
    normalized = _normalize_label(label_text)
    print(
        f"NY debug -> field='{label_text}' label='{label_text}' normalized='{normalized}' strategy='{strategy}'"
    )


def _log_field_failure(label_text: str, normalized: str, reason: str) -> None:
    print(
        f"NY debug -> field='{label_text}' label='{label_text}' normalized='{normalized}' failure='{reason}'"
    )


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


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
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


async def _safe_attr(locator: Locator, attr_name: str) -> str:
    value = await locator.get_attribute(attr_name)
    return "" if value is None else value
