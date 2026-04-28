"""California filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, select_dropdown_field, set_radio_field

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
    _FieldSpec("Report Type", "report_type"),
    _FieldSpec("Submission Type", "submission_type"),
    _FieldSpec("Report Year", "report_year"),
    _FieldSpec("Fiscal Year End", "fiscal_year_end_month"),
)


_CA_VALID_REPORT_TYPES: set[str] = {
    "annual report",
    "sco audit report",
    "agent report",
    "life insurance",
    "voluntary compliance program report",
}

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
        if field.key == "report_type" and value:
            normalized_report_type = _normalize_label(value)
            if normalized_report_type not in _CA_VALID_REPORT_TYPES:
                print("Invalid CA report_type value")
        if value:
            await _guarded(errors, f"dropdown '{field.label}'", lambda: _select_dropdown_by_label(page, field.label, value))

    submission_type = _as_string(record.get("submission_type"))
    is_remit_report = submission_type.lower() == "remit report"

    remit_id = _as_string(record.get("remit_report_id"))
    funds = _as_string(record.get("funds_remitted_via"))
    if is_remit_report and remit_id:
        await _guarded(errors, "text 'Remit Report ID'", lambda: _fill_text_by_label(page, "Remit Report ID", remit_id))
    if is_remit_report and funds:
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: _select_dropdown_by_label(page, "Funds Remitted Via", funds))

    negative_raw = record.get("negative_report")
    negative = _resolve_negative_report(negative_raw, record.get("amount_to_remit"))
    print(f"CA debug -> negative_report raw='{_as_string(negative_raw)}' resolved={negative}")
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative))
    print(f"CA debug -> intended Negative Report radio='{'Yes' if negative else 'No'}'")

    safe_box_raw = record.get("safe_deposit_box")
    safe_box = _as_bool(safe_box_raw)
    if safe_box is not None:
        print(f"CA debug -> safe_deposit_box raw='{_as_string(safe_box_raw)}' resolved={safe_box}")
        await _guarded(errors, "radio 'Includes Safe Deposit Box'", lambda: _set_yes_no_radio_by_label(page, "Includes Safe Deposit Box", safe_box))

    total_cash = _as_string(record.get("amount_to_remit"))
    total_shares = _as_string(record.get("total_shares"))
    if not negative and not total_cash:
        errors.append("amount_to_remit is required when negative_report is No.")

    cash_label = "Total Cash Remitted" if is_remit_report else "Total Cash Reported"
    shares_label = "Total Shares Remitted" if is_remit_report else "Total Shares Reported"

    if negative:
        print(f"CA debug -> skipping {cash_label} because negative report is Yes")
        print(f"CA debug -> skipping {shares_label} because negative report is Yes")
    else:
        if total_cash:
            await _guarded(errors, f"text '{cash_label}'", lambda: _fill_text_if_enabled_by_label(page, cash_label, total_cash))
        if total_shares:
            await _guarded(errors, f"text '{shares_label}'", lambda: _fill_text_if_enabled_by_label(page, shares_label, total_shares))


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print("CA warning: NAUPA file not found, skipping upload and leaving tab open for manual action.")
        return

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
            print(f"CA debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"CA debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"CA debug -> upload selector '{selector}' failed: {exc}")
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

    raise CaliforniaAutomationError("Could not find CA upload file input. Attempted selectors: input[type='file'], input[type='file']:visible, input[accept], input[type='file'][accept].")


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
    await fill_text_field(page, label_text, value, "CA")


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    await select_dropdown_field(page, label_text, value, "CA")


async def _fill_text_if_enabled_by_label(page: Page, label_text: str, value: str) -> None:
    control, matched, row_count, strategy = await _resolve_nearest_control(page, label_text, _TEXT_SELECTOR, "text")
    enabled = await control.is_enabled()
    print(f"CA debug -> {label_text} enabled: {'yes' if enabled else 'no'}")
    if not enabled:
        print(f"CA debug -> skipping {label_text} because field is disabled")
        return
    await control.fill(value)
    _log_success("CA", label_text, matched, row_count, strategy)


def _resolve_negative_report(raw_value: Any, amount_to_remit: Any) -> bool:
    explicit = _as_bool(raw_value)
    if explicit is not None:
        return explicit

    amount = _as_float(amount_to_remit)
    if amount is None:
        return True
    return amount <= 0


def _as_float(value: Any) -> Optional[float]:
    text = _as_string(value).replace(',', '')
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "CA")


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
            if all_count > 20:
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


async def _find_radio_by_visible_text(radios: Locator, target_text: str) -> Optional[Locator]:
    count = await radios.count()
    normalized_target = _normalize_label(target_text)
    for i in range(count):
        radio = radios.nth(i)
        label_text = _normalize_label(await _radio_label_text(radio))
        if label_text == normalized_target:
            return radio
    for i in range(count):
        radio = radios.nth(i)
        label_text = _normalize_label(await _radio_label_text(radio))
        if normalized_target in label_text:
            return radio
    return None


async def _get_checked_radio_label_text(radios: Locator) -> str:
    count = await radios.count()
    for i in range(count):
        radio = radios.nth(i)
        if await radio.is_checked():
            return await _radio_label_text(radio)
    return ""


async def _radio_label_text(radio: Locator) -> str:
    radio_id = await _safe_attr(radio, "id")
    if radio_id:
        by_for = radio.locator(f"xpath=ancestor::*[self::div or self::tr or self::td][1]//label[@for='{radio_id}']").first
        if await by_for.count() > 0:
            text = _clean_ws(await by_for.inner_text())
            if text:
                return text

    sibling = radio.locator("xpath=following-sibling::label[1]").first
    if await sibling.count() > 0:
        text = _clean_ws(await sibling.inner_text())
        if text:
            return text

    parent = radio.locator("xpath=ancestor::label[1]").first
    if await parent.count() > 0:
        text = _clean_ws(await parent.inner_text())
        if text:
            return text

    return _clean_ws(await _safe_attr(radio, "value"))


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
