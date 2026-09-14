"""
Landing page (public) — Sign In / Sign Up / OTP verification.

Token storage: FastAPI sets the reconcile_session cookie (HttpOnly; SameSite=Lax)
directly in its response headers. The HTML forms on this page POST directly
to FastAPI, allowing the browser to receive and store the HttpOnly cookie natively.
"""
from __future__ import annotations

import os
import urllib.parse

import requests
import streamlit as st
import streamlit.components.v1 as components

from config import API_URL

st.markdown(
    '<div class="hero"><div class="eyebrow" role="doc-subtitle">Agentic bookkeeping • phase 01</div>'
    '<h1>Reconcile</h1>'
    '<p>Sign in to access your financial reconciliation control room.</p></div>',
    unsafe_allow_html=True,
)

# SEO meta injection (best-effort inside Streamlit iframe)
components.html(
    """<meta name="description" content="Reconcile — agentic bookkeeping control room. Ingests bank transactions, categorises with AI, posts to ledger.">
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebPage","name":"Reconcile","description":"Agentic bookkeeping control room"}
</script>""",
    height=0,
)

col1, col2, col3 = st.columns([1, 2, 1])

# Parse URL state
qp = st.query_params
error_msg = qp.get("error")
success_msg = qp.get("success")
show_otp = qp.get("show_otp") == "1"
verify_email = qp.get("verify_email", "")
url_mode = qp.get("mode", "signin")

form_styles = """
<style>
.html-form-container {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1rem;
}
.html-form-container label {
    display: block;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    font-weight: 500;
}
.html-form-container input {
    width: 100%;
    background: var(--white);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 0.65rem;
    margin-bottom: 1.2rem;
    font-family: inherit;
    box-sizing: border-box;
}
.html-form-container input:focus {
    outline: 2px solid var(--orange);
    outline-offset: 1px;
}
.html-form-container button {
    width: 100%;
    background: var(--ink);
    color: var(--white);
    border: none;
    border-radius: 4px;
    padding: 0.65rem 1rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
}
.html-form-container button:hover {
    background: #2f493b;
}
</style>
"""

with col2:
    if error_msg:
        st.error(error_msg)
    if success_msg:
        st.success(success_msg)

    st.markdown(form_styles, unsafe_allow_html=True)
    
    # Use standard Streamlit radio to toggle modes when not in OTP
    if not show_otp:
        # Default radio to what's in URL if user just submitted and failed
        idx = 1 if url_mode == "signup" else 0
        mode = st.radio("Access", ["Sign In", "Sign Up"], horizontal=True, label_visibility="collapsed", index=idx)
    else:
        mode = "OTP"

    if mode == "OTP":
        # ── OTP verification form ────────────────────────────────────────
        st.subheader("Verify your email")
        html_form = f"""
        <div class="html-form-container">
            <form action="{API_URL}/auth/form/verify-otp" method="POST">
                <label for="email">Email</label>
                <input type="email" id="email" name="email" value="{verify_email}" readonly style="background:#e8eee8;color:#46534c;cursor:not-allowed;">
                
                <label for="otp">6-digit OTP</label>
                <input type="text" id="otp" name="otp" required autocomplete="one-time-code" placeholder="Check server console for local dev">
                
                <button type="submit">Verify & Sign In</button>
            </form>
        </div>
        """
        st.markdown(html_form, unsafe_allow_html=True)
        
        if st.button("Cancel"):
            # Clear URL params
            st.query_params.clear()
            st.rerun()

    elif mode == "Sign In":
        # ── Normal sign-in form ──────────────────────────────────────────
        st.subheader("Account Login")
        html_form = f"""
        <div class="html-form-container">
            <form action="{API_URL}/auth/form/login" method="POST">
                <label for="login-email">Email address</label>
                <input type="email" id="login-email" name="email" placeholder="name@company.com" autocomplete="email" required>
                
                <label for="login-password">Password</label>
                <input type="password" id="login-password" name="password" autocomplete="current-password" required>
                
                <button type="submit">Sign In</button>
            </form>
        </div>
        """
        st.markdown(html_form, unsafe_allow_html=True)

        st.divider()

        # ── Google sign-in button ────────────────────────────────────────
        if st.button("Sign in with Google", use_container_width=True):
            try:
                resp = requests.get(f"{API_URL}/auth/google/login", timeout=5)
                if resp.status_code == 200:
                    auth_url = resp.json()["auth_url"]
                    st.markdown(
                        f'<meta http-equiv="refresh" content="0;url={auth_url}">',
                        unsafe_allow_html=True,
                    )
                else:
                    st.error(f"Google auth unavailable: {resp.json().get('detail', '')}")
            except Exception as exc:
                st.error(f"Error connecting to Google auth: {exc}")

    elif mode == "Sign Up":
        # ── Sign-up form ─────────────────────────────────────────────────────
        st.subheader("Create Account")
        html_form = f"""
        <div class="html-form-container">
            <form action="{API_URL}/auth/form/signup" method="POST">
                <label for="signup-email">Email address</label>
                <input type="email" id="signup-email" name="email" placeholder="name@company.com" autocomplete="email" required>
                
                <label for="signup-password">Password</label>
                <input type="password" id="signup-password" name="password" autocomplete="new-password" required>
                
                <label for="signup-confirm">Confirm Password</label>
                <input type="password" id="signup-confirm" name="confirm_password" autocomplete="new-password" required>
                
                <button type="submit">Sign Up</button>
            </form>
        </div>
        """
        st.markdown(html_form, unsafe_allow_html=True)
