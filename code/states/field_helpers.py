"""Shared strict field targeting helpers for state runners."""

from __future__ import annotations

from typing import Optional

from playwright.async_api import Locator, Page


class FieldResolutionError(RuntimeError):
    """Raised when strict row-scoped field resolution fails."""


def _normalize(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace(":", "").strip().lower().split())


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    joined = ", \"'\", ".join(f"'{piece}'" for piece in pieces)
    return f"concat({joined})"


async def _visible_count(locator: Locator) -> int:
    total = await locator.count()
    visible = 0
    for i in range(total):
        node = locator.nth(i)
        if await node.is_visible():
            visible += 1
    return visible


async def locate_strict_row_for_label(page: Page, label_text: str, control_type: str, state_tag: str) -> tuple[Locator, str]:
    normalized = _normalize(label_text)
    xpath = (
        "xpath=//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ*:',"
        "'abcdefghijklmnopqrstuvwxyz  '), "
        f"{_xpath_literal(normalized)})]"
    )
    label_nodes = page.locator(xpath)
    label_count = await label_nodes.count()

    for i in range(label_count):
        label_node = label_nodes.nth(i)
        if not await label_node.is_visible():
            continue

        matched_label = " ".join((await label_node.inner_text()).split())

        candidates = (
            label_node.locator("xpath=ancestor::div[1]").first,
            label_node.locator("xpath=ancestor::*[contains(@class,'form-group')][1]").first,
            label_node.locator("xpath=ancestor::*[contains(@class,'row')][1]").first,
            label_node.locator("xpath=ancestor::td[1]").first,
            label_node.locator("xpath=ancestor::tr[1]").first,
            label_node.locator("xpath=ancestor::li[1]").first,
            label_node.locator("xpath=ancestor::div[2]").first,
            label_node.locator("xpath=ancestor::div[3]").first,
        )

        for row in candidates:
            if await row.count() <= 0:
                continue

            text_count = await _visible_count(row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea"))
            select_count = await _visible_count(row.locator("select"))
            radio_count = await _visible_count(row.locator("input[type='radio']"))
            checkbox_count = await _visible_count(row.locator("input[type='checkbox']"))

            reason = ""
            accepted = False
            if control_type == "text":
                accepted = 1 <= text_count <= 2
                reason = "too broad" if text_count > 2 else "no text controls"
            elif control_type == "dropdown":
                accepted = select_count == 1
                reason = "too broad" if select_count > 1 else "no dropdown"
            elif control_type == "radio":
                accepted = 2 <= radio_count <= 6
                reason = "too broad" if radio_count > 6 else "no/too few radios"
            elif control_type == "checkbox":
                accepted = 1 <= checkbox_count <= 3
                reason = "too broad" if checkbox_count > 3 else "no checkbox"

            if accepted:
                print(
                    f"{state_tag} debug -> field='{label_text}' matched_label='{matched_label}' "
                    f"candidate_text={text_count} candidate_selects={select_count} "
                    f"candidate_radios={radio_count} candidate_checkboxes={checkbox_count} accepted='strict row'"
                )
                return row, matched_label

            print(
                f"{state_tag} debug -> field='{label_text}' matched_label='{matched_label}' "
                f"candidate_text={text_count} candidate_selects={select_count} "
                f"candidate_radios={radio_count} candidate_checkboxes={checkbox_count} rejected='{reason}'"
            )

    raise FieldResolutionError(f"Unable to locate strict row for '{label_text}' ({control_type}).")


async def fill_text_field(page: Page, label_text: str, value: str, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "text", state_tag)
    control = row.locator("input:not([type='hidden']):not([type='radio']):not([type='checkbox']), textarea").first
    await control.fill(value)
    actual = await control.input_value()
    if actual != value:
        raise FieldResolutionError(f"{state_tag} failed value verification for '{label_text}'. Expected '{value}' got '{actual}'.")
    print(f"{state_tag} debug -> field='{label_text}' type='TEXT' value='{value}' strategy='strict row {matched}'")


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
        raise FieldResolutionError(f"{state_tag} dropdown verification failed for '{label_text}'. selected='{selected}' requested='{value}'.")

    print(f"{state_tag} debug -> field='{label_text}' type='DROPDOWN' value='{value}' strategy='strict row {matched} + {strategy}'")


async def set_radio_field(page: Page, label_text: str, yes_no_value: bool, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "radio", state_tag)
    radios = row.locator("input[type='radio']")
    target = "yes" if yes_no_value else "no"

    target_radio: Optional[Locator] = None
    count = await radios.count()
    for i in range(count):
        radio = radios.nth(i)
        text = _normalize(await _radio_text(row, radio))
        if text == target or target in text:
            target_radio = radio
            break

    if target_radio is None:
        raise FieldResolutionError(f"{state_tag} unable to find radio '{target}' for '{label_text}'.")

    await target_radio.set_checked(True, force=True)

    checked_text = ""
    for i in range(count):
        radio = radios.nth(i)
        if await radio.is_checked():
            checked_text = await _radio_text(row, radio)
            break

    if _normalize(checked_text) != target and target not in _normalize(checked_text):
        raise FieldResolutionError(f"{state_tag} radio verification failed for '{label_text}'. expected='{target}' actual='{checked_text}'.")

    print(f"{state_tag} debug -> field='{label_text}' type='RADIO' value='{'Yes' if yes_no_value else 'No'}' strategy='strict row {matched}'")


async def set_checkbox_field(page: Page, label_text: str, checked: bool, state_tag: str) -> None:
    row, matched = await locate_strict_row_for_label(page, label_text, "checkbox", state_tag)
    control = row.locator("input[type='checkbox']").first
    await control.set_checked(checked, force=True)
    if await control.is_checked() != checked:
        raise FieldResolutionError(f"{state_tag} checkbox verification failed for '{label_text}'.")
    print(f"{state_tag} debug -> field='{label_text}' type='CHECKBOX' value='{checked}' strategy='strict row {matched}'")


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
