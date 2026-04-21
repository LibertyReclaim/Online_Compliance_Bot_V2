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

_ALL_FIELD_LABELS: tuple[str, ...] = tuple({
    *[field.label for field in _TEXT_FIELDS],
    *[field.label for field in _EMAIL_FIELDS],
    *[field.label for field in _DROPDOWN_FIELDS],
    "Remit Report ID",
    "Funds Remitted Via",
    "This is a Negative Report",
    "Includes Safe Deposit Box",
    "Total Cash Remitted",
    "Total Cash Reported",
    "Total Shares Remitted",
    "Total Shares Reported",
})


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
        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))

    email_value = record.get("email")
    if email_value:
        for field in _EMAIL_FIELDS:
            await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, email_value))

    for field in _DROPDOWN_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            continue
        await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_label(page, field.label, value))

    submission_type = _as_string(record.get("ca_submission_type"))
    is_remit_report = submission_type.lower() == "remit report"

    if is_remit_report:
        remit_report_id = _as_string(record.get("ca_remit_report_id"))
        funds_remitted_via = _as_string(record.get("ca_funds_remitted_via"))
        if remit_report_id:
            await _guarded(errors, "text 'Remit Report ID'", lambda: _fill_text_by_label(page, "Remit Report ID", remit_report_id))
        if funds_remitted_via:
            await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: _select_dropdown_by_label(page, "Funds Remitted Via", funds_remitted_via))

    negative_report = _as_bool(record.get("ca_negative_report"))
    if negative_report is not None:
        await _guarded(errors, "radio 'This is a Negative Report'", lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative_report))

    safe_deposit = _as_bool(record.get("ca_safe_deposit_box"))
    if safe_deposit is not None:
        await _guarded(errors, "radio 'Includes Safe Deposit Box'", lambda: _set_yes_no_radio_by_label(page, "Includes Safe Deposit Box", safe_deposit))

    total_cash = _as_string(record.get("ca_total_cash"))
    total_shares = _as_string(record.get("ca_total_shares"))
    if negative_report is False and not total_cash:
        errors.append("ca_total_cash is required when ca_negative_report is No.")

    cash_label = "Total Cash Remitted" if is_remit_report else "Total Cash Reported"
    shares_label = "Total Shares Remitted" if is_remit_report else "Total Shares Reported"

    if total_cash:
        await _guarded(errors, f"text '{cash_label}'", lambda: _fill_text_by_label(page, cash_label, total_cash))
    if total_shares:
        await _guarded(errors, f"text '{shares_label}'", lambda: _fill_text_by_label(page, shares_label, total_shares))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    print(f"Using NAUPA file: {file_path}")
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print("CA warning: NAUPA file not found, skipping upload and leaving tab open for manual action.")
        return

    file_inputs = page.locator("input[type='file']")
    if (await file_inputs.count()) > 0:
        await file_inputs.first.set_input_files(str(file_path))
        await page.wait_for_timeout(1200)
        return

    if await _click_add_document_if_present(page):
        file_inputs = page.locator("input[type='file']")
        if (await file_inputs.count()) > 0:
            await file_inputs.first.set_input_files(str(file_path))
            await page.wait_for_timeout(1200)
            return

    raise CaliforniaAutomationError("Could not find CA upload file input (input[type='file']).")


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


_TEXT_SELECTOR = "input[type='text'], input:not([type='hidden']), textarea"
_SELECT_SELECTOR = "select"
_RADIO_SELECTOR = "input[type='radio']"
_ALL_CONTROLS_SELECTOR = "input:not([type='hidden']), select, textarea"


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    control, matched, row_count, strategy = await _resolve_nearest_control(page, label_text, _TEXT_SELECTOR, "text")
    await control.scroll_into_view_if_needed()
    await control.fill(value)
    _log_success(label_text, matched, row_count, strategy)


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    control, matched, row_count, strategy = await _resolve_nearest_control(page, label_text, _SELECT_SELECTOR, "select")
    await control.scroll_into_view_if_needed()
    try:
        await control.select_option(label=value)
    except Exception as exc:
        raise CaliforniaAutomationError(f"Unable to select option label '{value}' for dropdown '{label_text}'.") from exc
    _log_success(label_text, matched, row_count, strategy)


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    controls, matched, row_count, strategy = await _resolve_nearest_control_collection(page, label_text, _RADIO_SELECTOR, "radio")
    target = await _pick_radio_by_semantics(controls, yes_value)
    row = target.locator("xpath=ancestor::*[self::div or self::tr or self::td][1]")
    if not await _click_radio_label_for_input(row, target):
        await target.set_checked(True, force=True)
    _log_success(label_text, matched, row_count, strategy)


async def _resolve_nearest_control(
    page: Page,
    field_label: str,
    control_selector: str,
    field_kind: str,
) -> tuple[Locator, str, int, str]:
    controls, matched, row_count, strategy = await _resolve_nearest_control_collection(page, field_label, control_selector, field_kind)
    return controls.first, matched, row_count, strategy


async def _resolve_nearest_control_collection(
    page: Page,
    field_label: str,
    control_selector: str,
    field_kind: str,
) -> tuple[Locator, str, int, str]:
    normalized = _normalize_label(field_label)
    anchors = await _find_label_anchors(page, normalized)
    if not anchors:
        reason = "no matching label anchors"
        _log_failure(field_label, normalized, reason)
        raise CaliforniaAutomationError(f"Unable to locate {field_kind} control for '{field_label}': {reason}.")

    sibling_pick = await _choose_adjacent_control(anchors, control_selector, normalized)
    if sibling_pick is not None:
        controls, matched = sibling_pick
        return controls, matched, await controls.count(), "nearest sibling"

    row_pick = await _choose_nearest_row_control(anchors, control_selector, normalized)
    if row_pick is not None:
        controls, matched = row_pick
        return controls, matched, await controls.count(), "nearest row"

    by_label = await _fallback_get_by_label(page, field_label, normalized, control_selector)
    if by_label is not None:
        controls, matched = by_label
        return controls, matched, await controls.count(), "get_by_label fallback"

    reason = "all strategies failed"
    _log_failure(field_label, normalized, reason)
    raise CaliforniaAutomationError(f"Unable to locate {field_kind} control for '{field_label}': {reason}.")


async def _choose_adjacent_control(
    anchors: list[tuple[Locator, str]],
    control_selector: str,
    normalized_label: str,
) -> Optional[tuple[Locator, str]]:
    for anchor, matched_text in anchors:
        candidates = (
            anchor.locator(f"xpath=following-sibling::{_selector_to_xpath(control_selector)}[1]"),
            anchor.locator(f"xpath=../following-sibling::*[1]//{_selector_to_xpath(control_selector)}[1]"),
            anchor.locator(f"xpath=ancestor::label[1]/following::{_selector_to_xpath(control_selector)}[1]"),
        )
        for candidate in candidates:
            if await candidate.count() <= 0:
                continue
            first = candidate.first
            if await first.is_visible():
                return first, matched_text

    _log_failure(normalized_label, normalized_label, "nearest sibling strategy: no adjacent controls")
    return None


async def _choose_nearest_row_control(
    anchors: list[tuple[Locator, str]],
    control_selector: str,
    normalized_field_label: str,
) -> Optional[tuple[Locator, str]]:
    for anchor, matched_text in anchors:
        containers = anchor.locator("xpath=ancestor::*[self::div or self::tr or self::td or self::li]")
        container_count = await containers.count()
        for idx in range(container_count):
            container = containers.nth(idx)
            all_controls = container.locator(_ALL_CONTROLS_SELECTOR)
            all_count = await all_controls.count()
            if all_count == 0:
                continue

            if all_count > 6:
                print(f"CA debug -> field='{normalized_field_label}' rejected broad row with {all_count} inputs")
                continue

            if await _row_has_other_known_label(container, normalized_field_label):
                print(f"CA debug -> field='{normalized_field_label}' rejected broad row with {all_count} inputs")
                continue

            typed_controls = container.locator(control_selector)
            typed_count = await typed_controls.count()
            if typed_count == 0:
                continue

            following = anchor.locator(f"xpath=ancestor::*[self::div or self::tr or self::td or self::li][{idx + 1}]//{_selector_to_xpath(control_selector)}")
            if await following.count() > 0:
                return following, matched_text
            return typed_controls, matched_text

    return None


async def _row_has_other_known_label(container: Locator, current_normalized_label: str) -> bool:
    text = _normalize_label(await container.inner_text())
    for known in _ALL_FIELD_LABELS:
        normalized_known = _normalize_label(known)
        if normalized_known == current_normalized_label:
            continue
        if normalized_known and normalized_known in text:
            return True
    return False


async def _fallback_get_by_label(
    page: Page,
    original_label: str,
    normalized_label: str,
    control_selector: str,
) -> Optional[tuple[Locator, str]]:
    for label in (original_label, normalized_label):
        candidate = page.get_by_label(label, exact=False)
        if await candidate.count() <= 0:
            continue
        controls = candidate.locator("xpath=self::input|self::textarea|self::select")
        if await controls.count() > 0:
            return controls, label
        descendants = candidate.locator(control_selector)
        if await descendants.count() > 0:
            return descendants, label
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
        anchors.append((node, (await node.inner_text()).strip()))
    return anchors


def _selector_to_xpath(selector: str) -> str:
    if selector == _TEXT_SELECTOR:
        return "input[not(@type='hidden')]|textarea"
    if selector == _SELECT_SELECTOR:
        return "select"
    if selector == _RADIO_SELECTOR:
        return "input[@type='radio']"
    return "input|select|textarea"


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


def _log_success(field_name: str, matched_label: str, row_count: int, strategy: str) -> None:
    print(f"CA debug -> field='{field_name}' matched_label='{matched_label}' row_inputs={row_count} strategy='{strategy}'")


def _log_failure(field_name: str, normalized: str, reason: str) -> None:
    print(f"CA debug -> field='{field_name}' label='{field_name}' normalized='{normalized}' failure='{reason}'")


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
