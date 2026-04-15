"""Excel loading helpers for Online_Compliance_Bot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


HOLDER_FILE_NAME = "holder_information.xlsx"
PAYMENT_FILE_NAME = "payment_file.xlsx"


class ExcelLoaderError(RuntimeError):
    """Raised when workbook data is missing or invalid."""


def load_holder_records(project_root: Path) -> list[dict[str, Any]]:
    """Load holder_information.xlsx into cleaned record dictionaries.

    New structure:
    - `id` is the internal merge key.
    - `holder_id` is the website Holder ID value (may be blank).
    """
    holder_path = project_root / HOLDER_FILE_NAME
    _require_exists(holder_path)

    holder_df = pd.read_excel(holder_path)
    holder_df = _clean_dataframe(holder_df)

    _require_columns(
        holder_df,
        required_columns=["id", "company_name", "holder_id"],
        workbook_name=HOLDER_FILE_NAME,
    )

    # Preserve columns as-is; do not alias/overwrite id <-> holder_id.
    return holder_df.to_dict(orient="records")


def load_payment_records(project_root: Path) -> list[dict[str, Any]]:
    """Load payment_file.xlsx into cleaned record dictionaries.

    New structure:
    - `id` is the internal merge key to holder workbook.
    """
    payment_path = project_root / PAYMENT_FILE_NAME
    _require_exists(payment_path)

    payment_df = pd.read_excel(payment_path)
    payment_df = _clean_dataframe(payment_df)

    _require_columns(
        payment_df,
        required_columns=["payment_id", "id", "company_name", "state_code", "report_year"],
        workbook_name=PAYMENT_FILE_NAME,
    )

    # Preserve columns as-is; do not alias/overwrite id <-> holder_id.
    return payment_df.to_dict(orient="records")


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    cleaned.columns = [str(col).strip() for col in cleaned.columns]

    for column in cleaned.columns:
        cleaned[column] = cleaned[column].map(_clean_cell)

    return cleaned


def _clean_cell(value: Any) -> Any:
    if pd.isna(value):
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, float) and value.is_integer():
        return int(value)

    return value


def _require_exists(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required workbook not found: {path}")


def _require_columns(df: pd.DataFrame, required_columns: list[str], workbook_name: str) -> None:
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise ExcelLoaderError(f"Workbook {workbook_name} is missing required columns: {', '.join(missing)}")
