"""Connecticut filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import FieldResolutionError, fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

CT_HOLDER_INFO_URL = "https://ctbiglist.gov/app/holder-info"


class ConnecticutAutomationError(RuntimeError):
    """Raised when CT automation cannot reliably continue."""


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
    _FieldSpec("Email Address", "email"),
    _FieldSpec("Email Address Confirmation", "email"),
)

_DROPDOWN_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Report Type", "report_type"),
    _FieldSpec("Report Year", "report_year"),
)

_CT_REPORT_TYPE_OPTIONS: set[str] = {
    "annual",
    "audit",
    "state agency reporting",
    "supplemental report",
}

_FUNDS_REMITTED_OPTIONS: set[str] = {"check", "ach", "wire"}


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run CT workflow through upload and preview pages; stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(CT_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_holder_info_page(page, record, errors)

    if errors:
        raise ConnecticutAutomationError("CT holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("CT debug -> clicking Next after CT holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_connecticut = run
run_connecticut_filing = run


async def _fill_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if field.key == "holder_id" and not value:
            continue
        if field.key == "phone_extension" and not value:
            continue
        if not value:
            continue
        await _guarded(errors, f"text '{field.label}'", lambda: _fill_text_by_label(page, field.label, value))

    report_type = _as_string(record.get("report_type"))
    if report_type:
        if _normalize(report_type) not in _CT_REPORT_TYPE_OPTIONS:
            print(f"CT warning -> Invalid report_type value: '{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: _select_dropdown_by_label(page, "Report Type", report_type))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_or_accept_disabled_report_year(page, report_year))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        raise ConnecticutAutomationError("negative_report is required for CT filing.")

    await _guarded(errors, "radio 'This is a Negative Report'", lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative))

    if negative:
        print("CT debug -> field='This is a Negative Report' type='RADIO' value='Yes' strategy='visible-text radio'")
        print("CT debug -> skipping amount fields because negative_report=Yes")
        return

    print("CT debug -> field='This is a Negative Report' type='RADIO' value='No' strategy='visible-text radio'")

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required when negative_report is No.")
    else:
        await _guarded(
            errors,
            "text 'Total Dollar Amount Remitted'",
            lambda: _fill_text_by_label(page, "Total Dollar Amount Remitted", amount_to_remit),
        )

    funds = _as_string(record.get("funds_remitted_via"))
    if not funds:
        errors.append("funds_remitted_via is required when negative_report is No.")
    else:
        if _normalize(funds) not in _FUNDS_REMITTED_OPTIONS:
            print(f"CT warning -> Unexpected funds_remitted_via value: '{funds}'")
        await _guarded(
            errors,
            "dropdown 'Funds Remitted Via'",
            lambda: _select_dropdown_by_label(page, "Funds Remitted Via", funds),
        )




async def _set_or_accept_disabled_report_year(page: Page, expected_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "CT")
    except FieldResolutionError as exc:
        raise ConnecticutAutomationError("CT could not locate Report Year dropdown row") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()
    current_text = (await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = (await control.evaluate("el => (el.value || '').trim()"))

    expected_norm = _normalize(expected_year)
    text_norm = _normalize(str(current_text))
    value_norm = _normalize(str(current_value))

    print(
        f"CT debug -> Report Year enabled={'yes' if enabled else 'no'} "
        f"current_text='{current_text}' current_value='{current_value}' expected='{expected_year}'"
    )

    if not enabled:
        if expected_norm in text_norm or expected_norm == value_norm:
            print(f"CT debug -> Report Year disabled but already set to expected value '{expected_year}'; accepting as valid")
            return
        raise ConnecticutAutomationError("CT Report Year dropdown stayed disabled after selecting Report Type and did not match expected value")

    await _select_dropdown_by_label(page, "Report Year", expected_year)

async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"CT warning -> NAUPA file does not exist: {file_path}; skipping upload.")
        return

    file_inputs = page.locator("input[type='file']")
    if await file_inputs.count() <= 0:
        print("CT warning -> Could not find CT upload input input[type='file']; skipping upload.")
        return

    await file_inputs.first.set_input_files(str(file_path))
    print(f"CT debug -> uploaded NAUPA file '{file_path}'")
    await page.wait_for_timeout(1200)


async def _click_next(page: Page) -> None:
    candidates = (
        page.get_by_role("button", name="Next", exact=True),
        page.locator("button:has-text('Next')"),
        page.locator("input[type='submit'][value='Next']"),
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
    raise ConnecticutAutomationError("Could not find a clickable 'Next' control on CT page.")


_TEXT_SELECTOR = "input[type='text'], input:not([type='hidden']), textarea"
_SELECT_SELECTOR = "select"
_RADIO_SELECTOR = "input[type='radio']"


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "CT")


async def _select_dropdown_by_label(page: Page, label_text: str, value: str) -> None:
    await select_dropdown_field(page, label_text, value, "CT")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "CT")


async def _resolve_control(page: Page, label_text: str, selector: str, field_type: str) -> tuple[Locator, str]:
    controls, strategy = await _resolve_control_collection(page, label_text, selector, field_type)
    return controls.first, strategy


async def _resolve_control_collection(page: Page, label_text: str, selector: str, field_type: str) -> tuple[Locator, str]:
    anchors = await _find_label_anchors(page, label_text)
    if not anchors:
        raise ConnecticutAutomationError(f"Could not find label node for '{label_text}'.")

    for anchor in anchors:
        rows = anchor.locator("xpath=ancestor::*[self::div or self::tr or self::td or self::li]")
        row_count = await rows.count()
        for i in range(row_count):
            row = rows.nth(i)
            controls = row.locator(selector)
            if await controls.count() <= 0:
                continue
            return controls, "xpath row"

    for query in (label_text, _normalize_label(label_text)):
        labeled = page.get_by_label(query, exact=False)
        direct = labeled.locator("xpath=self::input|self::textarea|self::select")
        if await direct.count() > 0:
            return direct, "get_by_label fallback"
        nested = labeled.locator(selector)
        if await nested.count() > 0:
            return nested, "get_by_label fallback"

    raise ConnecticutAutomationError(f"Unable to resolve {field_type} control for label '{label_text}'.")


async def _find_label_anchors(page: Page, label_text: str) -> list[Locator]:
    normalized = _normalize_label(label_text)
    xpath = (
        "xpath=//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{_xpath_literal(normalized)})]"
    )
    matches = page.locator(xpath)
    count = await matches.count()

    anchors: list[tuple[int, Locator]] = []
    for i in range(count):
        node = matches.nth(i)
        if not await node.is_visible():
            continue
        text = _clean_ws(await node.inner_text())
        if not text:
            continue
        anchors.append((len(text), node))

    anchors.sort(key=lambda item: item[0])
    return [node for _, node in anchors]


async def _select_option_resilient(select: Locator, field_label: str, desired_value: str) -> str:
    try:
        await select.select_option(label=desired_value)
        return "select_option(label)"
    except Exception:
        pass

    options = await select.evaluate_all(
        "els => els[0] ? Array.from(els[0].options).map(o => ({text: (o.textContent || '').trim(), value: o.value})) : []"
    )

    target = _normalize(desired_value)
    for option in options:
        text = _normalize(str(option.get("text", "")))
        if text == target:
            await select.select_option(value=str(option.get("value", "")))
            return "normalized exact option text"

    for option in options:
        text = _normalize(str(option.get("text", "")))
        if target and (target in text or text in target):
            await select.select_option(value=str(option.get("value", "")))
            return "partial option text"

    option_texts = [str(option.get("text", "")) for option in options]
    raise ConnecticutAutomationError(
        f"Unable to select '{desired_value}' for dropdown '{field_label}'. Available options: {option_texts}"
    )


async def _find_radio_by_visible_text(radios: Locator, target_text: str) -> Optional[Locator]:
    target = _normalize(target_text)
    count = await radios.count()

    for i in range(count):
        radio = radios.nth(i)
        label_text = _normalize(await _radio_label_text(radio))
        if label_text == target:
            return radio

    for i in range(count):
        radio = radios.nth(i)
        label_text = _normalize(await _radio_label_text(radio))
        if target in label_text:
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

    sibling_label = radio.locator("xpath=following-sibling::label[1]").first
    if await sibling_label.count() > 0:
        text = _clean_ws(await sibling_label.inner_text())
        if text:
            return text

    parent_label = radio.locator("xpath=ancestor::label[1]").first
    if await parent_label.count() > 0:
        text = _clean_ws(await parent_label.inner_text())
        if text:
            return text

    return _clean_ws(await _safe_attr(radio, "value"))


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


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"CT automation warning: {message}")
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


def _normalize_label(text: str) -> str:
    return " ".join(text.replace("*", "").replace(":", "").strip().lower().split())


def _normalize(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def _clean_ws(text: str) -> str:
    return " ".join(text.split())


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


async def _safe_attr(locator: Locator, attr_name: str) -> str:
    value = await locator.get_attribute(attr_name)
    return "" if value is None else value
