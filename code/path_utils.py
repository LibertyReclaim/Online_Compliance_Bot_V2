"""Path helpers for NAUPA report file discovery."""

from __future__ import annotations

from pathlib import Path


def build_naupa_filename(company_name: str, state_code: str, report_year: str | int) -> str:
    """Return `[Company Name]_[STATE] [YEAR] NAUPA.txt`."""
    company = str(company_name).strip()
    state = str(state_code).strip().upper()
    year = str(report_year).strip()
    return f"{company}_{state} {year} NAUPA.txt"


def build_naupa_path(project_root: Path, company_name: str, state_code: str, report_year: str | int) -> Path:
    """Return `project_root / company_name / filename`."""
    filename = build_naupa_filename(company_name=company_name, state_code=state_code, report_year=report_year)
    company_folder = project_root / str(company_name).strip()
    return company_folder / filename
