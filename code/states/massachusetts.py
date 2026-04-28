"""Massachusetts filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import (
    FieldResolutionError,
    fill_text_field,
    locate_strict_row_for_label,
    select_dropdown_field,
    set_radio_field,
)

MA_HOLDER_INFO_URL = "https://findmassmoney.gov/app/holder-info"


class MassachusettsAutomationError(RuntimeError):
    """Raised when MA automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Holder Contact", "contact_name", required=True),
    _TextFieldSpec("Contact Phone No", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email", required=True),
    _TextFieldSpec("Address 1", "address_1", required=True),
    _TextFieldSpec("Address 2", "address_2"),
    _TextFieldSpec("City", "city", required=True),
)


async def run(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    """Run MA workflow through upload/preview and stop before submit/signature."""
    record = _merge_records(holder_row, payment_row)
    naupa_path = Path(naupa_file_path).expanduser().resolve()

    await page.goto(MA_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)

    errors: list[str] = []
    await _fill_ma_holder_info_page(page, record, errors)

    if errors:
        raise MassachusettsAutomationError("MA holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("MA debug -> clicking Next after MA holder info completed")
    await _click_next(page)
    await _upload_naupa_file(page, naupa_path)
    await _click_next(page)


run_massachusetts = run
run_massachusetts_filing = run


async def _fill_ma_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        print(f"MA debug -> field='{field.label}' type='TEXT'")
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: _fill_text_by_label(page, f.label, v))

    postal_code = _as_string(record.get("zip")) or _as_string(record.get("zip_code"))
    if not postal_code:
        errors.append("zip/zip_code is required for 'Postal Code'.")
    else:
        await _guarded(errors, "text 'Postal Code'", lambda: _fill_text_by_label(page, "Postal Code", postal_code))

    await _guarded(errors, "text 'Address 3'", lambda: _fill_text_by_label(page, "Address 3", ""))

    state_value = _as_string(record.get("state")) or "Massachusetts"
    await _guarded(errors, "dropdown 'State'", lambda: _set_ma_state_dropdown(page, state_value))

    state_of_incorp = _as_string(record.get("state_of_incorporation")) or _as_string(record.get("state_incorporation"))
    if state_of_incorp:
        await _guarded(
            errors,
            "dropdown 'State of Incorporation'",
            lambda: _set_dropdown_or_accept_disabled(page, "State of Incorporation", state_of_incorp),
        )

    month_value, day_value, year_value = _extract_date_of_incorporation_parts(record)
    if not (month_value and day_value and year_value):
        errors.append("Date of Incorporation (month/day/year) is required for MA filing.")
    else:
        await _guarded(
            errors,
            "dropdown triplet 'Date of Incorporation'",
            lambda: _set_date_of_incorporation_triplet(page, month_value, day_value, year_value),
        )

    report_type_raw = _as_string(record.get("report_type"))
    if not report_type_raw:
        errors.append("report_type is required for 'Report Type'.")
    else:
        report_type = _normalize_ma_report_type(report_type_raw)
        await _guarded(errors, "dropdown 'Report Type'", lambda: _set_dropdown_or_accept_disabled(page, "Report Type", report_type))

    report_year = _as_string(record.get("report_year"))
    if report_year:
        await _guarded(errors, "dropdown 'Report Year'", lambda: _set_ma_report_year(page, report_year))

    negative_report = _as_bool(record.get("negative_report"))
    if negative_report is None:
        negative_report = False
    await _guarded(
        errors,
        "radio 'This is a Negative Report'",
        lambda: _set_yes_no_radio_by_label(page, "This is a Negative Report", negative_report),
    )

    amount_to_remit = _as_string(record.get("amount_to_remit"))
    if not amount_to_remit:
        errors.append("amount_to_remit is required for MA totals.")

    aggregate_cash_total = _as_string(record.get("aggregate_cash_total")) or amount_to_remit
    total_shares = _as_string(record.get("total_shares")) or "0"
    number_of_owners = _as_string(record.get("number_of_owners")) or "1"

    if aggregate_cash_total:
        print("MA debug -> field='Aggregate Cash Total' mapped_from='aggregate_cash_total'")
        await _guarded(
            errors,
            "text 'Aggregate Cash Total'",
            lambda: _fill_text_by_label(page, "Aggregate Cash Total", aggregate_cash_total),
        )

    if amount_to_remit:
        print("MA debug -> field='Owner Cash Total' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Owner Cash Total'",
            lambda: _fill_text_by_label(page, "Owner Cash Total", amount_to_remit),
        )
        print("MA debug -> field='Total of Cash Amount Reported' mapped_from='amount_to_remit'")
        await _guarded(
            errors,
            "text 'Total of Cash Amount Reported'",
            lambda: _fill_text_by_label(page, "Total of Cash Amount Reported", amount_to_remit),
        )

    await _guarded(
        errors,
        "text 'Total Number of Shares Reported'",
        lambda: _fill_text_by_label(page, "Total Number of Shares Reported", total_shares),
    )
    await _guarded(
        errors,
        "text 'Number of Owners Reported'",
        lambda: _fill_text_by_label(page, "Number of Owners Reported", number_of_owners),
    )


async def _set_date_of_incorporation_triplet(page: Page, month_value: str, day_value: str, year_value: str) -> None:
    print("MA debug -> field='Date of Incorporation' type='DROPDOWN_TRIPLET'")

    label_candidates = page.locator(
        "xpath=//*[self::label or self::span or self::div or self::p or self::strong or self::b or self::td][contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), 'date of incorporation')]"
    )

    row = None
    candidate_count = await label_candidates.count()
    for i in range(min(candidate_count, 12)):
        label = label_candidates.nth(i)
        try:
            if not await label.is_visible():
                continue
        except Exception:
            continue

        for xpath in [
            "xpath=ancestor::div[1]",
            "xpath=ancestor::*[contains(@class,'form-group')][1]",
            "xpath=ancestor::*[contains(@class,'row')][1]",
            "xpath=ancestor::td[1]",
            "xpath=ancestor::tr[1]",
            "xpath=ancestor::li[1]",
            "xpath=ancestor::div[2]",
            "xpath=ancestor::div[3]",
            "xpath=ancestor::div[4]",
        ]:
            candidate_row = label.locator(xpath).first
            try:
                if await candidate_row.count() <= 0:
                    continue
            except Exception:
                continue

            selects = candidate_row.locator("select")
            if await selects.count() >= 3:
                row = candidate_row
                break
        if row is not None:
            break

    if row is None:
        raise MassachusettsAutomationError("MA could not locate Date of Incorporation row with 3 dropdowns.")

    selects = row.locator("select")
    select_count = await selects.count()
    print(f"MA debug -> found {select_count} dropdowns in Date of Incorporation row")
    if select_count < 3:
        raise MassachusettsAutomationError(
            f"MA Date of Incorporation row found but expected 3 selects; found {select_count}."
        )

    parts = ((month_value, "month"), (day_value, "day"), (year_value, "year"))
    for index, (raw_value, part_name) in enumerate(parts):
        value = _normalize_date_part(raw_value)
        control = selects.nth(index)
        try:
            await control.select_option(label=value)
        except Exception:
            options = await control.evaluate(
                "el => Array.from(el.options).map(o => ({text:(o.textContent||'').trim(), value:(o.value||'').trim()}))"
            )
            matched_value = None
            for option in options:
                text_norm = _normalize(str(option.get("text", "")))
                value_norm = _normalize(str(option.get("value", "")))
                target_norm = _normalize(value)
                if text_norm == target_norm or value_norm == target_norm:
                    matched_value = str(option.get("value", ""))
                    break
            if matched_value is None:
                for option in options:
                    text_norm = _normalize(str(option.get("text", "")))
                    target_norm = _normalize(value)
                    if text_norm and (text_norm in target_norm or target_norm in text_norm):
                        matched_value = str(option.get("value", ""))
                        break
            if matched_value is None:
                raise MassachusettsAutomationError(
                    f"MA could not select Date of Incorporation {part_name}='{value}'."
                )
            await control.select_option(value=matched_value)

        selected_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(value) not in _normalize(selected_text) and _normalize(selected_text) not in _normalize(value):
            raise MassachusettsAutomationError(
                f"MA Date of Incorporation {part_name} verification failed. selected='{selected_text}' expected='{value}'."
            )
        print(f"MA debug -> selected {part_name}='{value}'")


async def _set_ma_state_dropdown(page: Page, expected_state: str) -> None:
    try:
        await _set_dropdown_or_accept_disabled(page, "State", expected_state)
    except Exception:
        await _set_dropdown_or_accept_disabled(page, "State", "Massachusetts")


async def _set_ma_report_year(page: Page, expected_year: str) -> None:
    label_text = "Report Year"
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "MA")
    except FieldResolutionError as exc:
        raise MassachusettsAutomationError("MA could not locate dropdown 'Report Year'.") from exc

    control = row.locator("select").first
    if await control.is_enabled():
        await _set_dropdown_or_accept_disabled(page, label_text, expected_year)
        return

    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
        return
    if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
        return

    raise MassachusettsAutomationError("MA Report Year is disabled but blank/unselected.")


async def _set_dropdown_or_accept_disabled(page: Page, label_text: str, expected_value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label_text, "dropdown", "MA")
    except FieldResolutionError as exc:
        raise MassachusettsAutomationError(f"MA could not locate dropdown '{label_text}'.") from exc

    control = row.locator("select").first
    enabled = await control.is_enabled()

    current_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
    current_value = _as_string(await control.evaluate("el => (el.value || '').trim()"))

    if not enabled:
        if _normalize(current_text) not in {"", "select an option", "select option", "please select"}:
            return
        if _normalize(current_value) not in {"", "0", "-1", "select", "select an option"}:
            return
        raise MassachusettsAutomationError(f"MA dropdown '{label_text}' is disabled and blank/unselected.")

    expected_norm = _normalize(expected_value)
    try:
        await select_dropdown_field(page, label_text, expected_value, "MA")
    except Exception as exc:
        latest_text = _as_string(await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()"))
        if _normalize(latest_text) == expected_norm:
            return
        raise MassachusettsAutomationError(
            f"MA failed selecting dropdown '{label_text}' with value '{expected_value}'."
        ) from exc


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1500)

    if not file_path.exists():
        print(f"MA warning -> NAUPA file does not exist: {file_path}; skipping upload.")
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
            print(f"MA debug -> upload selector='{selector}' count={count}")
            if count <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                value = await locator.first.get_attribute("value")
                print(f"MA debug -> upload success selector='{selector}' value='{value}'")
                await page.wait_for_timeout(1200)
                return
            except Exception as exc:
                print(f"MA debug -> upload selector '{selector}' failed: {exc}")
        await page.wait_for_timeout(800)

    raise MassachusettsAutomationError(
        "Could not find MA upload file input. Attempted selectors: "
        "input[type='file'], input[type='file']:visible, input[accept], input[type='file'][accept]."
    )


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
    raise MassachusettsAutomationError("Could not find a clickable 'Next' control on MA page.")


async def _fill_text_by_label(page: Page, label_text: str, value: str) -> None:
    await fill_text_field(page, label_text, value, "MA")


async def _set_yes_no_radio_by_label(page: Page, label_text: str, yes_value: bool) -> None:
    await set_radio_field(page, label_text, yes_value, "MA")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        message = f"Failed to set {field_desc}: {exc}"
        print(f"MA automation warning: {message}")
        errors.append(message)


def _normalize_ma_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
    }
    normalized = _normalize(raw_value)
    if normalized in mapping:
        return mapping[normalized]
    return raw_value


def _extract_date_of_incorporation_parts(record: Dict[str, Any]) -> tuple[str, str, str]:
    raw_date = _as_string(record.get("date_of_incorporation"))
    if raw_date:
        parsed = _try_parse_date(raw_date)
        if parsed is not None:
            month, day, year = parsed
            print(
                f"MA debug -> parsed date_of_incorporation='{raw_date}' into month='{month}' day='{day}' year='{year}'"
            )
            return month, day, year

    month = _as_string(record.get("date_of_incorporation_month"))
    day = _as_string(record.get("date_of_incorporation_day"))
    year = _as_string(record.get("date_of_incorporation_year"))
    return month, day, year


def _try_parse_date(value: str) -> Optional[tuple[str, str, str]]:
    text = _as_string(value)
    if not text:
        return None

    known_formats = ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%Y/%m/%d")
    for fmt in known_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return str(parsed.month), str(parsed.day), str(parsed.year)
        except ValueError:
            continue

    if text.isdigit() and len(text) == 8:
        # MMDDYYYY
        month = str(int(text[:2]))
        day = str(int(text[2:4]))
        year = text[4:]
        return month, day, year

    return None


def _normalize_date_part(value: str) -> str:
    text = _as_string(value)
    if not text:
        return ""
    if text.isdigit() and len(text) in {1, 2}:
        return str(int(text))
    return text


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


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace(":", "").strip().lower().split())
