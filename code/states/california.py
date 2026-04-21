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
    *[f.label for f in _TEXT_FIELDS],
    *[f.label for f in _EMAIL_FIELDS],
    *[f.label for f in _DROPDOWN_FIELDS],
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
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

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

    email_value = _as_string(record.get("email"))
    if email_value:
        for field in _EMAIL_FIELDS:
            await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, email_value))

    for field in _DROPDOWN_FIELDS:
        value = _as_string(record.get(field.key))
        if value:
            await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_label(page, field.label, value))

    submission_type = _as_string(record.get("ca_submission_type"))
    is_remit_report = submission_type.lower() == "remit report"

    remit_id = _as_string(record.get("ca_remit_report_id"))
    funds = _as_string(record.get("ca_funds_remitted_via"))
    if is_remit_report and remit_id:
        await _guarded(errors, "text 'Remit Report ID'", lambda: _fill_text_by_label(page, "Remit Report ID", remit_id))
    if is_remit_report and funds:
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: _select_dropdown_by_label(page, "Funds Remitted Via", funds))

    negative = _as_bool(record.get("ca_negative_report"))
    if negative is not None:
        await _guarded(errors, "radio 'This is a Negative Report'", lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative))

    safe_box = _as_bool(record.get("ca_safe_deposit_box"))
    if safe_box is not None:
        await _guarded(errors, "radio 'Includes Safe Deposit Box'", lambda: _set_yes_no_radio_by_label(page, "Includes Safe Deposit Box", safe_box))

    total_cash = _as_string(record.get("ca_total_cash"))
    total_shares = _as_string(record.get("ca_total_shares"))
    if negative is False and not total_cash:
        errors.append("ca_total_cash is required when ca_negative_report is No.")

    cash_label = "Total Cash Remitted" if is_remit_report else "Total Cash Reported"
    shares_label = "Total Shares Remitted" if is_remit_report else "Total Shares Reported"

    if total_cash:
        await _guarded(errors, f"text '{cash_label}'", lambda: _fill_text_by_label(page, cash_label, total_cash))
    if total_shares:
        await _guarded(errors, f"text '{shares_label}'", lambda: _fill_text_by_label(page, shares_label, total_shares))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
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
    raise CaliforniaAutomationError("Could not find a clickable 'Next' control on CA page.")


_TEXT_SELECTOR = "input[type='text'], input:not([type='hidden']), textarea"
_SELECT_SELECTOR = "select"
_RADIO_SELECTOR = "input[type='radio']"
_ALL_CONTROLS_SELECTOR = "input:not([type='hidden']), select, textarea"


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    control, matched, row_count, strategy = await _resolve_nearest_control(page, label_text, _TEXT_SELECTOR, "text")
    await control.fill(value)
    _log_success("CA", label_text, matched, row_count, strategy)


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    control, matched, row_count, strategy = await _resolve_nearest_control(page, label_text, _SELECT_SELECTOR, "select")
    await control.select_option(label=value)
    _log_success("CA", label_text, matched, row_count, strategy)


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    controls, matched, row_count, strategy = await _resolve_nearest_control_collection(page, label_text, _RADIO_SELECTOR, "radio")
    target = await _pick_radio_by_semantics(controls, yes_value)
    row = target.locator("xpath=ancestor::*[self::div or self::tr or self::td][1]")
    if not await _click_radio_label_for_input(row, target):
        await target.set_checked(True, force=True)
    _log_success("CA", label_text, matched, row_count, strategy)


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
        raise CaliforniaAutomationError(f"Unable to locate {field_kind} control for '{field_label}': no matching label anchors.")

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

    raise CaliforniaAutomationError(f"Unable to locate {field_kind} control for '{field_label}': all strategies failed.")


async def _choose_adjacent_control(anchors: list[tuple[Locator, str]], selector: str) -> Optional[tuple[Locator, str]]:
    xpath_tag = _selector_to_xpath(selector)
    for anchor, matched in anchors:
        for candidate in (
            anchor.locator(f"xpath=following-sibling::{xpath_tag}[1]"),
            anchor.locator(f"xpath=parent::*/*[self::{xpath_tag}][1]"),
            anchor.locator(f"xpath=parent::*//{xpath_tag}[1]"),
        ):
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
            if all_count > 5 or await _row_has_other_known_labels(row, current_label):
                print(f"CA debug -> field='{matched}' rejected broad row with {all_count} inputs")
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
    base_xpath = (
        "//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{_xpath_literal(normalized_label)})]"
    )
    scoped = page.locator(f"xpath={base_xpath}[self::label or self::span or self::div or self::p or self::td]")
    broad = page.locator(f"xpath={base_xpath}")

    anchors = await _collect_filtered_label_nodes(scoped, normalized_label)
    if anchors:
        return anchors
    return await _collect_filtered_label_nodes(broad, normalized_label)


async def _collect_filtered_label_nodes(locator: Locator, normalized_label: str) -> list[tuple[Locator, str]]:
    results: list[tuple[Locator, str]] = []
    count = await locator.count()
    for i in range(count):
        node = locator.nth(i)
        if not await node.is_visible():
            continue
        text = _clean_ws(await node.inner_text())
        if not text:
            continue
        normalized_text = _normalize_label(text)
        if normalized_label not in normalized_text:
            continue
        if len(text) > 100:
            continue
        if _count_known_labels(normalized_text) > 2:
            continue
        results.append((node, text))
    results.sort(key=lambda item: len(item[1]))
    return results


async def _row_has_other_known_labels(row: Locator, current_normalized: str) -> bool:
    text = _normalize_label(await row.inner_text())
    hits = 0
    for label in _ALL_FIELD_LABELS:
        normalized = _normalize_label(label)
        if normalized == current_normalized:
            continue
        if normalized and normalized in text:
            hits += 1
        if hits >= 2:
            return True
    return False


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
    return "input|select|textarea"


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


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
