"""New York filing runner for the Online_Compliance_Bot project.

This module intentionally avoids standard label-based selectors because the NY
holder portal renders form controls in row/container layouts where `<label for>`
bindings can be inconsistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

NY_HOLDER_INFO_URL = "https://ouf.osc.ny.gov/app/holder-info"


class NewYorkAutomationError(RuntimeError):
    """Raised when a required NY form control cannot be located or used."""


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


# ----- Public runner -------------------------------------------------------

def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run New York workflow through upload step and stop at preview/signature.

    The workflow intentionally does NOT sign or submit.
    """
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    if not naupa_path.exists():
        raise FileNotFoundError(f"NAUPA file not found: {naupa_path}")

    page.goto(NY_HOLDER_INFO_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(wait_after_navigation_ms)

    _fill_holder_info_page(page, record)
    _click_next(page)

    _upload_naupa_file(page, naupa_path)
    _click_next(page)


# Alias helpers for compatibility with different registries.
run_newyork = run
run_newyork_filing = run


# ----- Page actions --------------------------------------------------------

def _fill_holder_info_page(page: Page, record: Dict[str, Any]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if value:
            _fill_text_by_row_label(page, field.label, value)

    for field in _SELECT_FIELDS:
        value = _as_string(record.get(field.key))
        if value:
            _select_dropdown_by_row_label(page, field.label, value)

    for field in _RADIO_FIELDS:
        value = _as_bool(record.get(field.key))
        if value is not None:
            _set_yes_no_radio_by_row_label(page, field.label, value)

    foreign_address = _as_bool(record.get("foreign_address"))
    if foreign_address is not None:
        _set_checkbox_by_row_label(page, _FOREIGN_ADDRESS_LABEL, foreign_address)


def _upload_naupa_file(page: Page, file_path: Path) -> None:
    file_input = page.locator("input[type='file']").first
    try:
        file_input.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        raise NewYorkAutomationError("Could not find visible file upload input on NY upload page.") from exc

    file_input.set_input_files(str(file_path))


def _click_next(page: Page) -> None:
    candidates = (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("text=Next").locator("xpath=ancestor::button[1]"),
    )

    for candidate in candidates:
        if candidate.count() > 0:
            target = candidate.first
            if target.is_enabled():
                target.click(timeout=10_000)
                page.wait_for_timeout(1000)
                return

    raise NewYorkAutomationError("Could not find a clickable 'Next' control on NY page.")


# ----- Container/row-based field helpers ----------------------------------

def _fill_text_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_row_container(page, label_text)
    input_locator = row.locator("input:not([type='radio']):not([type='checkbox']):not([type='file']), textarea").first

    if input_locator.count() == 0:
        raise NewYorkAutomationError(f"Text input not found for label containing '{label_text}'.")

    input_locator.scroll_into_view_if_needed()
    input_locator.fill(value)


def _select_dropdown_by_row_label(page: Page, label_text: str, value: str) -> None:
    row = _find_row_container(page, label_text)
    select_locator = row.locator("select").first

    if select_locator.count() == 0:
        raise NewYorkAutomationError(f"Dropdown/select not found for label containing '{label_text}'.")

    select_locator.scroll_into_view_if_needed()

    # Try by value first, then label. Avoid fill() for dropdowns.
    try:
        select_locator.select_option(value=value)
        return
    except Exception:
        pass

    try:
        select_locator.select_option(label=value)
        return
    except Exception as exc:
        raise NewYorkAutomationError(
            f"Unable to select '{value}' for dropdown containing label '{label_text}'."
        ) from exc


def _set_yes_no_radio_by_row_label(page: Page, label_text: str, is_yes: bool) -> None:
    row = _find_row_container(page, label_text)
    radios = row.locator("input[type='radio']")
    count = radios.count()

    if count == 0:
        raise NewYorkAutomationError(f"Radio inputs not found for label containing '{label_text}'.")

    desired_tokens: Iterable[str] = ("yes", "true") if is_yes else ("no", "false")

    # 1) Prefer radio with matching value/id/name markers.
    for i in range(count):
        radio = radios.nth(i)
        metadata = " ".join(
            filter(
                None,
                [
                    _safe_attr(radio, "value"),
                    _safe_attr(radio, "id"),
                    _safe_attr(radio, "name"),
                    _safe_attr(radio, "aria-label"),
                ],
            )
        ).lower()

        if any(token in metadata for token in desired_tokens):
            if _click_radio_via_for_label(row, radio):
                return
            radio.set_checked(True, force=True)
            return

    # 2) Fallback: assume first=Yes, second=No.
    fallback_index = 0 if is_yes else min(1, count - 1)
    fallback_radio = radios.nth(fallback_index)
    if _click_radio_via_for_label(row, fallback_radio):
        return
    fallback_radio.set_checked(True, force=True)


def _set_checkbox_by_row_label(page: Page, label_text: str, should_check: bool) -> None:
    row = _find_row_container(page, label_text)
    checkbox = row.locator("input[type='checkbox']").first

    if checkbox.count() == 0:
        raise NewYorkAutomationError(f"Checkbox not found for label containing '{label_text}'.")

    checkbox.set_checked(should_check, force=True)


def _find_row_container(page: Page, label_text: str) -> Locator:
    label_node = _find_label_node(page, label_text)

    # Walk up to a likely row/container and return first visible match.
    for xpath in (
        "xpath=ancestor::*[self::div or self::tr][.//input or .//select or .//textarea][1]",
        "xpath=ancestor::*[.//input or .//select or .//textarea][1]",
    ):
        container = label_node.locator(xpath).first
        if container.count() > 0 and container.is_visible():
            return container

    raise NewYorkAutomationError(f"Could not find row container for label containing '{label_text}'.")


def _find_label_node(page: Page, label_text: str) -> Locator:
    # Match both strict and loose variants because NY labels may include * and : suffixes.
    escaped = label_text.replace(' ', r'\s+')
    patterns = [
        f"text={label_text}",
        f"text=/{escaped}/i",
    ]

    for pattern in patterns:
        loc = page.locator(pattern).first
        if loc.count() > 0 and loc.is_visible():
            return loc

    # Strong fallback: any element containing text.
    fallback = page.locator(f"xpath=//*[contains(normalize-space(.), '{label_text}')] ").first
    if fallback.count() > 0 and fallback.is_visible():
        return fallback

    raise NewYorkAutomationError(f"Could not find visible label text containing '{label_text}'.")


def _click_radio_via_for_label(row: Locator, radio: Locator) -> bool:
    radio_id = _safe_attr(radio, "id")
    if not radio_id:
        return False

    label_for_radio = row.locator(f"label[for='{radio_id}']").first
    if label_for_radio.count() == 0 or not label_for_radio.is_visible():
        return False

    label_for_radio.click(force=True)
    return True


# ----- Value normalization --------------------------------------------------

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
