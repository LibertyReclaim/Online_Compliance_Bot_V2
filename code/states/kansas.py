"""Kansas filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

KS_HOLDER_INFO_URL = "https://missingmoney.ks.gov/app/holder-info"
KS_HIPAA_LABEL = "Does this report include records that are subject to the HIPAA Privacy Rule?"


class KansasAutomationError(RuntimeError):
    """Raised when KS automation cannot reliably continue."""


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

    await page.goto(KS_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_ks_holder_info_page(page, record, errors)

    if errors:
        raise KansasAutomationError("KS holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("KS debug -> clicking Next after KS holder info completed")
    await click_next(page, "after KS holder info")
    await _upload_naupa_file(page, naupa_path)


async def run_kansas(
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


async def _fill_ks_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _as_string(record.get(field.key))
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        _print_text_debug(field, value)
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "KS"))

    report_type = _as_string(record.get("report_type"))
    if not report_type:
        errors.append("report_type is required for 'Report Type'.")
    else:
        print(f"KS debug -> Report Type mapped from report_type='{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "KS"))

    await _set_report_year_if_enabled(page, _as_string(record.get("report_year")))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    print(f"KS debug -> This is a Negative Report mapped from negative_report='{('Yes' if negative else 'No')}'")
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: set_radio_field(page, "This is a Negative Report", negative, "KS"))

    if not negative:
        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print(f"KS debug -> Total Dollar Amount Remitted mapped from amount_to_remit='{amount}'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "KS"))

    funds = _as_string(record.get("funds_remitted_via"))
    if funds:
        print(f"KS debug -> Funds Remitted Via mapped from funds_remitted_via='{funds}'")
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", funds, "KS"))

    hipaa = _as_bool(record.get("hipaa_privacy_rule"))
    if hipaa is None:
        hipaa = False
    print(f"KS debug -> HIPAA mapped from hipaa_privacy_rule='{('Yes' if hipaa else 'No')}'")
    await _set_hipaa_if_available(page, hipaa)


def _print_text_debug(field: _TextFieldSpec, value: str) -> None:
    if field.label == "Email Address Confirmation":
        print(f"KS debug -> Email Confirmation reused from email='{value}'")
    else:
        print(f"KS debug -> {field.label} mapped from {field.key}='{value}'")


async def _set_report_year_if_enabled(page: Page, report_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "KS")
        locator = row.locator("select").first
        if await locator.count() <= 0:
            return

        disabled_or_readonly = await locator.evaluate(
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
        if disabled_or_readonly:
            print("KS debug -> Report Year disabled; skipping")
            return

        if not report_year:
            errors_msg = "KS debug -> Report Year blank; skipping"
            print(errors_msg)
            return

        print(f"KS debug -> Report Year mapped from report_year='{report_year}'")
        try:
            await locator.select_option(value=report_year)
            return
        except Exception:
            pass

        try:
            await locator.select_option(label=report_year)
        except Exception:
            return
    except Exception:
        print("KS debug -> Report Year disabled; skipping")


async def _set_hipaa_if_available(page: Page, value: bool) -> None:
    for label in (KS_HIPAA_LABEL, "Does this report include records that are subject to the HIPAA Privacy Rule", "HIPAA Privacy Rule"):
        try:
            await set_radio_field(page, label, value, "KS")
            return
        except Exception:
            continue
    print("KS debug -> HIPAA radio not found; skipping optional HIPAA field")


async def _wait_for_upload_page(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
        print("KS debug -> reached holder-upload page")
        return
    except PlaywrightTimeoutError:
        pass

    for text in ("Upload File", "Upload This Report"):
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            print("KS debug -> reached holder-upload page")
            return
        except PlaywrightTimeoutError:
            continue

    raise KansasAutomationError("KS did not reach holder-upload after holder info Next.")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    await _wait_for_upload_page(page)

    if not file_path.exists():
        raise KansasAutomationError(f"KS NAUPA file does not exist: {file_path}")

    print(f"KS debug -> uploading KS NAUPA file: {file_path}")
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
        raise KansasAutomationError("Could not find KS upload file input.")

    await page.wait_for_timeout(1500)
    print("KS debug -> NAUPA uploaded; clicking upload-page Next")
    await click_next(page, "after KS upload")
    await _wait_for_preview_or_signature(page)
    print("KS debug -> reached holder-preview page")
    print("KS finished - waiting for manual signature")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise KansasAutomationError("KS upload did not reach holder-preview or signature prompt.")


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
    raise KansasAutomationError(f"Could not find a clickable Next control {context}.")


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
