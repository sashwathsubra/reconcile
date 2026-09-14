from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st

from config import API_URL
COOKIE_NAME = "reconcile_session"


def _get_session_token() -> str | None:
    """Read the session token from the HttpOnly cookie via Streamlit's cookie bridge.
    Falls back to session_state if already hydrated this run.
    """
    return st.session_state.get("token") or st.context.cookies.get(COOKIE_NAME)


def get_auth_headers() -> dict[str, str]:
    token = _get_session_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def api_request(method: str, path: str, **kwargs: Any) -> requests.Response | None:
    headers = kwargs.pop("headers", {})
    headers.update(get_auth_headers())
    try:
        response = requests.request(method, f"{API_URL}{path}", headers=headers, **kwargs)
        if response.status_code == 401:
            st.session_state.pop("token", None)
            st.session_state.pop("email", None)
            st.warning("Session expired. Please log in again.")
            st.rerun()
        return response
    except requests.RequestException as exc:
        st.error(f"Backend communication error: {exc}")
        return None


def get(path: str) -> list[dict[str, Any]]:
    response = api_request("GET", path, timeout=5)
    if response is not None and response.ok:
        return response.json()
    return []


user_col, logout_col = st.columns([4, 1])
with user_col:
    st.markdown(f"<div class='user-badge'>Logged in as: <strong>{st.session_state.get('email', 'User')}</strong></div>", unsafe_allow_html=True)
with logout_col:
    if st.button("Log out", key="logout-btn"):
        # Revoke the session server-side first (marks the token_hash as revoked in DB)
        try:
            requests.post(f"{API_URL}/auth/logout", headers=get_auth_headers(), timeout=5)
        except Exception:
            pass  # Best-effort; session_state clear below is always safe
        st.session_state.pop("token", None)
        st.session_state.pop("email", None)
        # The browser's HttpOnly cookie is cleared by the Set-Cookie: max-age=0
        # header returned by POST /auth/logout.  No JS needed.
        st.rerun()

# ── Reset demo data expander ────────────────────────────────────────────────
with st.expander("⚠️ Reset demo data", expanded=False):
    st.warning(
        "This clears every row from every demo table (transactions, runs, ledger entries, "
        "audit log, vendor rules, human corrections) and resets auto-increment counters for your company. "
        "It does **not** touch Plaid Sandbox or QuickBooks Sandbox — only this app's database records."
    )
    confirmed = st.checkbox(
        "I understand this clears all local demo data",
        key="reset-confirm-checkbox",
    )
    if st.button("Reset now", key="reset-now-btn", disabled=not confirmed):
        resp = api_request("POST", "/admin/reset", timeout=10)
        if resp is not None and resp.ok:
            data = resp.json()
            deleted = data.get("deleted", {})
            summary = ", ".join(f"{tbl}: {cnt} rows" for tbl, cnt in deleted.items())
            st.success(f"Reset complete — {summary}")
            st.rerun()
        elif resp is not None:
            st.error(f"Reset failed: {resp.text}")

# ── Toolbar ─────────────────────────────────────────────────────────────────
refresh_col, status_col = st.columns([1, 4])
with refresh_col:
    if st.button("Refresh data", key="refresh-data"):
        st.rerun()
with status_col:
    st.caption("Live view · refreshes automatically after every completed action")

if st.button("Run Plaid sandbox demo", type="primary"):
    with st.spinner("Running Plaid, Groq, and QuickBooks sandbox steps. This can take up to three minutes..."):
        response = api_request("POST", "/runs/demo", timeout=180)
    if response is not None and response.ok:
        data = response.json()
        st.success(f"Run {data.get('run_id')} completed and {data.get('ledger', {}).get('posted', 0)} entries synced.")
        st.rerun()
    elif response is not None:
        st.error(f"Run failed: {response.text}")

transactions = get("/transactions")
reviews = get("/reviews")
ledger_entries = get("/ledger")
audits = get("/audit")
audit_evidence = get("/audit-log")
runs = get("/runs")

posted = sum(item["status"] == "posted" for item in transactions)
col1, col2, col3, col4 = st.columns(4)
col1.metric("Runs", len(runs))
col2.metric("Transactions", len(transactions))
col3.metric("Posted", posted)
col4.metric("Audit events", len(audits))

# ── Human review queue ───────────────────────────────────────────────────────
st.subheader("Human review queue")

if reviews:
    # ── Select-all / deselect-all toggle ──────────────────────────────────
    sel_all_col, _ = st.columns([1, 5])
    with sel_all_col:
        if st.button("Select all", key="select-all-btn"):
            for review in reviews:
                st.session_state[f"bulk-select-{review['id']}"] = True
        if st.button("Deselect all", key="deselect-all-btn"):
            for review in reviews:
                st.session_state[f"bulk-select-{review['id']}"] = False

    # ── Per-row cards with checkboxes ──────────────────────────────────────
    for review in reviews:
        with st.container(border=True):
            check_col, content_col = st.columns([0.04, 0.96])
            with check_col:
                # Accessible label via help text — screen readers use this
                vendor_label = review.get('parsed_vendor_raw') or 'Unknown vendor'
                st.checkbox(
                    f"Select transaction #{review['id']}",
                    key=f"bulk-select-{review['id']}",
                    label_visibility="collapsed",
                    help=f"Select #{review['id']} — {vendor_label} for bulk action",
                )
            with content_col:
                st.markdown(
                    f"**#{review['id']} · {review.get('parsed_vendor_raw') or 'Unknown vendor'}**  "
                    f"${review.get('parsed_amount') or 0:,.2f} · {review.get('parsed_date') or 'No date'}"
                )
                st.caption(
                    f"Proposal: {review.get('proposed_vendor') or 'Unknown'} / "
                    f"{review.get('category') or 'Unknown'} · "
                    f"confidence {review.get('confidence_score') or 0:.2f} · "
                    f"status {review.get('status')}"
                )
                st.write(review.get("reasoning") or "No reasoning recorded.")
                if review.get("status") == "needs_info":
                    with st.form(f"info-{review['id']}"):
                        additional_info = st.text_input(
                            "Missing detail",
                            placeholder="Example: This is the team's monthly coffee subscription",
                            key=f"info-value-{review['id']}",
                        )
                        submitted_info = st.form_submit_button("Provide detail and retry")
                        if submitted_info:
                            resp = api_request(
                                "POST",
                                f"/corrections/{review['id']}",
                                json={"action": "provide_info", "additional_info": additional_info},
                                timeout=5,
                            )
                            if resp is not None and resp.ok:
                                st.success("Detail supplied; categorization retried.")
                                st.rerun()
                            elif resp is not None:
                                st.error(f"Retry failed: {resp.text}")
                approve_col, correct_col = st.columns([1, 2])
                with approve_col:
                    if st.button("Approve", key=f"approve-{review['id']}"):
                        resp = api_request(
                            "POST",
                            f"/corrections/{review['id']}",
                            json={"approve": True},
                            timeout=5,
                        )
                        if resp is not None and resp.ok:
                            st.success("Approved and marked matched.")
                            st.rerun()
                        elif resp is not None:
                            st.error(f"Approval failed: {resp.text}")
                with correct_col:
                    with st.form(f"correct-{review['id']}"):
                        corrected_vendor = st.text_input(
                            "Canonical vendor",
                            value=review.get("proposed_vendor") or "",
                            key=f"vendor-{review['id']}",
                        )
                        corrected_category = st.text_input(
                            "Category",
                            value=review.get("category") or "",
                            key=f"category-{review['id']}",
                        )
                        submitted = st.form_submit_button("Correct and learn")
                        if submitted:
                            resp = api_request(
                                "POST",
                                f"/corrections/{review['id']}",
                                json={
                                    "corrected_vendor": corrected_vendor,
                                    "corrected_category": corrected_category,
                                },
                                timeout=5,
                            )
                            if resp is not None and resp.ok:
                                st.success("Correction learned immediately.")
                                st.rerun()
                            elif resp is not None:
                                st.error(f"Correction failed: {resp.text}")

    # ── Bulk action bar — only visible when ≥1 row is checked ─────────────
    selected_ids = [r["id"] for r in reviews if st.session_state.get(f"bulk-select-{r['id']}")]
    if selected_ids:
        st.markdown('<div class="bulk-bar">', unsafe_allow_html=True)
        st.markdown(f"**{len(selected_ids)} transaction{'s' if len(selected_ids) != 1 else ''} selected**")
        bulk_approve_col, bulk_correct_col = st.columns([1, 2])

        with bulk_approve_col:
            if st.button(f"✓ Approve {len(selected_ids)} selected", key="bulk-approve-btn"):
                resp = api_request(
                    "POST",
                    "/corrections/bulk",
                    json={"transaction_ids": selected_ids, "approve": True},
                    timeout=10,
                )
                if resp is not None and resp.ok:
                    data = resp.json()
                    ok = len(data.get("successes", []))
                    fail = len(data.get("failures", []))
                    msg = f"Bulk approved {ok} transaction{'s' if ok != 1 else ''}."
                    if fail:
                        msg += f" {fail} failed — check IDs: {[f['transaction_id'] for f in data['failures']]}"
                    st.success(msg)
                    for rid in selected_ids:
                        st.session_state[f"bulk-select-{rid}"] = False
                    st.rerun()
                elif resp is not None:
                    st.error(f"Bulk approval failed: {resp.text}")

        with bulk_correct_col:
            with st.form("bulk-correct-form"):
                bulk_vendor = st.text_input(
                    "Canonical vendor (applied to all selected)",
                    key="bulk-vendor-input",
                    placeholder="e.g. Starbucks",
                )
                bulk_category = st.text_input(
                    "Category (applied to all selected)",
                    key="bulk-category-input",
                    placeholder="e.g. Meals & Entertainment",
                )
                bulk_submitted = st.form_submit_button(f"Correct {len(selected_ids)} selected and learn")
                if bulk_submitted:
                    if not bulk_vendor or not bulk_category:
                        st.error("Both vendor and category are required for bulk correction.")
                    else:
                        resp = api_request(
                            "POST",
                            "/corrections/bulk",
                            json={
                                "transaction_ids": selected_ids,
                                "corrected_vendor": bulk_vendor,
                                "corrected_category": bulk_category,
                            },
                            timeout=10,
                        )
                        if resp is not None and resp.ok:
                            data = resp.json()
                            ok = len(data.get("successes", []))
                            fail = len(data.get("failures", []))
                            msg = f"Bulk corrected {ok} transaction{'s' if ok != 1 else ''}."
                            if fail:
                                msg += f" {fail} failed — check IDs: {[f['transaction_id'] for f in data['failures']]}"
                            st.success(msg)
                            for rid in selected_ids:
                                st.session_state[f"bulk-select-{rid}"] = False
                            st.rerun()
                        elif resp is not None:
                            st.error(f"Bulk correction failed: {resp.text}")

        st.markdown("</div>", unsafe_allow_html=True)

else:
    st.info("No flagged transactions need review.")

# ── Data panels ──────────────────────────────────────────────────────────────
left, right = st.columns([1.25, 1])
with left:
    st.subheader("Bank intake")
    if transactions:
        st.dataframe(
            [
                {
                    "vendor": item["parsed_vendor_raw"],
                    "amount": f"${item['parsed_amount']:,.2f}",
                    "date": item["parsed_date"],
                    "status": item["status"],
                    "source id": item["external_id"],
                }
                for item in transactions
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No transactions yet. Trigger the demo run to begin the flow.")

with right:
    st.subheader("Ledger sync")
    if ledger_entries:
        st.dataframe(
            [
                {
                    "ledger id": item["external_ledger_id"],
                    "vendor": item["vendor"],
                    "amount": f"${item['amount']:,.2f}",
                    "status": item["status"],
                }
                for item in ledger_entries
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No ledger entries yet.")

st.subheader("Audit trail")
if audits:
    st.dataframe(
        [
            {
                "event": item["event_type"],
                "actor": item["actor"],
                "detail": item["detail_text"],
                "at": item["created_at"],
            }
            for item in audits[:20]
        ],
        width="stretch",
        hide_index=True,
    )
else:
    st.info("Audit events will appear here after the first run.")

st.subheader("Agent decision evidence")
if audit_evidence:
    st.dataframe(
        [
            {
                "type": item["record_type"],
                "transaction": item["transaction_id"],
                "decision / event": item["decision"],
                "reasoning / detail": item["reasoning"],
                "actor": item["actor"] or "agent",
                "timestamp": item["timestamp"],
            }
            for item in audit_evidence[:50]
        ],
        width="stretch",
        hide_index=True,
    )
else:
    st.info("Decision evidence will appear after the first categorization.")

# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown(
    """<div class="app-footer" role="contentinfo">
    <p>
        <strong>Reconcile</strong> &mdash; Agentic bookkeeping &bull; Version 0.1.0<br>
        Operated by <strong>[OPERATOR NAME]</strong>
        &nbsp;&middot;&nbsp;
        <a href="https://BASE_DOMAIN.example.com/docs/privacy-policy" target="_blank" rel="noopener noreferrer">Privacy Policy</a>
        &nbsp;&middot;&nbsp;
        <a href="https://BASE_DOMAIN.example.com/docs/terms-of-service" target="_blank" rel="noopener noreferrer">Terms of Service</a>
        &nbsp;&middot;&nbsp;
        <a href="https://BASE_DOMAIN.example.com/docs/cookie-policy" target="_blank" rel="noopener noreferrer">Cookie Policy</a>
    </p>
    <p style="font-size:.7rem;">
        Financial data is processed in accordance with India's Digital Personal Data Protection Act, 2023 (DPDP Act).
        Legal documents are drafts pending review &mdash; not yet in effect.
        &nbsp;|&nbsp;
        Google Fonts (DM Sans, Space Mono) are loaded from Google servers; see Cookie Policy.
    </p>
    </div>""",
    unsafe_allow_html=True,
)
