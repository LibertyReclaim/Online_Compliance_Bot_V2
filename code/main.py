"""Entry point for Online_Compliance_Bot."""

from __future__ import annotations

import asyncio
import inspect
import traceback
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

from excel_loader import load_holder_records, load_payment_records
from path_utils import build_naupa_path
from state_registry import get_registered_states, get_state_runner
from states.field_helpers import wait_for_holder_form_ready


def _project_root() -> Path:
    # Script is expected to be run from /code (`py main.py`), but this keeps it robust.
    return Path(__file__).resolve().parent.parent


def _index_holders_by_internal_id(holder_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for holder in holder_records:
        internal_id = str(holder.get("id", "")).strip()
        if internal_id:
            indexed[internal_id] = holder
    return indexed


def _is_negative_report(amount_to_remit: Any) -> bool:
    raw = str(amount_to_remit).strip().replace(",", "")
    if not raw:
        return False
    try:
        return float(raw) < 0
    except ValueError:
        return False




def _wrap_page_goto_for_readiness(page: Page, state_code: str) -> None:
    original_goto = page.goto

    async def _goto_with_readiness(*args: Any, **kwargs: Any) -> Any:
        result = await original_goto(*args, **kwargs)
        target_url = ""
        if args:
            target_url = str(args[0] or "")
        elif "url" in kwargs:
            target_url = str(kwargs.get("url") or "")

        if "/holder-info" in target_url:
            await wait_for_holder_form_ready(page, state_code)

        return result

    page.goto = _goto_with_readiness  # type: ignore[assignment]


async def _run_state_task(page: Page, holder: dict[str, Any], payment: dict[str, Any], naupa_path: Path) -> None:
    state_code = str(payment.get("state_code", "")).strip().upper()
    payment_id = payment.get("payment_id")
    print(f"Starting {state_code} in new tab...")

    try:
        print(f"Dispatching state runner for payment_id={payment_id}, state={state_code}")
        runner = get_state_runner(state_code)
        runner_name = getattr(runner, "__qualname__", getattr(runner, "__name__", str(runner)))
        runner_module = getattr(runner, "__module__", "")
        print(f"Selected runner: {runner_module}.{runner_name}")

        if state_code == "CA":
            print("Starting CA navigation...")

        _wrap_page_goto_for_readiness(page, state_code)
        result = runner(page=page, holder_row=holder, payment_row=payment, naupa_file_path=naupa_path)

        if inspect.isawaitable(result):
            await result
        else:
            raise TypeError(f"State runner for {state_code} must be async when using async Playwright.")

        print(f"{state_code} finished - waiting for manual signature")
    except Exception:
        print(f"\n=== AUTOMATION ERROR ({state_code}) ===")
        print(traceback.format_exc())
        print("Automation failed for this state tab. Browser will remain open so you can manually inspect the page.")




async def _run_payment_task(
    browser: Browser,
    semaphore: asyncio.Semaphore,
    holder: dict[str, Any],
    payment: dict[str, Any],
    naupa_path: Path,
) -> None:
    async with semaphore:
        page = await browser.new_page()
        await _run_state_task(page, holder, payment, naupa_path)


async def run() -> None:
    project_root = _project_root()

    holder_records = load_holder_records(project_root)
    payment_records = load_payment_records(project_root)
    holders_by_internal_id = _index_holders_by_internal_id(holder_records)

    print(f"Registered states: {', '.join(get_registered_states())}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        tasks: list[asyncio.Task[None]] = []
        semaphore = asyncio.Semaphore(3)

        for payment in payment_records:
            state_code = str(payment.get("state_code", "")).strip().upper()
            internal_id = str(payment.get("id", "")).strip()
            company_name = str(payment.get("company_name", "")).strip()
            report_year = payment.get("report_year", "")

            if not state_code:
                print(f"Skipping payment_id={payment.get('payment_id')} (missing state_code)")
                continue

            if not internal_id:
                print(f"Skipping payment_id={payment.get('payment_id')} (missing internal id)")
                continue

            holder = holders_by_internal_id.get(internal_id)
            if holder is None:
                print(f"Skipping payment_id={payment.get('payment_id')} (internal id '{internal_id}' not found)")
                continue

            if not company_name:
                company_name = str(holder.get("company_name", "")).strip()

            naupa_path = build_naupa_path(
                project_root=project_root,
                company_name=company_name,
                state_code=state_code,
                report_year=report_year,
            )

            report_kind = "negative" if _is_negative_report(payment.get("amount_to_remit")) else "positive"
            print(
                f"Queueing payment_id={payment.get('payment_id')} state={state_code} "
                f"internal_id={internal_id} report={report_kind} naupa='{naupa_path}'"
            )

            tasks.append(
                asyncio.create_task(_run_payment_task(browser, semaphore, holder, payment, naupa_path))
            )

        if tasks:
            await asyncio.gather(*tasks)
        else:
            print("No valid payment rows to run.")

        print("All state tabs processed. Browser will remain open for manual review/signature.")
        print("Press Ctrl+C to close browser and exit.")

        try:
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            print("\nShutdown requested by user. Closing browser...")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
