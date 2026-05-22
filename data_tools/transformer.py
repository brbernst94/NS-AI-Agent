"""Data transformation and NetSuite-ready import file generation."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

from netsuite.field_registry import resolve_record_type, NETSUITE_FIELDS

logger = logging.getLogger(__name__)


class DataTransformer:
    """Apply mappings, clean data, and generate NetSuite CSV Import-ready files."""

    # ------------------------------------------------------------------
    # Main transform pipeline
    # ------------------------------------------------------------------

    def transform(
        self,
        source_df: pd.DataFrame,
        mapping: dict[str, str | None],
        record_type: str,
    ) -> pd.DataFrame:
        """
        Full transformation pipeline:
        1. Apply field mapping (rename / drop columns)
        2. Clean and normalize data values
        3. Return NetSuite-ready DataFrame

        mapping: {source_column: netsuite_field_id | None}
                 None values → drop that column
        """
        canonical = resolve_record_type(record_type)
        if canonical is None:
            logger.warning("Unknown record type '%s' — skipping type-specific transforms.", record_type)
            canonical = record_type

        ns_fields = NETSUITE_FIELDS.get(canonical, {})

        # Step 1: Apply mapping
        df = source_df.copy()
        df = df.fillna("")

        rename_map: dict[str, str] = {}
        drop_cols: list[str] = []
        for src_col, ns_col in mapping.items():
            if src_col not in df.columns:
                continue
            if ns_col is None:
                drop_cols.append(src_col)
            else:
                rename_map[src_col] = ns_col

        df = df.drop(columns=drop_cols, errors="ignore")
        df = df.rename(columns=rename_map)

        # Step 2: Type-aware cleaning
        for col in df.columns:
            col_lower = col.lower()
            field_def = ns_fields.get(col_lower, {})
            field_type = field_def.get("type", "text") if field_def else "text"

            if field_type == "date":
                df[col] = df[col].apply(_normalize_date)
            elif field_type in ("currency", "decimal"):
                df[col] = df[col].apply(_normalize_numeric)
            elif field_type == "integer":
                df[col] = df[col].apply(_normalize_integer)
            elif field_type == "checkbox":
                df[col] = df[col].apply(_normalize_boolean)
            elif field_type == "phone":
                df[col] = df[col].apply(_normalize_phone)
            elif field_type == "email":
                df[col] = df[col].apply(lambda v: str(v).strip().lower())
            elif field_type in ("text", "textarea"):
                df[col] = df[col].apply(_clean_text)
            else:
                df[col] = df[col].apply(lambda v: str(v).strip())

        # Step 3: General cleanup
        # Remove completely blank rows
        df = df[df.apply(lambda row: any(str(v).strip() for v in row), axis=1)]

        logger.info(
            "Transform complete: %d rows, %d columns for %s.",
            len(df),
            len(df.columns),
            canonical,
        )
        return df

    # ------------------------------------------------------------------
    # Generate NetSuite import CSV
    # ------------------------------------------------------------------

    def generate_netsuite_csv(
        self,
        df: pd.DataFrame,
        record_type: str,
        output_path: str | Path,
    ) -> Path:
        """
        Write a NetSuite CSV Import-ready CSV file.

        - Column headers are the NetSuite internal field IDs (lowercase)
        - Encoding: UTF-8 with BOM (handles special characters in NS importer)
        - Line endings: CRLF for Windows compatibility
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Ensure column names are lowercase (NetSuite expects lowercase internal IDs)
        df = df.copy()
        df.columns = pd.Index([str(c).lower().strip() for c in df.columns])

        # Write with UTF-8 BOM encoding
        df.to_csv(
            str(output_path),
            index=False,
            encoding="utf-8-sig",
            lineterminator="\r\n",
        )

        logger.info(
            "Generated NetSuite import CSV: %s (%d rows, %d columns).",
            output_path,
            len(df),
            len(df.columns),
        )
        return output_path

    # ------------------------------------------------------------------
    # Quick-transform convenience
    # ------------------------------------------------------------------

    def auto_transform_csv(
        self,
        input_path: str | Path,
        record_type: str,
        output_path: str | Path | None = None,
        source_system: str = "generic",
    ) -> tuple[pd.DataFrame, Path]:
        """
        Load a CSV, auto-suggest mappings, apply them, and write output file.
        Returns (transformed_df, output_path).
        """
        from data_tools.mapper import FieldMapper

        input_path = Path(input_path)
        if output_path is None:
            output_path = input_path.parent / f"{input_path.stem}_netsuite_import.csv"
        output_path = Path(output_path)

        df = pd.read_csv(str(input_path), dtype=str).fillna("")
        source_fields = list(df.columns)

        mapper = FieldMapper()
        suggestions = mapper.suggest_mapping(source_fields, source_system, record_type)

        # Build mapping: only accept suggestions with confidence >= 0.6
        mapping: dict[str, str | None] = {}
        for src_field, info in suggestions.items():
            if info["confidence"] >= 0.6 and info["netsuite_field"]:
                mapping[src_field] = info["netsuite_field"]
            else:
                mapping[src_field] = None  # Drop unmapped

        transformed = self.transform(df, mapping, record_type)
        out = self.generate_netsuite_csv(transformed, record_type, output_path)
        return transformed, out


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_date(val: Any) -> str:
    """Normalize date values to MM/DD/YYYY for NetSuite."""
    if not val or str(val).strip() == "":
        return ""
    s = str(val).strip()

    # Already MM/DD/YYYY
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", s):
        # Zero-pad
        parts = s.split("/")
        return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"

    # ISO 8601: YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"

    # DD-Mon-YYYY (e.g., 15-Jan-2023)
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    m2 = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", s)
    if m2:
        mon = months.get(m2.group(2).lower())
        if mon:
            return f"{mon}/{int(m2.group(1)):02d}/{m2.group(3)}"

    # DD/MM/YYYY vs MM/DD/YYYY — ambiguous; try to detect European format
    m3 = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m3:
        return f"{int(m3.group(2)):02d}/{int(m3.group(1)):02d}/{m3.group(3)}"

    # YYYYMMDD
    m4 = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m4:
        return f"{m4.group(2)}/{m4.group(3)}/{m4.group(1)}"

    # Try pandas parsing as fallback
    try:
        ts = pd.Timestamp(s)
        return f"{ts.month:02d}/{ts.day:02d}/{ts.year}"
    except Exception:
        pass

    return s  # Return as-is if we can't parse


def _normalize_numeric(val: Any) -> str:
    """Strip currency symbols, commas, and spaces from numeric values."""
    if not val or str(val).strip() == "":
        return ""
    s = str(val).strip()
    # Remove $, £, €, commas, spaces
    clean = re.sub(r"[\$£€,\s]", "", s)
    # Handle parentheses for negatives: (1234.56) → -1234.56
    m = re.match(r"^\((.+)\)$", clean)
    if m:
        clean = "-" + m.group(1)
    try:
        num = float(clean)
        # Format: no trailing zeros beyond 2 decimals, but keep up to 6
        if num == int(num):
            return str(int(num))
        return f"{num:.6f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def _normalize_integer(val: Any) -> str:
    """Convert to integer string."""
    if not val or str(val).strip() == "":
        return ""
    s = str(val).strip()
    clean = re.sub(r"[,\s]", "", s)
    try:
        return str(int(float(clean)))
    except ValueError:
        return s


def _normalize_boolean(val: Any) -> str:
    """Normalize boolean-ish values to TRUE/FALSE for NetSuite."""
    if not val or str(val).strip() == "":
        return "FALSE"
    s = str(val).strip().lower()
    if s in ("true", "t", "yes", "y", "1", "x", "checked", "on"):
        return "TRUE"
    if s in ("false", "f", "no", "n", "0", "unchecked", "off"):
        return "FALSE"
    return "FALSE"


def _normalize_phone(val: Any) -> str:
    """Normalize phone numbers to a consistent format."""
    if not val or str(val).strip() == "":
        return ""
    s = str(val).strip()
    # Strip everything but digits, +, (, ), -, space, ext
    # Preserve ext/x references
    ext_match = re.search(r"(?:ext|x)[\s.#]*(\d+)", s, re.IGNORECASE)
    ext_suffix = f" x{ext_match.group(1)}" if ext_match else ""
    digits = re.sub(r"\D", "", s.split("ext")[0].split(" x")[0] if " x" in s.lower() else s)
    # US: 10 or 11 digits
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}{ext_suffix}"
    # International or non-standard: return cleaned original
    return s


def _clean_text(val: Any) -> str:
    """Strip leading/trailing whitespace and normalize internal whitespace."""
    if not val:
        return ""
    s = str(val).strip()
    # Collapse multiple spaces/tabs to single space
    s = re.sub(r"[ \t]+", " ", s)
    return s
