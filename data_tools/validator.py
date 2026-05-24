"""Migration CSV validation rules for NetSuite record types."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from netsuite.field_registry import NETSUITE_FIELDS, get_required_fields, resolve_record_type

logger = logging.getLogger(__name__)

# Date patterns NetSuite accepts (CSV Import requires MM/DD/YYYY)
_DATE_PATTERNS = [
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),        # MM/DD/YYYY (preferred)
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),             # ISO 8601 (will warn, needs transform)
    re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$"),     # DD-Mon-YYYY
    re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"),       # DD.MM.YYYY (European)
]
_PREFERRED_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# US phone: at least 10 digits
_PHONE_DIGITS_RE = re.compile(r"\d")

_BOOLEAN_TRUE = {"true", "t", "yes", "y", "1", "x"}
_BOOLEAN_FALSE = {"false", "f", "no", "n", "0", ""}

MAX_ERRORS_PER_FIELD = 5  # Stop reporting after this many per field to avoid noise


@dataclass
class ValidationIssue:
    row: int  # 1-based row number (header = row 0, first data row = 1)
    field: str
    message: str
    value: str = ""


@dataclass
class ValidationResult:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "errors": [
                {"row": e.row, "field": e.field, "message": e.message, "value": e.value}
                for e in self.errors
            ],
            "warnings": [
                {"row": w.row, "field": w.field, "message": w.message, "value": w.value}
                for w in self.warnings
            ],
            "stats": self.stats,
        }

    def summary(self) -> str:
        lines = [
            f"Validation Summary",
            f"  Total rows  : {self.stats.get('total_rows', '?')}",
            f"  Valid rows  : {self.stats.get('valid_rows', '?')}",
            f"  Error rows  : {self.stats.get('error_rows', '?')}",
            f"  Errors      : {len(self.errors)}",
            f"  Warnings    : {len(self.warnings)}",
        ]
        if self.errors:
            lines.append("\nErrors (first 20):")
            for e in self.errors[:20]:
                lines.append(f"  Row {e.row:>4} | {e.field:<30} | {e.message} (value: '{e.value[:50]}')")
        if self.warnings:
            lines.append("\nWarnings (first 20):")
            for w in self.warnings[:20]:
                lines.append(f"  Row {w.row:>4} | {w.field:<30} | {w.message} (value: '{w.value[:50]}')")
        return "\n".join(lines)


class CSVValidator:
    """Validates a migration CSV file against a NetSuite record type's field rules."""

    def validate(self, file_path: str | Path, record_type: str) -> ValidationResult:
        """
        Validate a CSV file for a given NetSuite record type.

        Returns a ValidationResult with errors, warnings, and stats.
        """
        file_path = Path(file_path)
        result = ValidationResult()

        # Resolve record type
        canonical = resolve_record_type(record_type)
        if canonical is None:
            result.errors.append(
                ValidationIssue(
                    row=0,
                    field="record_type",
                    message=f"Unknown record type '{record_type}'. Valid types: {', '.join(NETSUITE_FIELDS.keys())}",
                )
            )
            result.stats = {"total_rows": 0, "valid_rows": 0, "error_rows": 0}
            return result

        ns_fields = NETSUITE_FIELDS[canonical]

        # Load CSV
        try:
            df = pd.read_csv(str(file_path), dtype=str)
        except Exception as exc:
            result.errors.append(
                ValidationIssue(row=0, field="file", message=f"Cannot read CSV: {exc}")
            )
            result.stats = {"total_rows": 0, "valid_rows": 0, "error_rows": 0}
            return result

        df = df.fillna("")
        total_rows = len(df)
        columns = [c.strip().lower() for c in df.columns]
        original_columns = list(df.columns)

        # Rename columns to lowercase for matching
        df.columns = pd.Index(columns)

        # ---- 1. Check required fields ----
        required = get_required_fields(canonical)
        for req_field in required:
            if req_field not in columns:
                result.errors.append(
                    ValidationIssue(
                        row=0,
                        field=req_field,
                        message=f"Required field '{req_field}' is missing from the CSV columns.",
                    )
                )

        # ---- 2. Warn about unknown columns ----
        unknown_cols = [col for col in columns if col not in ns_fields and col != "externalid"]
        for col in unknown_cols[:10]:  # Limit noise
            result.warnings.append(
                ValidationIssue(
                    row=0,
                    field=col,
                    message=f"Column '{col}' is not a recognized NetSuite {canonical} field.",
                )
            )

        # ---- 3. Per-row validation ----
        error_rows: set[int] = set()
        field_error_counts: dict[str, int] = {}

        for row_idx, row in df.iterrows():
            row_num = int(row_idx) + 1  # 1-based

            for col in columns:
                if col not in ns_fields:
                    continue

                field_def = ns_fields[col]
                raw_val = str(row.get(col, "")).strip()
                field_type = field_def.get("type", "text")
                max_len = field_def.get("max_length")
                is_required = field_def.get("required", False)

                err_count = field_error_counts.get(col, 0)

                # 3a. Required field blank
                if is_required and not raw_val:
                    if err_count < MAX_ERRORS_PER_FIELD:
                        result.errors.append(
                            ValidationIssue(
                                row=row_num,
                                field=col,
                                message=f"Required field '{col}' is blank.",
                                value=raw_val,
                            )
                        )
                        field_error_counts[col] = err_count + 1
                    error_rows.add(row_num)
                    continue

                if not raw_val:
                    continue  # Optional + blank = OK

                # 3b. Max length
                if max_len and len(raw_val) > max_len:
                    if err_count < MAX_ERRORS_PER_FIELD:
                        result.errors.append(
                            ValidationIssue(
                                row=row_num,
                                field=col,
                                message=f"Value exceeds max length of {max_len} (actual: {len(raw_val)}).",
                                value=raw_val,
                            )
                        )
                        field_error_counts[col] = err_count + 1
                    error_rows.add(row_num)

                # 3c. Type-specific validation
                if field_type == "date":
                    _validate_date(raw_val, col, row_num, result, field_error_counts)

                elif field_type in ("currency", "decimal"):
                    _validate_numeric(raw_val, col, row_num, result, field_error_counts, error_rows, allow_negative=(field_type == "decimal"))

                elif field_type == "integer":
                    _validate_integer(raw_val, col, row_num, result, field_error_counts, error_rows)

                elif field_type == "checkbox":
                    _validate_boolean(raw_val, col, row_num, result, field_error_counts, error_rows)

                elif field_type == "email":
                    if not _EMAIL_RE.match(raw_val):
                        if field_error_counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
                            result.warnings.append(
                                ValidationIssue(
                                    row=row_num,
                                    field=col,
                                    message="Value does not look like a valid email address.",
                                    value=raw_val,
                                )
                            )
                            field_error_counts[col] = field_error_counts.get(col, 0) + 1

                elif field_type == "phone":
                    digits = _PHONE_DIGITS_RE.findall(raw_val)
                    if len(digits) < 7:
                        if field_error_counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
                            result.warnings.append(
                                ValidationIssue(
                                    row=row_num,
                                    field=col,
                                    message="Phone number has fewer than 7 digits — may be invalid.",
                                    value=raw_val,
                                )
                            )
                            field_error_counts[col] = field_error_counts.get(col, 0) + 1

        # ---- 4. Duplicate externalid check ----
        if "externalid" in columns:
            ext_ids = df["externalid"][df["externalid"] != ""]
            dupes = ext_ids[ext_ids.duplicated()]
            if not dupes.empty:
                for dupe_val in dupes.unique()[:5]:
                    dup_rows = df.index[df["externalid"] == dupe_val].tolist()
                    result.errors.append(
                        ValidationIssue(
                            row=dup_rows[1] + 1 if len(dup_rows) > 1 else 0,
                            field="externalid",
                            message=f"Duplicate externalid '{dupe_val}' found in rows {[r+1 for r in dup_rows]}.",
                            value=dupe_val,
                        )
                    )
                    for r in dup_rows[1:]:
                        error_rows.add(r + 1)

        # ---- 5. Stats ----
        valid_rows = total_rows - len(error_rows)
        result.stats = {
            "total_rows": total_rows,
            "valid_rows": valid_rows,
            "error_rows": len(error_rows),
            "columns_found": len(columns),
            "required_fields": required,
            "record_type": canonical,
        }

        logger.info(
            "Validation complete: %d rows, %d errors, %d warnings.",
            total_rows,
            len(result.errors),
            len(result.warnings),
        )
        return result


# ---------------------------------------------------------------------------
# Helper validators
# ---------------------------------------------------------------------------

def _validate_date(
    val: str,
    col: str,
    row_num: int,
    result: ValidationResult,
    counts: dict[str, int],
) -> None:
    matched = any(p.match(val) for p in _DATE_PATTERNS)
    if not matched:
        if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
            result.errors.append(
                ValidationIssue(
                    row=row_num,
                    field=col,
                    message="Date value is not in a recognized format. NetSuite CSV Import requires MM/DD/YYYY.",
                    value=val,
                )
            )
            counts[col] = counts.get(col, 0) + 1
    elif not _PREFERRED_DATE_RE.match(val):
        if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
            result.warnings.append(
                ValidationIssue(
                    row=row_num,
                    field=col,
                    message="Date format is not MM/DD/YYYY — transform before import to avoid errors.",
                    value=val,
                )
            )
            counts[col] = counts.get(col, 0) + 1


def _validate_numeric(
    val: str,
    col: str,
    row_num: int,
    result: ValidationResult,
    counts: dict[str, int],
    error_rows: set[int],
    allow_negative: bool = False,
) -> None:
    clean = val.replace(",", "").replace("$", "").replace(" ", "")
    try:
        num = float(clean)
        if not allow_negative and num < 0:
            if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
                result.warnings.append(
                    ValidationIssue(
                        row=row_num,
                        field=col,
                        message="Negative value in a currency/amount field — confirm this is intentional.",
                        value=val,
                    )
                )
                counts[col] = counts.get(col, 0) + 1
        if val != clean:
            if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
                result.warnings.append(
                    ValidationIssue(
                        row=row_num,
                        field=col,
                        message=f"Currency/numeric value contains formatting characters (commas or $ signs). Strip before import. Cleaned: '{clean}'",
                        value=val,
                    )
                )
                counts[col] = counts.get(col, 0) + 1
    except ValueError:
        if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
            result.errors.append(
                ValidationIssue(
                    row=row_num,
                    field=col,
                    message=f"Value is not a valid number.",
                    value=val,
                )
            )
            counts[col] = counts.get(col, 0) + 1
        error_rows.add(row_num)


def _validate_integer(
    val: str,
    col: str,
    row_num: int,
    result: ValidationResult,
    counts: dict[str, int],
    error_rows: set[int],
) -> None:
    try:
        int(val.replace(",", ""))
    except ValueError:
        if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
            result.errors.append(
                ValidationIssue(
                    row=row_num,
                    field=col,
                    message="Value must be an integer.",
                    value=val,
                )
            )
            counts[col] = counts.get(col, 0) + 1
        error_rows.add(row_num)


def _validate_boolean(
    val: str,
    col: str,
    row_num: int,
    result: ValidationResult,
    counts: dict[str, int],
    error_rows: set[int],
) -> None:
    norm = val.lower().strip()
    if norm not in _BOOLEAN_TRUE and norm not in _BOOLEAN_FALSE:
        if counts.get(col, 0) < MAX_ERRORS_PER_FIELD:
            result.warnings.append(
                ValidationIssue(
                    row=row_num,
                    field=col,
                    message=f"Boolean field has unexpected value '{val}'. Use TRUE/FALSE, T/F, YES/NO, Y/N, or 1/0.",
                    value=val,
                )
            )
            counts[col] = counts.get(col, 0) + 1
