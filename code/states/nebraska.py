"""Nebraska filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

NE_HOLDER_INFO_URL = "https://nebraskalostcash.nebraska.gov/app/holder-info"


class NebraskaAutomationError(RuntimeError):
    """Raised when NE automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Holder ID", "holder_id"),
    _TextFieldSpec("Contact Name", "contact_name", required=True),
    _TextFieldSpec("Contact Phone Number", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email Address", "email", required=True),
    _TextFieldSpec("Email Address Confirmation", "email", required=True),
)


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

    await page.goto(NE_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_ne_holder_info_page(page, record, errors)

    if errors:
        raise NebraskaAutomationError("NE holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("NE debug -> clicking holder-info NEXT")
    await click_next(page, "after NE holder info")
    await _upload_naupa_file(page, naupa_path)


async def run_nebraska(
    page: Page,
    holder_row: Dict[str, Any],
    payment_row: Dict[str, Any],
    naupa_file_path: str | Path,
    *,
    wait_after_navigation_ms: int = 1500,
) -> None:
    return await run(
        page,
        holder_row,
        payment_row,
        naupa_file_path,
        wait_after_navigation_ms=wait_after_navigation_ms,
    )


async def _fill_ne_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        _print_text_debug(field, value)
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "NE"))

    report_type = _as_string(record.get("report_type"))
    if not report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        print(f"NE debug -> Report Type mapped from report_type='{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "NE"))

    await _set_report_year_if_enabled(page, _as_string(record.get("report_year")), errors)

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        errors.append("negative_report is required for 'This is a Negative Report' and must be Yes or No.")
        negative = False
    print(f"NE debug -> Negative Report mapped from negative_report='{('Yes' if negative else 'No')}'")
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: set_radio_field(page, "This is a Negative Report", negative, "NE"))

    if not negative:
        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print(f"NE debug -> Total Dollar Amount Remitted mapped from amount_to_remit='{amount}'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "NE"))

    funds = _as_string(record.get("funds_remitted_via")) or "Online"
    print(f"NE debug -> Funds Remitted Via mapped from funds_remitted_via='{funds}'")
    await _select_optional_dropdown_if_enabled(page, errors, "Funds Remitted Via", funds)


def _print_text_debug(field: _TextFieldSpec, value: str) -> None:
    if field.label == "Email Address Confirmation":
        print(f"NE debug -> Email Address Confirmation reused from email='{value}'")
    else:
        print(f"NE debug -> {field.label} mapped from {field.key}='{value}'")


async def _set_report_year_if_enabled(page: Page, report_year: str, errors: list[str]) -> None:
    last_error: Optional[Exception] = None

    for control_type in ("text", "dropdown"):
        try:
            row, _ = await locate_strict_row_for_label(page, "Report Year", control_type, "NE")
            control = (
                row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
                if control_type == "text"
                else row.locator("select").first
            )
            if await control.count() <= 0:
                return
            if await _is_disabled_or_readonly(control):
                print("NE debug -> Report Year disabled; skipping")
                return
            if not report_year:
                errors.append("report_year is required for 'Report Year'.")
                return

            print(f"NE debug -> Report Year mapped from report_year='{report_year}'")
            if control_type == "text":
                await _guarded(errors, "text 'Report Year'", lambda: fill_text_field(page, "Report Year", report_year, "NE"))
            else:
                await _guarded(errors, "dropdown 'Report Year'", lambda: select_dropdown_field(page, "Report Year", report_year, "NE"))
            return
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        errors.append(f"Failed to set Report Year: {last_error}")


async def _select_optional_dropdown_if_enabled(page: Page, errors: list[str], label: str, value: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, label, "dropdown", "NE")
        control = row.locator("select").first
        if await control.count() <= 0:
            return
        if await _is_disabled_or_readonly(control):
            return
    except Exception:
        return

    await _guarded(errors, f"dropdown '{label}'", lambda: select_dropdown_field(page, label, value, "NE"))


async def _is_disabled_or_readonly(locator: Any) -> bool:
    return bool(
        await locator.evaluate(
            """
            el => Boolean(
                el.disabled ||
                el.readOnly ||
                el.getAttribute('readonly') !== null ||
                el.getAttribute('disabled') !== null ||
                el.getAttribute('aria-disabled') === 'true'
            )
            """
        )
    )


async def _wait_for_upload_page(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
        print("NE debug -> reached holder-upload page")
        return
    except PlaywrightTimeoutError:
        pass

    for text in ("Upload File", "Upload This Report"):
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            print("NE debug -> reached holder-upload page")
            return
        except PlaywrightTimeoutError:
            continue

    raise NebraskaAutomationError("NE did not reach holder-upload after holder info Next.")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    await _wait_for_upload_page(page)

    if not file_path.exists():
        raise NebraskaAutomationError(f"NE NAUPA file does not exist: {file_path}")

    print(f"NE debug -> uploading NE NAUPA file: {file_path}")
    selectors = ["input[type='file']", "input[type='file']:visible", "input[accept]", "input[type='file'][accept]"]
    uploaded = False
    for _ in range(3):
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            try:
                await locator.first.set_input_files(str(file_path))
                uploaded = True
                break
            except Exception:
                continue
        if uploaded:
            break
        await page.wait_for_timeout(800)

    if not uploaded:
        raise NebraskaAutomationError("Could not find NE upload file input.")

    await page.wait_for_timeout(1500)
    print("NE debug -> clicking upload-page NEXT")
    await click_next(page, "after NE upload")
    await _wait_for_preview_or_signature(page)
    print("NE debug -> reached holder-preview page")
    print("NE finished - waiting for manual signature")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise NebraskaAutomationError("NE upload did not reach holder-preview or signature prompt.")


async def click_next(page: Page, context: str) -> None:
    for candidate in (
        page.get_by_role("button", name="Next", exact=True),
        page.get_by_role("button", name="next"),
        page.locator("button:has-text('Next')"),
        page.locator("button:has-text('NEXT')"),
        page.locator("input[type='submit'][value='Next']"),
        page.locator("input[type='submit'][value='NEXT']"),
    ):
        count = await candidate.count()
        for i in range(count):
            target = candidate.nth(i)
            if not await target.is_visible():
                continue
            if not await target.is_enabled():
                continue
            await target.click(timeout=10_000)
            await page.wait_for_timeout(1000)
            return
    raise NebraskaAutomationError(f"Could not find a clickable Next control {context}.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


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
