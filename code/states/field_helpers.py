"""Shared strict field targeting helpers for state runners."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from playwright.async_api import Locator, Page


class FieldResolutionError(RuntimeError):
    """Raised when strict row-scoped field resolution fails."""


_MAX_VISIBLE_SCAN = 15
_MAX_ANCESTOR_DEPTH = 6
_MAX_ROW_CHARS = 500
_MAX_TOTAL_CONTROLS = 6


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace(":", "").strip().lower().split())


def _normalize_value_for_compare(value: str) -> str:
    lowered = str(value).strip().lower()
    compact = re.sub(r"[\s\-\(\)\./]", "", lowered)
    return re.sub(r"[^a-z0-9]", "", compact)


def _looks_like_amount_field(label_text: str) -> bool:
    label = _normalize(label_text)
    tokens = ("amount", "cash", "remitted", "reported", "dollars", "shares", "total")
    return any(token in label for token in tokens)


def _parse_currency_value(value: str) -> Optional[Decimal]:
    raw = str(value).strip()
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").replace(" ", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _short(text: str, max_len: int = 90) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return f"{collapsed[:max_len - 3]}..."


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


async def _safe_inner_text(locator: Locator) -> str:
    try:
        return " ".join((await locator.inner_text()).split())
    except Exception:
        return ""


async def _safe_count(locator: Locator, cap: int = _MAX_VISIBLE_SCAN) -> int:
    try:
        total = await locator.count()
        return min(total, cap)
    except Exception:
        return 0


async def _visible_count(locator: Locator, cap: int = _MAX_VISIBLE_SCAN) -> int:
    """Best-effort bounded visible count that never raises outward."""
    try:
        total = await locator.count()
    except Exception:
        return 0

    inspect = min(total, cap)
    visible = 0
    for i in range(inspect):
        try:
            if await locator.nth(i).is_visible():
                visible += 1
        except asyncio.CancelledError:
            return visible
        except Exception:
            continue
    return visible


async def _collect_label_candidates(page: Page, label_text: str) -> list[tuple[Locator, str]]:
    normalized = _normalize(label_text)
    tag_xpath = "self::label or self::span or self::div or self::p or self::strong or self::b or self::td"
    xpath = (
        "xpath=//*[(" + tag_xpath + ") and contains(translate(normalize-space(string(.)), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:', 'abcdefghijklmnopqrstuvwxyz  '), "
        f"{_xpath_literal(normalized)})]"
    )

    nodes = page.locator(xpath)
    count = await _safe_count(nodes, cap=40)
    candidates: list[tuple[Locator, str, int]] = []

    for i in range(count):
        node = nodes.nth(i)
        try:
            if not await node.is_visible():
                continue
        except Exception:
            continue

        text = await _safe_inner_text(node)
        if not text:
            continue

        normalized_text = _normalize(text)
        if normalized not in normalized_text:
            continue

        label_len = max(len(normalized), 1)
        if len(normalized_text) > (label_len * 4):
            continue

        score = 2
        if normalized_text == normalized:
            score = 0
        elif normalized_text.startswith(normalized):
            score = 1

        candidates.append((node, text, score))

    candidates.sort(key=lambda item: (item[2], len(item[1])))
    return [(node, text) for node, text, _ in candidates]


async def locate_strict_row_for_label(page: Page, label_text: str, control_type: str, state_tag: str) -> tuple[Locator, str]:
    label_candidates = await _collect_label_candidates(page, label_text)

    if not label_candidates:
        raise FieldResolutionError(f"Unable to locate strict row for '{label_text}' ({control_type}): no matching label anchors.")

    for label_node, matched_label in label_candidates:
        ancestor_candidates = (
            label_node.locator("xpath=ancestor::div[1]").first,
            label_node.locator("xpath=ancestor::*[contains(@class,'form-group')][1]").first,
            label_node.locator("xpath=ancestor::*[contains(@class,'row')][1]").first,
            label_node.locator("xpath=ancestor::td[1]").first,
            label_node.locator("xpath=ancestor::tr[1]").first,
            label_node.locator("xpath=ancestor::li[1]").first,
            label_node.locator("xpath=ancestor::div[2]").first,
            label_node.locator("xpath=ancestor::div[3]").first,
            label_node.locator("xpath=ancestor::div[4]").first,
            label_node.locator("xpath=ancestor::div[5]").first,
        )

        for depth, row in enumerate(ancestor_candidates):
            if depth >= _MAX_ANCESTOR_DEPTH:
                break

            try:
                if await row.count() <= 0:
                    continue
            except Exception:
                continue

            row_text = await _safe_inner_text(row)
            candidate_chars = len(_normalize(row_text))

            if control_type != "dropdown" and candidate_chars > _MAX_ROW_CHARS:
                print(
                    f"{state_tag} debug -> field='{label_text}' matched_label='{_short(matched_label)}' "
                    f"candidate_chars={candidate_chars} controls=? rejected='row text too long'"
                )
                continue

            total_controls = await _safe_count(
                row.locator("input:not([type='hidden']), textarea, select"),
                cap=_MAX_TOTAL_CONTROLS + 1,
            )
            if total_controls > _MAX_TOTAL_CONTROLS:
                print(
                    f"{state_tag} debug -> field='{label_text}' matched_label='{_short(matched_label)}' "
                    f"candidate_chars={candidate_chars} controls={total_controls} rejected='too many controls'"
                )
                continue

            text_count = await _visible_count(
                row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea")
            )
            select_count = await _visible_count(row.locator("select"))
            radio_count = await _visible_count(row.locator("input[type='radio']"))
            checkbox_count = await _visible_count(row.locator("input[type='checkbox']"))

            accepted = False
            reason = ""
            if control_type == "text":
                accepted = 1 <= text_count <= 2 and select_count <= 2
                reason = "text controls out of range"
            elif control_type == "dropdown":
                accepted = select_count == 1 and text_count <= 1 and radio_count <= 2 and checkbox_count <= 1
                reason = "dropdown structure mismatch"
            elif control_type == "radio":
                accepted = 2 <= radio_count <= 6
                reason = "radio count out of range"
            elif control_type == "checkbox":
                accepted = 1 <= checkbox_count <= 3
                reason = "checkbox count out of range"

            if accepted:
                print(
                    f"{state_tag} debug -> field='{label_text}' matched_label='{_short(matched_label)}' "
                    f"candidate_chars={candidate_chars} candidate_text={text_count} candidate_selects={select_count} "
                    f"candidate_radios={radio_count} candidate_checkboxes={checkbox_count} accepted='strict row'"
                )
                return row, matched_label

            print(
                f"{state_tag} debug -> field='{label_text}' matched_label='{_short(matched_label)}' "
                f"candidate_chars={candidate_chars} candidate_text={text_count} candidate_selects={select_count} "
                f"candidate_radios={radio_count} candidate_checkboxes={checkbox_count} rejected='{reason}'"
            )

    raise FieldResolutionError(f"Unable to locate strict row for '{label_text}' ({control_type}).")


async def fill_text_field(page: Page, label_text: str, value: str, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "text", state_tag)
    control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first

    await control.fill("")
    await control.type(value)
    await control.blur()

    actual = await control.input_value()

    if _looks_like_amount_field(label_text):
        expected_num = _parse_currency_value(value)
        actual_num = _parse_currency_value(actual)
        if expected_num is not None and actual_num is not None and expected_num == actual_num:
            print(
                f"{state_tag} debug -> field='{label_text}' raw_actual='{actual}' raw_expected='{value}' "
                f"numeric_actual='{actual_num}' numeric_expected='{expected_num}' accepted='currency match' strategy='strict row {_short(matched)}'"
            )
            return

    expected_norm = _normalize_value_for_compare(value)
    actual_norm = _normalize_value_for_compare(actual)

    if expected_norm != actual_norm:
        raise FieldResolutionError(
            f"{state_tag} text verification failed for '{label_text}'. raw_expected='{value}' raw_actual='{actual}' "
            f"normalized_expected='{expected_norm}' normalized_actual='{actual_norm}'."
        )

    status = "formatted match" if value != actual else "exact match"
    print(
        f"{state_tag} debug -> field='{label_text}' raw_actual='{actual}' raw_expected='{value}' "
        f"normalized_actual='{actual_norm}' normalized_expected='{expected_norm}' accepted='{status}' strategy='strict row {_short(matched)}'"
    )


async def select_dropdown_field(page: Page, label_text: str, value: str, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "dropdown", state_tag)
    control = row.locator("select").first

    strategy = "select_option(label)"
    try:
        await control.select_option(label=value)
    except Exception:
        options = await control.evaluate_all(
            "els => els[0] ? Array.from(els[0].options).map(o => ({text:(o.textContent||'').trim(), value:o.value})) : []"
        )
        target = _normalize(value)
        matched_value: Optional[str] = None
        strategy = ""

        for option in options:
            text = _normalize(str(option.get("text", "")))
            if text == target:
                matched_value = str(option.get("value", ""))
                strategy = "normalized exact"
                break

        if matched_value is None:
            for option in options:
                text = _normalize(str(option.get("text", "")))
                if target and (target in text or text in target):
                    matched_value = str(option.get("value", ""))
                    strategy = "partial"
                    break

        if matched_value is None:
            raise FieldResolutionError(f"{state_tag} unable to select '{value}' for '{label_text}'.")

        await control.select_option(value=matched_value)

    selected = await control.evaluate("el => (el.selectedOptions[0]?.textContent || '').trim()")
    if _normalize(value) not in _normalize(str(selected)) and _normalize(str(selected)) not in _normalize(value):
        raise FieldResolutionError(
            f"{state_tag} dropdown verification failed for '{label_text}'. selected='{selected}' requested='{value}'."
        )

    print(
        f"{state_tag} debug -> field='{label_text}' type='DROPDOWN' value='{value}' "
        f"strategy='strict row {_short(matched)} + {strategy}'"
    )


async def set_radio_field(page: Page, label_text: str, yes_no_value: bool, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "radio", state_tag)
    radios = row.locator("input[type='radio']")
    target = "yes" if yes_no_value else "no"

    target_radio: Optional[Locator] = None
    count = await radios.count()
    for i in range(min(count, _MAX_VISIBLE_SCAN)):
        radio = radios.nth(i)
        text = _normalize(await _radio_text(row, radio))
        if text == target or target in text:
            target_radio = radio
            break

    if target_radio is None:
        raise FieldResolutionError(f"{state_tag} unable to find radio '{target}' for '{label_text}'.")

    await target_radio.set_checked(True, force=True)

    checked_text = ""
    for i in range(min(count, _MAX_VISIBLE_SCAN)):
        radio = radios.nth(i)
        try:
            if await radio.is_checked():
                checked_text = await _radio_text(row, radio)
                break
        except Exception:
            continue

    if _normalize(checked_text) != target and target not in _normalize(checked_text):
        raise FieldResolutionError(
            f"{state_tag} radio verification failed for '{label_text}'. expected='{target}' actual='{checked_text}'."
        )

    print(
        f"{state_tag} debug -> field='{label_text}' type='RADIO' value='{'Yes' if yes_no_value else 'No'}' "
        f"strategy='strict row {_short(matched)}'"
    )


async def set_checkbox_field(page: Page, label_text: str, checked: bool, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "checkbox", state_tag)
    control = row.locator("input[type='checkbox']").first
    await control.set_checked(checked, force=True)
    if await control.is_checked() != checked:
        raise FieldResolutionError(f"{state_tag} checkbox verification failed for '{label_text}'.")
    print(f"{state_tag} debug -> field='{label_text}' type='CHECKBOX' value='{checked}' strategy='strict row {_short(matched)}'")


async def _radio_text(row: Locator, radio: Locator) -> str:
    radio_id = await radio.get_attribute("id")
    if radio_id:
        by_for = row.locator(f"label[for='{radio_id}']").first
        if await by_for.count() > 0:
            text = " ".join((await by_for.inner_text()).split())
            if text:
                return text

    sibling = radio.locator("xpath=following-sibling::label[1]").first
    if await sibling.count() > 0:
        text = " ".join((await sibling.inner_text()).split())
        if text:
            return text

    parent = radio.locator("xpath=ancestor::label[1]").first
    if await parent.count() > 0:
        text = " ".join((await parent.inner_text()).split())
        if text:
            return text

    value = await radio.get_attribute("value")
    return "" if value is None else value


async def wait_for_field_enabled(
    page: Page,
    label_text: str,
    control_type: str,
    state_tag: str,
    timeout_ms: int = 10_000,
) -> Locator:
    row, _ = await locate_strict_row_for_label(page, label_text, control_type, state_tag)

    if control_type == "dropdown":
        control = row.locator("select").first
    elif control_type == "text":
        control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
    elif control_type == "radio":
        control = row.locator("input[type='radio']").first
    else:
        control = row.locator("input[type='checkbox']").first

    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await control.is_enabled():
                return control
        except Exception:
            pass
        await asyncio.sleep(0.25)

    raise FieldResolutionError(f"{state_tag} field '{label_text}' stayed disabled.")
