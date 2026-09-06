#!/usr/bin/env python3
"""Extract NetSuite's REST metadata catalog into a zip for the NS-AI-Agent knowledge base.

Runs on YOUR machine with YOUR credentials; nothing is sent anywhere except to
your NetSuite account. Output is schema only (record and field definitions) —
no business data.

Setup (one time, in NetSuite):
  1. Setup > Company > Enable Features > SuiteCloud: enable
     "REST Web Services" and "Token-Based Authentication".
  2. Setup > Integration > Manage Integrations > New:
     name it (e.g. "NS-AI-Agent extract"), tick Token-Based Authentication,
     save, and copy the Consumer Key / Consumer Secret.
  3. Setup > Users/Roles > Access Tokens > New: pick the integration, your
     user, and a role with REST Web Services permission (Administrator is
     fine for a one-off extract). Copy the Token ID / Token Secret.

Usage:
  pip install requests requests-oauthlib
  export NS_ACCOUNT=1234567          # or 1234567_SB1 for a sandbox
  export NS_CONSUMER_KEY=...
  export NS_CONSUMER_SECRET=...
  export NS_TOKEN_ID=...
  export NS_TOKEN_SECRET=...
  python tools/netsuite_extract.py                 # full extract
  python tools/netsuite_extract.py --limit 5       # quick smoke test

Then upload the resulting zip in the app (Knowledge Base > NetSuite Catalog)
or POST it to /knowledge/catalog/import.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from datetime import datetime

try:
    import requests
    from requests_oauthlib import OAuth1
except ImportError:
    sys.exit("Missing dependencies. Run: pip install requests requests-oauthlib")


def account_host(account: str) -> str:
    # 1234567_SB1 -> 1234567-sb1.suitetalk.api.netsuite.com
    return f"{account.lower().replace('_', '-')}.suitetalk.api.netsuite.com"


def build_auth(account: str) -> OAuth1:
    return OAuth1(
        client_key=os.environ["NS_CONSUMER_KEY"],
        client_secret=os.environ["NS_CONSUMER_SECRET"],
        resource_owner_key=os.environ["NS_TOKEN_ID"],
        resource_owner_secret=os.environ["NS_TOKEN_SECRET"],
        realm=account.upper(),
        signature_method="HMAC-SHA256",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="Only fetch the first N record types (smoke test)")
    parser.add_argument("--out", default=None, help="Output zip path")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between requests")
    args = parser.parse_args()

    missing = [k for k in ("NS_ACCOUNT", "NS_CONSUMER_KEY", "NS_CONSUMER_SECRET", "NS_TOKEN_ID", "NS_TOKEN_SECRET") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")

    account = os.environ["NS_ACCOUNT"]
    base = f"https://{account_host(account)}/services/rest/record/v1/metadata-catalog"
    auth = build_auth(account)
    session = requests.Session()
    session.auth = auth

    print(f"Fetching catalog list from {base} ...")
    resp = session.get(base, headers={"Accept": "application/json"}, timeout=120)
    if resp.status_code != 200:
        sys.exit(f"Catalog list failed: HTTP {resp.status_code}\n{resp.text[:500]}")
    items = resp.json().get("items", [])
    names = [it.get("name") for it in items if it.get("name")]
    print(f"Found {len(names)} record types.")
    if args.limit:
        names = names[: args.limit]

    out_path = args.out or f"netsuite_metadata_{account.lower()}_{datetime.now():%Y%m%d}.zip"
    fetched: dict[str, dict] = {}
    queue = list(names)
    failures: list[str] = []

    def refs_in(obj) -> set[str]:
        found: set[str] = set()
        if isinstance(obj, dict):
            ref = obj.get("$ref")
            if isinstance(ref, str) and "metadata-catalog/" in ref:
                found.add(ref.rstrip("/").split("/")[-1].split("#")[0])
            for v in obj.values():
                found |= refs_in(v)
        elif isinstance(obj, list):
            for v in obj:
                found |= refs_in(v)
        return found

    while queue:
        name = queue.pop(0)
        if name in fetched:
            continue
        url = f"{base}/{name}"
        try:
            r = session.get(url, headers={"Accept": "application/schema+json"}, timeout=120)
            if r.status_code != 200:
                failures.append(f"{name}: HTTP {r.status_code}")
                continue
            schema = r.json()
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            continue
        fetched[name] = schema
        # Sublist collections/elements are separate catalog entries referenced by $ref.
        for ref in refs_in(schema):
            if ref not in fetched and ref not in queue and ref.lower() not in ("nsresource", "nslink", "nserror"):
                queue.append(ref)
        print(f"  [{len(fetched)}] {name}")
        time.sleep(args.delay)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, schema in fetched.items():
            zf.writestr(f"{name}.json", json.dumps(schema, indent=1))
        zf.writestr("_manifest.json", json.dumps({
            "account": account, "extracted_at": datetime.utcnow().isoformat() + "Z",
            "schemas": sorted(fetched), "failures": failures,
        }, indent=1))

    print(f"\nWrote {len(fetched)} schemas to {out_path}")
    if failures:
        print(f"{len(failures)} failures (listed in _manifest.json):")
        for f in failures[:20]:
            print("  " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
