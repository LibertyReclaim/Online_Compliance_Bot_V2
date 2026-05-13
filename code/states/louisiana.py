"""Louisiana filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

LA_HOLDER_INFO_URL = "https://louisiana.findyourunclaimedproperty.com/app/holder-info"
LA_HIPAA_LABEL = "Does this report include records that are subject to the HIPAA Privacy Rule"


class LouisianaAutomationError(RuntimeError):
    """Raised when LA automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Holder Name", "holder_name", required=True),
    _TextFieldSpec("Holder Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Contact Name", "contact_name", required=True),
    _TextFieldSpec("Contact Phone Number", "contact_phone", required=True),
    _TextFieldSpec("Phone Extension", "phone_extension"),
    _TextFieldSpec("Email", "email", required=True),
    _TextFieldSpec("Email Confirmation", "email_confirmation", required=True),
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

    await page.goto(LA_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Holder Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_la_holder_info_page(page, record, errors)

    if errors:
        raise LouisianaAutomationError("LA holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("LA debug -> clicking Next after LA holder info completed")
    await click_next(page, "after LA holder info")
    await _upload_naupa_file(page, naupa_path)


async def run_louisiana(
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


async def _fill_la_holder_info_page(page: Page, record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value = _resolve_text_field_value(record, field)
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            continue
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "LA"))

    state_incorporation = _as_string(record.get("state_incorporation")) or _as_string(record.get("state_of_incorporation"))
    if state_incorporation:
        print(f"LA debug -> State of Incorporation from Excel='{state_incorporation}'")
        await _guarded(
            errors,
            "dropdown 'State of Incorporation'",
            lambda: select_dropdown_field(page, "State of Incorporation", state_incorporation, "LA"),
        )

    report_type = _normalize_report_type(_as_string(record.get("report_type")))
    if report_type:
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "LA"))

    await _set_report_year_if_enabled(page, _as_string(record.get("report_year")))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    await _set_negative_report(page, negative, errors)

    if not negative:
        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print(f"LA debug -> Total Dollar Amount Remitted mapped from amount_to_remit='{amount}'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "LA"))

        shares = _as_string(record.get("total_shares")) or "0"
        print(f"LA debug -> Number of Shares Reported mapped from total_shares='{shares}'")
        await _guarded(errors, "text 'Number of Shares Reported'", lambda: fill_text_field(page, "Number of Shares Reported", shares, "LA"))

        funds = _normalize_funds(_as_string(record.get("funds_remitted_via")))
        await _guarded(errors, "dropdown 'Funds Remitted Via'", lambda: select_dropdown_field(page, "Funds Remitted Via", funds, "LA"))

    hipaa_raw = _as_string(record.get("hipaa_privacy_rule")) or _as_string(record.get("includes_hipaa_records"))
    hipaa = _as_bool(hipaa_raw)
    if hipaa is None:
        hipaa = False
    print(f"LA debug -> HIPAA mapped from hipaa_privacy_rule/includes_hipaa_records='{('Yes' if hipaa else 'No')}'")
    await _set_hipaa_if_available(page, hipaa)


def _resolve_text_field_value(record: Dict[str, Any], field: _TextFieldSpec) -> str:
    value = _as_string(record.get(field.key))
    if field.key == "holder_tax_id" and not value:
        return _as_string(record.get("fein"))
    if field.key == "email_confirmation" and not value:
        fallback_email = _as_string(record.get("email"))
        if fallback_email:
            print("LA debug -> Email Confirmation missing; using Email value")
            return fallback_email
    return value


async def _set_report_year_if_enabled(page: Page, report_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "LA")
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
            print("LA debug -> skipping Report Year (disabled field)")
            return

        if not report_year:
            return

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
        print("LA debug -> skipping Report Year (disabled field)")


async def _set_negative_report(page: Page, value: bool, errors: list[str]) -> None:
    try:
        await set_radio_field(page, "This is a Negative Report", value, "LA")
        return
    except Exception:
        pass

    await _guarded(errors, "radio 'Is this a negative report?'", lambda: set_radio_field(page, "Is this a negative report?", value, "LA"))


async def _set_hipaa_if_available(page: Page, value: bool) -> None:
    for label in (LA_HIPAA_LABEL, "HIPAA Privacy Rule"):
        try:
            await set_radio_field(page, label, value, "LA")
            return
        except Exception:
            continue
    print("LA debug -> HIPAA radio not found; skipping optional HIPAA field")


async def _wait_for_upload_page(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
        print("LA debug -> reached holder-upload page")
        return
    except PlaywrightTimeoutError:
        pass

    for text in ("Upload File", "Upload This Report"):
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            print("LA debug -> reached holder-upload page")
            return
        except PlaywrightTimeoutError:
            continue

    raise LouisianaAutomationError("LA did not reach holder-upload after holder info Next.")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    await _wait_for_upload_page(page)

    if not file_path.exists():
        raise LouisianaAutomationError(f"LA NAUPA file does not exist: {file_path}")

    print(f"LA debug -> uploading NAUPA file: {file_path}")
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
        raise LouisianaAutomationError("Could not find LA upload file input.")

    await page.wait_for_timeout(1500)
    print("LA debug -> NAUPA uploaded; clicking upload-page Next")
    await click_next(page, "after LA upload")
    await _wait_for_preview_or_signature(page)
    print("LA debug -> reached holder-preview; waiting for manual signature")
    print("LA finished - waiting for manual signature")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise LouisianaAutomationError("LA upload did not reach holder-preview or signature prompt.")


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
    raise LouisianaAutomationError(f"Could not find a clickable Next control {context}.")


async def _guarded(errors: list[str], field_desc: str, action: Any) -> None:
    try:
        result = action()
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception as exc:
        errors.append(f"Failed to set {field_desc}: {exc}")


def _normalize_report_type(raw_value: str) -> str:
    mapping = {
        "annual": "Annual Report",
        "annual report": "Annual Report",
    }
    return mapping.get(_normalize(raw_value), raw_value)


def _normalize_funds(raw_value: str) -> str:
    mapping = {
        "check": "Check",
        "ach": "ACH",
        "wire": "Wire",
        "wire transfer": "Wire",
        "online": "Online",
        "electronic": "Online",
    }
    normalized = _normalize(raw_value)
    return mapping.get(normalized, raw_value or "Check")


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
