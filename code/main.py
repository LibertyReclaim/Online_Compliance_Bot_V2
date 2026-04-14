"""Entry point for Online_Compliance_Bot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from excel_loader import load_holder_records, load_payment_records
from path_utils import build_naupa_path
from state_registry import get_state_runner


def _project_root() -> Path:
    # Script is expected to be run from /code (`py main.py`), but this keeps it robust.
    return Path(__file__).resolve().parent.parent


def _index_holders_by_id(holder_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for holder in holder_records:
        holder_id = str(holder.get("holder_id", "")).strip()
        if holder_id:
            indexed[holder_id] = holder
    return indexed


def _is_negative_report(amount_to_remit: Any) -> bool:
    raw = str(amount_to_remit).strip().replace(",", "")
    if not raw:
        return False
    try:
        return float(raw) < 0
    except ValueError:
        return False


def run() -> None:
    project_root = _project_root()

    holder_records = load_holder_records(project_root)
    payment_records = load_payment_records(project_root)
    holders_by_id = _index_holders_by_id(holder_records)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        for payment in payment_records:
            state_code = str(payment.get("state_code", "")).strip().upper()
            holder_id = str(payment.get("holder_id", "")).strip()
            company_name = str(payment.get("company_name", "")).strip()
            report_year = payment.get("report_year", "")

            if not state_code:
                print(f"Skipping payment_id={payment.get('payment_id')} (missing state_code)")
                continue

            if not holder_id:
                print(f"Skipping payment_id={payment.get('payment_id')} (missing holder_id)")
                continue

            holder = holders_by_id.get(holder_id)
            if holder is None:
                print(f"Skipping payment_id={payment.get('payment_id')} (holder_id '{holder_id}' not found)")
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
                f"Running payment_id={payment.get('payment_id')} state={state_code} "
                f"holder_id={holder_id} report={report_kind} naupa='{naupa_path}'"
            )

            runner = get_state_runner(state_code)
            page = browser.new_page()
            runner(page=page, holder_row=holder, payment_row=payment, naupa_file_path=naupa_path)

            print(
                f"NY flow reached post-upload step for payment_id={payment.get('payment_id')}. "
                "Review preview/signature page manually in the open browser window."
            )

        print("All payment rows processed. Browser will remain open for manual review.")
        input("Press Enter to close the browser and exit...")
        browser.close()


if __name__ == "__main__":
    run()
