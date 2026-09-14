from __future__ import annotations

import base64
import os
from typing import Any

import requests
from dotenv import load_dotenv

from app import db
from app.services.utils import retry_with_backoff

load_dotenv()


class QuickBooksClient:
    token_url = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    api_root = "https://sandbox-quickbooks.api.intuit.com/v3/company"

    def __init__(self) -> None:
        self.client_id = os.environ.get("QUICKBOOKS_CLIENT_ID", "")
        self.client_secret = os.environ.get("QUICKBOOKS_CLIENT_SECRET", "")
        self.realm_id = os.environ.get("QUICKBOOKS_REALM_ID", "")
        self.refresh_token = os.environ.get("QUICKBOOKS_REFRESH_TOKEN", "")
        self.access_token: str | None = None

    def refresh_access_token(self) -> str:
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

        response = requests.post(
            self.token_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )

        print(f"QUICKBOOKS token refresh HTTP {response.status_code}")
        print(response.text)

        response.raise_for_status()

        token_response = response.json()
        self.access_token = token_response["access_token"]
        return self.access_token

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Make a request with automatic retry on transient errors."""

        def _make_request():
            token = self.access_token or self.refresh_access_token()

            headers = kwargs.pop("headers", {})
            headers.update(
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                }
            )

            response = requests.request(
                method,
                f"{self.api_root}/{self.realm_id}{path}",
                headers=headers,
                timeout=30,
                **kwargs,
            )

            if response.status_code == 401:
                self.refresh_access_token()
                headers["Authorization"] = f"Bearer {self.access_token}"

                response = requests.request(
                    method,
                    f"{self.api_root}/{self.realm_id}{path}",
                    headers=headers,
                    timeout=30,
                    **kwargs,
                )

            print(f"QUICKBOOKS {method} {path} HTTP {response.status_code}")
            print(response.text)

            response.raise_for_status()
            return response

        return retry_with_backoff(_make_request, max_retries=2, backoff_base=1.0)

    def company_info(self) -> dict[str, Any]:
        return self.request(
            "GET",
            "/companyinfo/" + self.realm_id,
        ).json()

    def create_journal_entry(self, transaction: dict[str, Any]) -> dict[str, Any]:
        amount = abs(float(transaction["parsed_amount"]))

        payload = {
            "TxnDate": transaction["parsed_date"],
            "PrivateNote": f"Reconcile transaction {transaction['external_id']}",
            "Line": [
                {
                    "Description": transaction["parsed_vendor_raw"],
                    "Amount": amount,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit",
                        "AccountRef": {"value": "1"},
                    },
                },
                {
                    "Description": f"Offset for {transaction['parsed_vendor_raw']}",
                    "Amount": amount,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Credit",
                        "AccountRef": {"value": "1"},
                    },
                },
            ],
        }

        return self.request(
            "POST",
            "/journalentry",
            json=payload,
        ).json()


def sync_pending_transactions(
    transaction_ids: list[int] | None = None,
    user_id: int | None = None,
    adapter: str = "quickbooks_sandbox",
) -> dict[str, Any]:
    """
    Sync pending transactions to ledger.
    Scoped by user_id.
    """
    client = QuickBooksClient() if adapter != "local_demo" else None

    company_info = None
    if client:
        try:
            company_info = client.company_info()
        except Exception as exc:
            error_msg = str(exc)
            db.add_audit(
                None,
                "quickbooks_company_info_failed",
                {"error": error_msg, "exception_type": type(exc).__name__},
                user_id=user_id,
            )

    if transaction_ids:
        placeholders = ",".join("%s" for _ in transaction_ids)
        params: list[Any] = list(transaction_ids)
        if user_id is not None:
            query = f"SELECT * FROM transactions WHERE status = 'pending' AND user_id = %s AND id IN ({placeholders}) ORDER BY id"
            pending = db.rows(query, [user_id, *params])
        else:
            query = f"SELECT * FROM transactions WHERE status = 'pending' AND id IN ({placeholders}) ORDER BY id"
            pending = db.rows(query, params)
    else:
        if user_id is not None:
            pending = db.rows(
                "SELECT * FROM transactions WHERE status = 'pending' AND user_id = %s ORDER BY id",
                (user_id,),
            )
        else:
            pending = db.rows("SELECT * FROM transactions WHERE status = 'pending' ORDER BY id")

    posted = 0
    failed = 0

    for transaction in pending:
        uid = user_id or transaction.get("user_id") or 1
        try:
            if adapter == "local_demo":
                external_id = f"local-demo-ledger-{transaction['id']}"
            else:
                ledger_response = client.create_journal_entry(transaction)
                external_id = ledger_response.get("JournalEntry", {}).get("Id", "unknown")

            with db.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO ledger_entries
                    (user_id, transaction_id, external_ledger_id, amount, vendor, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (transaction_id) DO NOTHING
                    """,
                    (
                        uid,
                        transaction["id"],
                        external_id,
                        transaction["parsed_amount"],
                        transaction["parsed_vendor_raw"],
                        "posted",
                        db.now(),
                    ),
                )

                conn.execute(
                    "UPDATE transactions SET status = 'posted' WHERE id = %s",
                    (transaction["id"],),
                )

            db.add_audit(
                transaction["id"],
                "ledger_synced",
                {
                    "adapter": adapter,
                    "external_ledger_id": external_id,
                },
                user_id=uid,
            )

            posted += 1

        except Exception as exc:
            # Journal entry creation failed — mark transaction as flagged
            error_msg = str(exc)
            failed += 1

            with db.connection() as conn:
                conn.execute(
                    "UPDATE transactions SET status = 'flagged' WHERE id = %s",
                    (transaction["id"],),
                )

            db.add_audit(
                transaction["id"],
                "ledger_sync_failed",
                {
                    "adapter": adapter,
                    "error": error_msg,
                    "exception_type": type(exc).__name__,
                },
                user_id=uid,
            )

    return {
        "posted": posted,
        "failed": failed,
        "adapter": adapter,
        "company_info": company_info,
    }
