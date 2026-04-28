"""New York filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, select_dropdown_field, set_checkbox_field, set_radio_field

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

_ALL_FIELD_LABELS: tuple[str, ...] = tuple({
    *[f.label for f in _TEXT_FIELDS],
    *[f.label for f in _SELECT_FIELDS],
    *[f.label for f in _RADIO_FIELDS],
    _FOREIGN_ADDRESS_LABEL,
})


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
        value = _resolve_holder_id_value(record) if field.key == "holder_id" else _as_string(record.get(field.key))
        if field.key in {"email", "email_confirmation"}:
            value = _as_string(record.get("email"))
        if not value:
            continue
        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))

    for field in _SELECT_FIELDS:
        value = _map_select_value(field.key, _as_string(record.get(field.key)))
        if field.key == "country" and not value:
            value = "United States of America"
        if not value:
            continue
        await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_label(page, field.label, value))

    for field in _RADIO_FIELDS:
        bool_value = _as_bool(record.get(field.key))
        if bool_value is None:
            continue
        await _guarded(errors, f"radio '{field.label}'", lambda: _set_yes_no_radio_by_label(page, field.label, bool_value))

    if _as_bool(record.get("foreign_address")):
        await _guarded(errors, f"checkbox '{_FOREIGN_ADDRESS_LABEL}'", lambda: _set_checkbox_by_label(page, _FOREIGN_ADDRESS_LABEL, True))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

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
            print(f"NY debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"NY debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"NY debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    upload_buttons = page.get_by_role("button", name="Upload", exact=False)
    if await upload_buttons.count() > 0:
        await upload_buttons.first.click()
        await page.wait_for_timeout(500)
        locator = page.locator("input[type='file']")
        if await locator.count() > 0:
            await locator.first.set_input_files(str(file_path))
            await page.wait_for_timeout(1200)
            return

    raise NewYorkAutomationError("Could not find NY upload file input. Attempted selectors: input[type='file'], input[type='file']:visible, input[accept], input[type='file'][accept].")


async def _click_add_document_if_present(page: Page) -> bool:
    for candidate in (
        page.get_by_role("button", name="ADD DOCUMENT", exact=False),
        page.locator("button:has-text('ADD DOCUMENT')"),
        page.locator("text=ADD DOCUMENT").locator("xpath=ancestor::button[1]"),
    ):
        if await candidate.count() <= 0:
            continue
        target = candidate.first
        if await target.is_visible() and await target.is_enabled():
            await target.click(timeout=10_000)
            await page.wait_for_timeout(500)
            return True
    return False


async def _click_next(page: Page) -> None:
    for candidate in (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("text=Next").locator("xpath=ancestor::button[1]"),
    ):
        if await candidate.count() <= 0:
            continue
        target = candidate.first
        if await target.is_enabled():
            await target.click(timeout=10_000)
            await page.wait_for_timeout(1000)
            return
    raise NewYorkAutomationError("Could not find a clickable 'Next' control on NY page.")


_TEXT_SELECTOR = "input[type='text'], input:not([type='hidden']), textarea"
_SELECT_SELECTOR = "select"
_RADIO_SELECTOR = "input[type='radio']"
_CHECKBOX_SELECTOR = "input[type='checkbox']"
_ALL_CONTROLS_SELECTOR = "input:not([type='hidden']), select, textarea"


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "NY")


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    await select_dropdown_field(page, label_text, value, "NY")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "NY")


async def _set_checkbox_by_label(page: Page, label_text: str, should_check: bool) -> None:
    await set_checkbox_field(page, label_text, should_check, "NY")


async def _resolve_nearest_control(
    page: Page,
    field_label: str,
    selector: str,
    field_kind: str,
) -> tuple[Locator, str, int, str]:
    controls, matched, count, strategy = await _resolve_nearest_control_collection(page, field_label, selector, field_kind)
    return controls.first, matched, count, strategy


async def _resolve_nearest_control_collection(
    page: Page,
    field_label: str,
    selector: str,
    field_kind: str,
) -> tuple[Locator, str, int, str]:
    normalized = _normalize_label(field_label)
    anchors = await _find_label_anchors(page, normalized)
    if not anchors:
        raise NewYorkAutomationError(f"Unable to locate {field_kind} control for '{field_label}': no matching label anchors.")

    adjacent = await _choose_adjacent_control(anchors, selector)
    if adjacent is not None:
        controls, matched = adjacent
        return controls, matched, await controls.count(), "nearest sibling"

    row_based = await _choose_nearest_row_control(anchors, selector, normalized)
    if row_based is not None:
        controls, matched = row_based
        return controls, matched, await controls.count(), "nearest row"

    fallback = await _fallback_get_by_label(page, field_label, normalized, selector)
    if fallback is not None:
        controls, matched = fallback
        return controls, matched, await controls.count(), "get_by_label fallback"

    raise NewYorkAutomationError(f"Unable to locate {field_kind} control for '{field_label}': all strategies failed.")


async def _choose_adjacent_control(anchors: list[tuple[Locator, str]], selector: str) -> Optional[tuple[Locator, str]]:
    xpath_tag = _selector_to_xpath(selector)
    for anchor, matched in anchors:
        candidates = (
            anchor.locator(f"xpath=following-sibling::{xpath_tag}[1]"),
            anchor.locator(f"xpath=parent::*/*[self::{xpath_tag}][1]"),
            anchor.locator(f"xpath=parent::*//{xpath_tag}[1]"),
        )
        for candidate in candidates:
            if await candidate.count() > 0 and await candidate.first.is_visible():
                return candidate, matched
    return None


async def _choose_nearest_row_control(
    anchors: list[tuple[Locator, str]],
    selector: str,
    current_label: str,
) -> Optional[tuple[Locator, str]]:
    for anchor, matched in anchors:
        containers = anchor.locator("xpath=ancestor::*[self::div or self::tr or self::td or self::li]")
        n = await containers.count()
        for i in range(n):
            row = containers.nth(i)
            all_count = await row.locator(_ALL_CONTROLS_SELECTOR).count()
            if all_count == 0:
                continue
            if all_count > 20:
                print(f"NY debug -> field='{matched}' rejected broad row with {all_count} inputs")
                continue
            typed = row.locator(selector)
            if await typed.count() == 0:
                continue
            return typed, matched
    return None


async def _fallback_get_by_label(
    page: Page,
    field_label: str,
    normalized_label: str,
    selector: str,
) -> Optional[tuple[Locator, str]]:
    for query in (field_label, normalized_label):
        candidate = page.get_by_label(query, exact=False)
        if await candidate.count() <= 0:
            continue
        direct = candidate.locator("xpath=self::input|self::textarea|self::select")
        if await direct.count() > 0:
            return direct, query
        descendants = candidate.locator(selector)
        if await descendants.count() > 0:
            return descendants, query
    return None


async def _find_label_anchors(page: Page, normalized_label: str) -> list[tuple[Locator, str]]:
    xpath = (
        "xpath=//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{_xpath_literal(normalized_label)})]"
    )
    matches = page.locator(xpath)
    count = await matches.count()
    anchors: list[tuple[Locator, str]] = []
    for i in range(count):
        node = matches.nth(i)
        if not await node.is_visible():
            continue
        text = _clean_ws(await node.inner_text())
        if not text:
            continue
        anchors.append((node, text))

    anchors.sort(key=lambda item: len(item[1]))
    return anchors


async def _row_has_other_known_labels(row: Locator, current_normalized: str) -> bool:
    _ = row
    _ = current_normalized
    return False


async def _select_dropdown_resilient(select: Locator, field_label: str, desired_value: str) -> None:
    print(f"NY debug -> dropdown='{field_label}' requested='{desired_value}'")

    try:
        await select.select_option(label=desired_value)
        print(f"NY debug -> dropdown='{field_label}' matched via label: '{desired_value}'")
        return
    except Exception:
        pass

    options = await select.evaluate_all(
        "els => els[0] ? Array.from(els[0].options).map(o => ({text: (o.textContent || '').trim(), value: o.value})) : []"
    )
    option_texts = [str(opt.get("text", "")) for opt in options]
    print(f"NY debug -> dropdown='{field_label}' options={option_texts}")

    target_norm = _normalize_option(desired_value)

    for opt in options:
        text = str(opt.get("text", ""))
        if _normalize_option(text) == target_norm:
            await select.select_option(value=str(opt.get("value", "")))
            print(f"NY debug -> dropdown='{field_label}' matched via normalized label: '{text}'")
            return

    for opt in options:
        text = str(opt.get("text", ""))
        norm = _normalize_option(text)
        if target_norm and (target_norm in norm or norm in target_norm):
            await select.select_option(value=str(opt.get("value", "")))
            print(f"NY debug -> dropdown='{field_label}' matched via partial label: '{text}'")
            return

    raise NewYorkAutomationError(f"Unable to select option label '{desired_value}' for dropdown '{field_label}'.")


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
    return radios.nth(0 if yes_value else min(1, count - 1))


async def _click_radio_label_for_input(row: Locator, radio: Locator) -> bool:
    radio_id = await _safe_attr(radio, "id")
    if radio_id:
        by_for = row.locator(f"label[for='{radio_id}']").first
        if await by_for.count() > 0 and await by_for.is_visible():
            await by_for.click(force=True)
            return True
    sibling = radio.locator("xpath=following-sibling::label[1]").first
    if await sibling.count() > 0 and await sibling.is_visible():
        await sibling.click(force=True)
        return True
    parent = radio.locator("xpath=ancestor::label[1]").first
    if await parent.count() > 0 and await parent.is_visible():
        await parent.click(force=True)
        return True
    return False


def _selector_to_xpath(selector: str) -> str:
    if selector == _TEXT_SELECTOR:
        return "input[not(@type='hidden')]|textarea"
    if selector == _SELECT_SELECTOR:
        return "select"
    if selector == _RADIO_SELECTOR:
        return "input[@type='radio']"
    if selector == _CHECKBOX_SELECTOR:
        return "input[@type='checkbox']"
    return "input|select|textarea"


def _resolve_holder_id_value(record: Dict[str, Any]) -> str:
    return _as_string(record.get("holder_id"))


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


def _normalize_option(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _clean_ws(text: str) -> str:
    return " ".join(text.split())


def _count_known_labels(text: str) -> int:
    return sum(1 for label in _ALL_FIELD_LABELS if _normalize_label(label) in text)


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


def _log_success(prefix: str, field_name: str, matched_label: str, row_inputs: int, strategy: str) -> None:
    print(
        f"{prefix} debug -> field='{field_name}' matched_label='{matched_label}' row_inputs={row_inputs} strategy='{strategy}'"
    )


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
