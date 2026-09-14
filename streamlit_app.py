"""
Reconcile — Streamlit entrypoint.

Session strategy
----------------
FastAPI sets a `reconcile_session` cookie with HttpOnly; SameSite=Lax;
Secure (in production).  JavaScript cannot read this cookie at all.
Streamlit's `st.context.cookies` exposes browser cookies to Python via
Streamlit's WebSocket protocol — not via JS — so the HttpOnly flag is
fully respected.  Streamlit's backend then forwards the token as a Bearer
header in server-side `requests` calls to FastAPI.

No token ever appears in the URL, in JS-readable storage, or in the JSON
response body.  The only JS-readable representation is the short-lived
Streamlit session_state which is in-process server memory, not the browser.
"""
from __future__ import annotations

import os

import requests
import streamlit as st
from jose import JWTError, jwt

from config import API_URL
COOKIE_NAME = "reconcile_session"
SECRET_KEY = os.getenv("SECRET_KEY", "reconcile-secret-key-for-local-development-32-chars-long")

st.set_page_config(
    page_title="Reconcile — Agentic Bookkeeping",
    page_icon="📒",
    layout="wide",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "Reconcile — agentic bookkeeping control room. Version 0.1.0.",
    },
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Space+Mono&display=swap');
    :root { --ink: #17211b; --muted: #46534c; --paper: #f7f8f5; --panel: #e8eee8; --line: #b8c5bb; --mint: #b9d9c2; --orange: #a94420; --white: #ffffff; }
    .stApp { background: var(--paper); color: var(--ink); }
    [data-testid='stAppViewContainer'], [data-testid='stHeader'] { background: var(--paper); }
    [data-testid='stMarkdownContainer'], [data-testid='stMarkdownContainer'] p, label, .stCaption, .stTextInput label { color: var(--ink); }
    h1, h2, h3 { font-family: 'DM Sans', sans-serif; letter-spacing: 0; }
    p, label, .stMarkdown { font-family: 'DM Sans', sans-serif; }
    .eyebrow { color: var(--orange); font-family: 'Space Mono', monospace; font-size: .75rem; letter-spacing: .12em; text-transform: uppercase; }
    .hero { border-bottom: 1px solid var(--line); padding: 1rem 0 1.75rem; margin-bottom: 1.5rem; }
    .hero h1 { font-size: 3.5rem; margin: .15rem 0 0; }
    .hero p { color: var(--muted); font-size: 1.05rem; max-width: 38rem; }
    [data-testid='stMetric'] { background: var(--panel); color: var(--ink); border: 1px solid var(--line); padding: 1rem; border-radius: 6px; }
    [data-testid='stMetricLabel'], [data-testid='stMetricValue'] { color: var(--ink); }
    .stButton > button, .stFormSubmitButton > button, button[data-testid^='stBaseButton'] { background: var(--ink) !important; color: var(--white) !important; border: 0; border-radius: 4px; padding: .65rem 1rem; }
    .stButton > button *, .stFormSubmitButton > button *, button[data-testid^='stBaseButton'] * { color: var(--white) !important; }
    .stButton > button:hover, .stFormSubmitButton > button:hover, button[data-testid^='stBaseButton']:hover { background: #2f493b !important; color: var(--white) !important; }
    .stButton > button:hover *, .stFormSubmitButton > button:hover *, button[data-testid^='stBaseButton']:hover * { color: var(--white) !important; }
    [data-testid='stDataFrame'], [data-testid='stTable'] { border: 1px solid var(--line); }
    [data-testid='stAlert'] { color: var(--ink); }
    input, textarea { background: var(--white); color: var(--ink); border-color: var(--line); }
    code { font-family: 'Space Mono', monospace; }
    .bulk-bar { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: .75rem 1rem; margin-bottom: 1rem; }
    .login-container { max-width: 26rem; margin: 2rem auto; padding: 2rem; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .user-badge { font-family: 'Space Mono', monospace; font-size: .8rem; color: var(--muted); }
    *:focus-visible { outline: 2px solid var(--orange) !important; outline-offset: 2px !important; }
    .app-footer { border-top: 1px solid var(--line); margin-top: 3rem; padding: 1.5rem 0 .5rem; font-size: .8rem; color: var(--muted); font-family: 'Space Mono', monospace; }
    .app-footer a { color: var(--muted); text-decoration: underline; }
    .app-footer a:hover { color: var(--ink); }
    
    /* Icon-rail sidebar */
    [data-testid="stSidebar"] {
        min-width: 5rem !important;
        max-width: 5rem !important;
        transition: min-width 0.2s ease, max-width 0.2s ease;
        overflow-x: hidden;
    }
    [data-testid="stSidebar"]:hover {
        min-width: 16rem !important;
        max-width: 16rem !important;
        z-index: 999999;
    }
    /* Hide text spans inside navigation until hovered */
    [data-testid="stSidebarNav"] ul li span:not(:first-child) {
        opacity: 0;
        transition: opacity 0.2s ease;
        white-space: nowrap;
    }
    [data-testid="stSidebar"]:hover [data-testid="stSidebarNav"] ul li span:not(:first-child) {
        opacity: 1;
    }
    /* Hide section headers in collapsed state */
    [data-testid="stSidebarNav"] ul li div {
        opacity: 0;
    }
    [data-testid="stSidebar"]:hover [data-testid="stSidebarNav"] ul li div {
        opacity: 1;
        transition: opacity 0.2s ease;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _token_from_cookie() -> str | None:
    """Read the HttpOnly session cookie via Streamlit's WebSocket cookie bridge.

    st.context.cookies gives Python server-side access to browser cookies.
    This is NOT JavaScript — HttpOnly is respected.
    """
    try:
        return st.context.cookies.get(COOKIE_NAME)
    except Exception:
        return None


def _decode_email(token: str) -> str | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload.get("email")
    except JWTError:
        return None


# ── Restore session from HttpOnly cookie on every page load ─────────────────
if os.getenv("REQUIRE_LOGIN", "false").lower() == "false":
    st.session_state["token"] = "dev_token"
    st.session_state["email"] = os.getenv("ADMIN_EMAIL", "admin@example.com").strip()
elif not st.session_state.get("token"):
    cookie_token = _token_from_cookie()
    if cookie_token:
        st.session_state["token"] = cookie_token
        st.session_state["email"] = _decode_email(cookie_token)


# ── Route to public or protected pages ──────────────────────────────────────
public_pages = {
    "Public": [
        st.Page("pages/landing.py", title="Sign In / Register", default=True, icon="🔐"),
        st.Page("pages/how_to_use.py", title="How to Use", icon="📖"),
    ]
}

protected_pages = {
    "Control Room": [
        st.Page("pages/control_room.py", title="Dashboard", default=True, icon="🎛️"),
    ],
    "Documentation": [
        st.Page("pages/how_to_use.py", title="How to Use", icon="📖"),
    ],
}

pg = st.navigation(protected_pages if st.session_state.get("token") else public_pages)
pg.run()
