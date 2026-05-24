"""Field mapping logic: suggest and apply field mappings from source to NetSuite."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

import pandas as pd

from netsuite.field_registry import NETSUITE_FIELDS, resolve_record_type

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source-system-specific heuristic mappings
# Keyed by source_system (lowercase) -> dict[source_field_pattern -> ns_field]
# ---------------------------------------------------------------------------
_SOURCE_SYSTEM_HINTS: dict[str, dict[str, str]] = {
    "salesforce": {
        "accountname": "companyname",
        "account_name": "companyname",
        "accountnumber": "entityid",
        "account_number": "entityid",
        "billingstreet": "addr1",
        "billingcity": "city",
        "billingstate": "state",
        "billingpostalcode": "zip",
        "billingzip": "zip",
        "billing_zip": "zip",
        "billing_postal_code": "zip",
        "billingcountry": "country",
        "shippingstreet": "addr1",
        "phone": "phone",
        "phonenumber": "phone",
        "fax": "fax",
        "website": "url",
        "description": "comments",
        "type": "category",
        "industry": "category",
        "annualrevenue": "creditlimit",
        "ownerid": "salesrep",
        "contact_firstname": "firstname",
        "contact_lastname": "lastname",
        "contact_email": "email",
        "contact_phone": "phone",
        "email": "email",
        "emailaddress": "email",
        "email_address": "email",
        "opportunityname": "memo",
        "amount": "amount",
        "closedate": "trandate",
        "invoicenumber": "tranid",
        "invoicedate": "trandate",
        "duedate": "duedate",
        "productcode": "itemid",
        "productname": "displayname",
        "unitprice": "rate",
        "quantity": "quantity",
    },
    "quickbooks": {
        "customername": "companyname",
        "customer_name": "companyname",
        "customerid": "externalid",
        "vendorname": "companyname",
        "vendor_name": "companyname",
        "vendorid": "externalid",
        "itemname": "itemid",
        "item_name": "itemid",
        "item_description": "salesdescription",
        "invoicenum": "tranid",
        "invoice_num": "tranid",
        "invoicedate": "trandate",
        "invoice_date": "trandate",
        "duedate": "duedate",
        "terms": "terms",
        "memo": "memo",
        "billeddate": "trandate",
        "amount": "amount",
        "rate": "rate",
        "qty": "quantity",
        "account": "incomeaccount",
        "expense_account": "expenseaccount",
        "ap_account": "payablesaccount",
        "taxid": "taxid",
        "1099": "is1099eligible",
        "address1": "addr1",
        "address2": "addr2",
        "city": "city",
        "state": "state",
        "zip": "zip",
        "country": "country",
        "email": "email",
        "phone": "phone",
        "fax": "fax",
    },
    "sap": {
        "kunnr": "externalid",       # Customer number
        "name1": "companyname",
        "name2": "addr2",
        "stras": "addr1",
        "ort01": "city",
        "regio": "state",
        "pstlz": "zip",
        "land1": "country",
        "telf1": "phone",
        "telfx": "fax",
        "smtp_addr": "email",
        "lifnr": "externalid",        # Vendor number
        "ktokk": "category",
        "stcd1": "taxid",
        "matnr": "itemid",
        "maktx": "salesdescription",
        "meins": "stockunit",
        "bklas": "costingmethod",
        "vbeln": "tranid",            # Document number
        "bldat": "trandate",
        "faedt": "duedate",
        "wrbtr": "amount",
        "menge": "quantity",
        "netpr": "rate",
        "bukrs": "subsidiary",
    },
    "dynamics": {
        "accountnumber": "entityid",
        "accountname": "companyname",
        "primarycontactname": "companyname",
        "telephone1": "phone",
        "telephone2": "altphone",
        "fax": "fax",
        "emailaddress1": "email",
        "websiteurl": "url",
        "address1_line1": "addr1",
        "address1_line2": "addr2",
        "address1_city": "city",
        "address1_stateorprovince": "state",
        "address1_postalcode": "zip",
        "address1_country": "country",
        "creditlimit": "creditlimit",
        "paymenttermscode": "terms",
        "transactioncurrencyid": "currency",
        "ownerid": "salesrep",
        "description": "comments",
        "productnumber": "itemid",
        "productname": "displayname",
        "description2": "salesdescription",
        "price": "salesprice",
        "standardcost": "cost",
        "invoicenumber": "tranid",
        "invoicedate": "trandate",
        "dueby": "duedate",
        "quantity": "quantity",
        "priceperunit": "rate",
        "extendedamount": "amount",
    },
}


def _normalize(name: str) -> str:
    """Normalize a field name for fuzzy comparison."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def _similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two normalized strings."""
    return SequenceMatcher(None, a, b).ratio()


class FieldMapper:
    """Suggests and applies field mappings from a source system to NetSuite."""

    def suggest_mapping(
        self,
        source_fields: list[str],
        source_system: str = "generic",
        target_record_type: str = "customer",
    ) -> dict[str, dict[str, Any]]:
        """
        Suggest NetSuite field mappings for source fields.

        Returns dict keyed by source_field:
            {
                "netsuite_field": str | None,
                "confidence": float,    # 0.0–1.0
                "method": str,          # "exact", "system_hint", "fuzzy", "unmapped"
                "notes": str,
            }
        """
        canonical = resolve_record_type(target_record_type)
        if canonical is None:
            logger.warning("Unknown record type '%s'.", target_record_type)
            canonical = "customer"

        ns_fields = NETSUITE_FIELDS.get(canonical, {})
        ns_field_ids = list(ns_fields.keys())
        ns_norm = {_normalize(fid): fid for fid in ns_field_ids}

        # Load source-system hints
        sys_key = source_system.lower().strip().replace(" ", "").replace("-", "")
        sys_hints = _SOURCE_SYSTEM_HINTS.get(sys_key, {})

        result: dict[str, dict[str, Any]] = {}

        for src_field in source_fields:
            norm_src = _normalize(src_field)

            # 1. Exact match against NetSuite field IDs
            if norm_src in ns_norm:
                ns_fid = ns_norm[norm_src]
                result[src_field] = {
                    "netsuite_field": ns_fid,
                    "confidence": 1.0,
                    "method": "exact",
                    "notes": ns_fields[ns_fid]["notes"],
                }
                continue

            # 2. Source-system hint lookup
            if norm_src in sys_hints:
                ns_fid = sys_hints[norm_src]
                if ns_fid in ns_fields:
                    result[src_field] = {
                        "netsuite_field": ns_fid,
                        "confidence": 0.95,
                        "method": "system_hint",
                        "notes": ns_fields[ns_fid]["notes"],
                    }
                    continue

            # 3. Fuzzy match
            best_fid: str | None = None
            best_score = 0.0
            for norm_ns, fid in ns_norm.items():
                score = _similarity(norm_src, norm_ns)
                if score > best_score:
                    best_score = score
                    best_fid = fid

            if best_fid and best_score >= 0.6:
                result[src_field] = {
                    "netsuite_field": best_fid,
                    "confidence": round(best_score, 3),
                    "method": "fuzzy",
                    "notes": ns_fields[best_fid]["notes"],
                }
            else:
                # 4. Common semantic aliases
                ns_fid = _semantic_alias(norm_src, canonical)
                if ns_fid and ns_fid in ns_fields:
                    result[src_field] = {
                        "netsuite_field": ns_fid,
                        "confidence": 0.7,
                        "method": "semantic_alias",
                        "notes": ns_fields[ns_fid]["notes"],
                    }
                else:
                    result[src_field] = {
                        "netsuite_field": None,
                        "confidence": 0.0,
                        "method": "unmapped",
                        "notes": "No matching NetSuite field found. Review manually.",
                    }

        return result

    def apply_mapping(
        self,
        df: pd.DataFrame,
        mapping: dict[str, str | None],
    ) -> pd.DataFrame:
        """
        Apply a field mapping dict to a DataFrame.

        mapping: {source_column: netsuite_field_id | None}
        Columns mapped to None are dropped.
        Returns a new DataFrame with NetSuite field names as columns.
        """
        rename_map: dict[str, str] = {}
        drop_cols: list[str] = []

        for src_col, ns_field in mapping.items():
            if src_col not in df.columns:
                continue
            if ns_field is None:
                drop_cols.append(src_col)
            else:
                rename_map[src_col] = ns_field

        result_df = df.copy()
        # Drop unmapped columns
        result_df = result_df.drop(columns=drop_cols, errors="ignore")
        # Rename mapped columns
        result_df = result_df.rename(columns=rename_map)
        return result_df

    def format_mapping_table(
        self, mapping_result: dict[str, dict[str, Any]]
    ) -> str:
        """Format a suggest_mapping result as a human-readable table."""
        lines = [
            f"{'Source Field':<35} {'NetSuite Field':<25} {'Confidence':<12} {'Method':<15}",
            "-" * 90,
        ]
        for src, info in mapping_result.items():
            ns = info.get("netsuite_field") or "(unmapped)"
            conf = f"{info.get('confidence', 0):.0%}"
            method = info.get("method", "")
            lines.append(f"{src:<35} {ns:<25} {conf:<12} {method:<15}")
        return "\n".join(lines)


def _semantic_alias(norm_src: str, record_type: str) -> str | None:
    """Map common field name patterns to NetSuite field IDs via semantic rules."""
    aliases: dict[str, str] = {
        # General
        "id": "externalid",
        "externalid": "externalid",
        "extid": "externalid",
        "sourceid": "externalid",
        "legacyid": "externalid",
        "name": "companyname" if record_type in ("customer", "vendor") else "itemid",
        "company": "companyname",
        "org": "companyname",
        "organization": "companyname",
        "fname": "firstname",
        "lname": "lastname",
        "givenname": "firstname",
        "surname": "lastname",
        "familyname": "lastname",
        "mail": "email",
        "emailaddress": "email",
        "tel": "phone",
        "telephone": "phone",
        "mobile": "mobilephone",
        "cell": "mobilephone",
        "cellphone": "mobilephone",
        "street": "addr1",
        "address": "addr1",
        "address1": "addr1",
        "address2": "addr2",
        "suite": "addr2",
        "postalcode": "zip",
        "postcode": "zip",
        "zipcode": "zip",
        "province": "state",
        "region": "state",
        "currencycode": "currency",
        "iso": "currency",
        "creditlimitamt": "creditlimit",
        "paymentterms": "terms",
        "net": "terms",
        "taxnumber": "taxid",
        "vat": "taxid",
        "vatnumber": "taxid",
        "ein": "taxid",
        "sku": "itemid",
        "partnumber": "itemid",
        "partno": "itemid",
        "partnum": "itemid",
        "uom": "stockunit",
        "unitofmeasure": "stockunit",
        "cost": "cost",
        "price": "salesprice",
        "unitprice": "salesprice",
        "msrp": "salesprice",
        "description": "salesdescription",
        "productdescription": "salesdescription",
        "invoicedate": "trandate",
        "orderdate": "trandate",
        "date": "trandate",
        "transactiondate": "trandate",
        "duedate": "duedate",
        "paymentdue": "duedate",
        "invoicenumber": "tranid",
        "invnum": "tranid",
        "ordernumber": "tranid",
        "ordernum": "tranid",
        "ponumber": "otherrefnum",
        "purchaseorder": "otherrefnum",
        "qty": "quantity",
        "qtyordered": "quantity",
        "dept": "department",
        "div": "department",
        "division": "department",
        "branch": "location",
        "warehouse": "location",
        "site": "location",
        "hiredate": "hiredate",
        "startdate": "hiredate",
        "terminationdate": "releasedate",
        "enddate": "releasedate",
        "supervisor": "supervisor",
        "manager": "supervisor",
        "reportsto": "supervisor",
        "jobtitle": "title",
        "position": "title",
        "emptype": "employeetype",
    }
    return aliases.get(norm_src)
