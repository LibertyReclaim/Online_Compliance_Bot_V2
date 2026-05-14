"""Alabama filing runner for the Online_Compliance_Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from states.field_helpers import fill_text_field, locate_strict_row_for_label, select_dropdown_field, set_radio_field

AL_HOLDER_INFO_URL = "https://alabama.findyourunclaimedproperty.com/app/holder-info"
AL_HIPAA_LABEL = "Does this report include records that are subject to the HIPAA Privacy Rule?"


class AlabamaAutomationError(RuntimeError):
    """Raised when AL automation cannot reliably continue."""


@dataclass(frozen=True)
class _TextFieldSpec:
    label: str
    key: str
    required: bool = False


_TEXT_FIELDS: tuple[_TextFieldSpec, ...] = (
    _TextFieldSpec("Business Name", "holder_name", required=True),
    _TextFieldSpec("Business Tax ID", "holder_tax_id", required=True),
    _TextFieldSpec("Business ID", "holder_id"),
    _TextFieldSpec("Business Contact", "contact_name", required=True),
    _TextFieldSpec("Contact Phone No.", "contact_phone", required=True),
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

    await page.goto(AL_HOLDER_INFO_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_after_navigation_ms)
    await page.get_by_label("Business Name").first.wait_for(timeout=20_000)

    errors: list[str] = []
    await _fill_al_holder_info_page(page, holder_row, record, errors)

    if errors:
        raise AlabamaAutomationError("AL holder-info form completed with errors:\n- " + "\n- ".join(errors))

    print("AL debug -> clicking Next after AL holder info completed")
    await click_next(page, "after AL holder info")
    await _upload_naupa_file(page, naupa_path)


async def run_alabama(
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


async def _fill_al_holder_info_page(page: Page, holder_row: Dict[str, Any], record: Dict[str, Any], errors: list[str]) -> None:
    for field in _TEXT_FIELDS:
        value, mapped_from = _resolve_text_field_value(record, field)
        if not value:
            if field.required:
                errors.append(f"{field.key} is required for '{field.label}'.")
            else:
                print(f"AL debug -> {field.label} mapped from {field.key}='NOTHING'")
            continue
        print(f"AL debug -> {field.label} mapped from {mapped_from}='{value}'")
        await _guarded(errors, f"text '{field.label}'", lambda f=field, v=value: fill_text_field(page, f.label, v, "AL"))

    state_value = _as_string(holder_row.get("state"))
    if state_value:
        print(f"AL debug -> State mapped from holder file state='{state_value}'")
        await _guarded(errors, "dropdown 'State'", lambda: select_dropdown_field(page, "State", state_value, "AL"))
    else:
        print("AL debug -> State mapped from holder file state='NOTHING'")

    report_type = _as_string(record.get("report_type"))
    if report_type:
        print(f"AL debug -> Report Type mapped from report_type='{report_type}'")
        await _guarded(errors, "dropdown 'Report Type'", lambda: select_dropdown_field(page, "Report Type", report_type, "AL"))
    else:
        errors.append("report_type is required for 'Report Type'.")

    await _set_report_year_if_enabled(page, _as_string(record.get("report_year")))

    negative = _as_bool(record.get("negative_report"))
    if negative is None:
        negative = False
    print(f"AL debug -> This is a Negative Report mapped from negative_report='{('Yes' if negative else 'No')}'")
    await _guarded(errors, "radio 'This is a Negative Report'", lambda: set_radio_field(page, "This is a Negative Report", negative, "AL"))

    if not negative:
        amount = _as_string(record.get("amount_to_remit"))
        if not amount:
            errors.append("amount_to_remit is required for 'Total Dollar Amount Remitted'.")
        else:
            print(f"AL debug -> Total Dollar Amount Remitted mapped from amount_to_remit='{amount}'")
            await _guarded(errors, "text 'Total Dollar Amount Remitted'", lambda: fill_text_field(page, "Total Dollar Amount Remitted", amount, "AL"))

    hipaa_raw = _as_string(record.get("hipaa_privacy_rule")) or _as_string(record.get("includes_hipaa_records"))
    hipaa = _as_bool(hipaa_raw)
    if hipaa is None:
        hipaa = False
    print(f"AL debug -> HIPAA mapped from hipaa_privacy_rule/includes_hipaa_records='{('Yes' if hipaa else 'No')}'")
    await _set_hipaa_if_available(page, hipaa)

    print("AL debug -> Alabama has no Funds Remitted Via field; skipping")


def _resolve_text_field_value(record: Dict[str, Any], field: _TextFieldSpec) -> tuple[str, str]:
    value = _as_string(record.get(field.key))
    mapped_from = field.key
    if field.key == "email_confirmation" and not value:
        fallback_email = _as_string(record.get("email"))
        if fallback_email:
            return fallback_email, "email_confirmation/email"
    return value, mapped_from


async def _set_report_year_if_enabled(page: Page, report_year: str) -> None:
    try:
        row, _ = await locate_strict_row_for_label(page, "Report Year", "dropdown", "AL")
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
            print("AL debug -> skipping Report Year (disabled field)")
            return

        if not report_year:
            return

        print(f"AL debug -> Report Year mapped from report_year='{report_year}'")
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
        print("AL debug -> skipping Report Year (disabled field)")


async def _set_hipaa_if_available(page: Page, value: bool) -> None:
    for label in (AL_HIPAA_LABEL, "Does this report include records that are subject to the HIPAA Privacy Rule", "HIPAA Privacy Rule"):
        try:
            await set_radio_field(page, label, value, "AL")
            return
        except Exception:
            continue
    print("AL debug -> HIPAA radio not found; skipping optional HIPAA field")


async def _wait_for_upload_page(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-upload**", timeout=20_000)
        print("AL debug -> reached holder-upload page")
        return
    except PlaywrightTimeoutError:
        pass

    for text in ("Upload File", "Upload This Report"):
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            print("AL debug -> reached holder-upload page")
            return
        except PlaywrightTimeoutError:
            continue

    raise AlabamaAutomationError("AL did not reach holder-upload after holder info Next.")


async def _upload_naupa_file(page: Page, file_path: Path) -> None:
    await _wait_for_upload_page(page)

    if not file_path.exists():
        raise AlabamaAutomationError(f"AL NAUPA file does not exist: {file_path}")

    print(f"AL debug -> uploading NAUPA file: {file_path}")
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
        raise AlabamaAutomationError("Could not find AL upload file input.")

    await page.wait_for_timeout(1500)
    print("AL debug -> NAUPA uploaded; clicking upload-page Next")
    await click_next(page, "after AL upload")
    await _wait_for_preview_or_signature(page)
    print("AL debug -> reached holder-preview; waiting for manual signature")
    print("AL finished - waiting for manual signature")


async def _wait_for_preview_or_signature(page: Page) -> None:
    try:
        await page.wait_for_url("**/app/holder-preview**", timeout=20_000)
        return
    except PlaywrightTimeoutError:
        pass

    signature_text = page.get_by_text("Electronic Signature Required")
    if await signature_text.first.count() > 0 and await signature_text.first.is_visible():
        return

    raise AlabamaAutomationError("AL upload did not reach holder-preview or signature prompt.")


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
    raise AlabamaAutomationError(f"Could not find a clickable Next control {context}.")


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
