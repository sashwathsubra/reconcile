from __future__ import annotations

import os
import time
import random
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.auth import (
    COOKIE_NAME,
    create_access_token,
    create_session,
    get_current_user,
    hash_password,
    make_session_cookie_kwargs,
    revoke_session,
    verify_password,
    GOOGLE_REDIRECT_URI,
)
from app.services import corrections, ingestion
from app.services.workflow import build_graph

STATIC_DIR = Path(__file__).parent / "static"


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    email: str
    password: str
    confirm_password: str


class VerifyOTPRequest(BaseModel):
    email: str
    otp: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CorrectionRequest(BaseModel):
    action: str | None = None
    corrected_vendor: str | None = None
    corrected_category: str | None = None
    additional_info: str | None = None
    approve: bool = False


class BulkCorrectionRequest(BaseModel):
    transaction_ids: list[int]
    approve: bool = False
    corrected_vendor: str | None = None
    corrected_category: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.startup_checks()
    db.init_db()
    yield


app = FastAPI(
    title="Reconcile",
    version="0.1.0",
    description="Agentic bookkeeping control room — ingests bank transactions, categorises with AI, posts to ledger.",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Serve static files (robots.txt, sitemap.xml, llms.txt, favicon.svg)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Convenience redirects for well-known paths ───────────────────────────────
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    """Serve robots.txt directly at root path for crawlers."""
    from fastapi.responses import FileResponse
    robots_path = STATIC_DIR / "robots.txt"
    if robots_path.exists():
        return FileResponse(str(robots_path), media_type="text/plain")
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    from fastapi.responses import FileResponse
    sitemap_path = STATIC_DIR / "sitemap.xml"
    if sitemap_path.exists():
        return FileResponse(str(sitemap_path), media_type="application/xml")
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request):
    from fastapi.responses import FileResponse
    llms_path = STATIC_DIR / "llms.txt"
    if llms_path.exists():
        return FileResponse(str(llms_path), media_type="text/plain")
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.get("/.well-known/structured-data", include_in_schema=False)
async def structured_data():
    """JSON-LD structured data describing this service (Person schema, individual operator)."""
    return JSONResponse({
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "Reconcile",
        "description": "Agentic bookkeeping control room that ingests bank transactions, categorises them using AI, and posts approved entries to a ledger.",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "creator": {
            "@type": "Person",
            "name": "[OPERATOR NAME — fill in before publication]",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "IN",
                "streetAddress": "[BUSINESS ADDRESS — fill in before publication]"
            }
        },
        "offers": {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "INR"
        }
    })


@app.get("/health", tags=["System"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={
            "detail": "Not found",
            "path": str(request.url.path),
            "hint": "Check /docs for available API endpoints.",
        },
    )


_RATE_LIMITS: dict[str, list[float]] = {}

def check_rate_limit(request: Request, max_requests: int = 5, window_seconds: int = 60) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    if client_ip not in _RATE_LIMITS:
        _RATE_LIMITS[client_ip] = []
    _RATE_LIMITS[client_ip] = [t for t in _RATE_LIMITS[client_ip] if now - t < window_seconds]
    if len(_RATE_LIMITS[client_ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    _RATE_LIMITS[client_ip].append(now)


@app.post("/auth/login")
def login(request: LoginRequest, req: Request, response: Response) -> dict[str, str]:
    check_rate_limit(req, max_requests=10)
    is_local = os.getenv("APP_ENV", "local") == "local"
    user = db.one("SELECT id, email, hashed_password, is_active FROM users WHERE email = %s", (request.email.strip(),))
    if not user or not user["hashed_password"] or not verify_password(request.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is pending verification. Please verify your OTP.",
        )
    token = create_access_token(
        data={"sub": str(user["id"]), "user_id": user["id"], "email": user["email"]}
    )
    create_session(user["id"], token)
    response.set_cookie(value=token, **make_session_cookie_kwargs(is_local))
    return {"email": user["email"]}


@app.post("/auth/signup")
def signup(request: SignupRequest, req: Request) -> dict[str, str]:
    check_rate_limit(req)
    if request.password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    email = request.email.strip()
    existing = db.one("SELECT id, is_active FROM users WHERE email = %s", (email,))
    
    otp = "".join(random.choices("0123456789", k=6))
    otp_hash = hash_password(otp)
    expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    hashed = hash_password(request.password)
    
    if existing:
        if existing.get("is_active"):
            raise HTTPException(status_code=400, detail="Email already registered")
        else:
            # Resend OTP for inactive account and update password
            with db.connection() as conn:
                conn.execute(
                    "UPDATE users SET hashed_password = %s, otp_hash = %s, otp_expires_at = %s WHERE id = %s",
                    (hashed, otp_hash, expires, existing["id"])
                )
    else:
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO users (email, hashed_password, is_active, otp_hash, otp_expires_at, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (email, hashed, False, otp_hash, expires, db.now())
            )
            
    # Mocking email delivery
    print(f"\n{'='*40}\nOTP for {email}: {otp}\n{'='*40}\n")
    return {"detail": "OTP sent to your email. Please verify to activate your account."}


@app.post("/auth/verify-otp")
def verify_otp(request: VerifyOTPRequest, req: Request, response: Response) -> dict[str, str]:
    check_rate_limit(req)
    is_local = os.getenv("APP_ENV", "local") == "local"
    email = request.email.strip()
    user = db.one("SELECT id, email, otp_hash, otp_expires_at FROM users WHERE email = %s AND is_active = False", (email,))
    
    if not user:
        raise HTTPException(status_code=400, detail="User not found or already verified.")
        
    if not user["otp_expires_at"] or datetime.fromisoformat(user["otp_expires_at"]) < datetime.now(UTC):
        raise HTTPException(status_code=400, detail="OTP expired. Please sign up again to receive a new code.")
        
    if not user["otp_hash"] or not verify_password(request.otp.strip(), user["otp_hash"]):
        raise HTTPException(status_code=400, detail="Invalid OTP.")
        
    with db.connection() as conn:
        conn.execute("UPDATE users SET is_active = True, otp_hash = NULL, otp_expires_at = NULL WHERE id = %s", (user["id"],))
        
    token = create_access_token(data={"sub": str(user["id"]), "user_id": user["id"], "email": user["email"]})
    create_session(user["id"], token)
    response.set_cookie(value=token, **make_session_cookie_kwargs(is_local))
    return {"email": user["email"]}


# ── Form-based endpoints for Streamlit HTML forms ───────────────────────────

def _redirect(url: str, **query_params: str) -> RedirectResponse:
    if query_params:
        url = f"{url}?{urllib.parse.urlencode(query_params)}"
    return RedirectResponse(url=url, status_code=303)

@app.post("/auth/form/login")
def form_login(
    req: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    try:
        check_rate_limit(req, max_requests=10)
    except HTTPException:
        return _redirect("http://localhost:8501/", error="Too many requests. Please try again later.")
        
    is_local = os.getenv("APP_ENV", "local") == "local"
    user = db.one("SELECT id, email, hashed_password, is_active FROM users WHERE email = %s", (email.strip(),))
    
    if not user or not user["hashed_password"] or not verify_password(password, user["hashed_password"]):
        return _redirect("http://localhost:8501/", error="Incorrect email or password.")
        
    if not user.get("is_active"):
        return _redirect("http://localhost:8501/", error="Account is pending verification.", verify_email=email.strip(), show_otp="1")
        
    token = create_access_token(
        data={"sub": str(user["id"]), "user_id": user["id"], "email": user["email"]}
    )
    create_session(user["id"], token)
    
    redirect = _redirect("http://localhost:8501/")
    redirect.set_cookie(value=token, **make_session_cookie_kwargs(is_local))
    return redirect

@app.post("/auth/form/signup")
def form_signup(
    req: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    try:
        check_rate_limit(req)
    except HTTPException:
        return _redirect("http://localhost:8501/", mode="signup", error="Too many requests.")
        
    if password != confirm_password:
        return _redirect("http://localhost:8501/", mode="signup", error="Passwords do not match.")
        
    email_clean = email.strip()
    existing = db.one("SELECT id, is_active FROM users WHERE email = %s", (email_clean,))
    
    otp = "".join(random.choices("0123456789", k=6))
    otp_hash = hash_password(otp)
    expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    hashed = hash_password(password)
    
    if existing:
        if existing.get("is_active"):
            return _redirect("http://localhost:8501/", mode="signup", error="Email already registered.")
        else:
            with db.connection() as conn:
                conn.execute(
                    "UPDATE users SET hashed_password = %s, otp_hash = %s, otp_expires_at = %s WHERE id = %s",
                    (hashed, otp_hash, expires, existing["id"])
                )
    else:
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO users (email, hashed_password, is_active, otp_hash, otp_expires_at, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (email_clean, hashed, False, otp_hash, expires, db.now())
            )
            
    print(f"\n{'='*40}\nOTP for {email_clean}: {otp}\n{'='*40}\n")
    return _redirect("http://localhost:8501/", success="Check the server console for your OTP.", verify_email=email_clean, show_otp="1")

@app.post("/auth/form/verify-otp")
def form_verify_otp(
    req: Request,
    email: str = Form(...),
    otp: str = Form(...),
) -> RedirectResponse:
    try:
        check_rate_limit(req)
    except HTTPException:
        return _redirect("http://localhost:8501/", error="Too many requests.", verify_email=email, show_otp="1")
        
    is_local = os.getenv("APP_ENV", "local") == "local"
    email_clean = email.strip()
    user = db.one("SELECT id, email, otp_hash, otp_expires_at FROM users WHERE email = %s AND is_active = False", (email_clean,))
    
    if not user:
        return _redirect("http://localhost:8501/", error="User not found or already verified.", verify_email=email_clean, show_otp="1")
        
    if not user["otp_expires_at"] or datetime.fromisoformat(user["otp_expires_at"]) < datetime.now(UTC):
        return _redirect("http://localhost:8501/", error="OTP expired. Please sign up again.")
        
    if not user["otp_hash"] or not verify_password(otp.strip(), user["otp_hash"]):
        return _redirect("http://localhost:8501/", error="Invalid OTP.", verify_email=email_clean, show_otp="1")
        
    with db.connection() as conn:
        conn.execute("UPDATE users SET is_active = True, otp_hash = NULL, otp_expires_at = NULL WHERE id = %s", (user["id"],))
        
    token = create_access_token(data={"sub": str(user["id"]), "user_id": user["id"], "email": user["email"]})
    create_session(user["id"], token)
    
    redirect = _redirect("http://localhost:8501/")
    redirect.set_cookie(value=token, **make_session_cookie_kwargs(is_local))
    return redirect




@app.get("/auth/google/login")
def google_login(req: Request):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="Google Client ID not configured.")
    
    redirect_uri = GOOGLE_REDIRECT_URI
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={client_id}&response_type=code&scope=openid%20email%20profile&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}&access_type=offline"
    )
    return {"auth_url": auth_url}


@app.get("/auth/google/callback")
async def google_callback(code: str):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = GOOGLE_REDIRECT_URI
    
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Google Auth not configured.")
        
    token_url = "https://oauth2.googleapis.com/token"
    async with httpx.AsyncClient() as client:
        token_res = await client.post(token_url, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        })
        
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to exchange Google token")
            
        access_token = token_res.json().get("access_token")
        
        profile_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if profile_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch Google profile")
            
        profile = profile_res.json()
        
    email = profile.get("email")
    google_id = profile.get("id")
    
    if not email or not google_id:
        raise HTTPException(status_code=400, detail="Google profile missing email or ID")
        
    user = db.one("SELECT id, email, google_id FROM users WHERE email = %s", (email,))
    
    if user:
        if not user.get("google_id"):
            # Link existing manual account to Google
            with db.connection() as conn:
                conn.execute("UPDATE users SET google_id = %s, is_active = True WHERE id = %s", (google_id, user["id"]))
        user_id = user["id"]
    else:
        # Create new user
        with db.connection() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, google_id, is_active, created_at) VALUES (%s, %s, True, %s) RETURNING id",
                (email, google_id, db.now())
            )
            row = cur.fetchone()
            user_id = row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            
    token = create_access_token(data={"sub": str(user_id), "user_id": user_id, "email": email})
    create_session(user_id, token)
    is_local = os.getenv("APP_ENV", "local") == "local"
    # Redirect back to Streamlit — cookie is set on the FastAPI origin (localhost:8000).
    # Streamlit reads it via st.context.cookies which bridges the browser cookie store
    # to Streamlit's Python backend over its WebSocket protocol (not JS).
    redirect = RedirectResponse(url="http://localhost:8501/")
    redirect.set_cookie(value=token, **make_session_cookie_kwargs(is_local))
    return redirect


@app.post("/auth/logout")
def logout(
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Revoke the session server-side and clear the HttpOnly cookie."""
    token = current_user.get("_token", "")
    if token:
        revoke_session(token)
    # Delete the cookie by setting max-age=0
    response.set_cookie(
        key=COOKIE_NAME,
        value="",
        httponly=True,
        samesite="lax",
        secure=os.getenv("APP_ENV", "local") != "local",
        max_age=0,
        path="/",
    )
    return {"detail": "Logged out successfully"}


@app.post("/runs/demo")
def trigger_demo_run(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = current_user["id"]
    run = ingestion.create_run(user_id=uid)
    transaction_ids: list[int] = []
    workflow_results: list[dict[str, Any]] = []

    if run.get("run_id"):
        transactions = db.rows(
            "SELECT * FROM transactions WHERE run_id = %s AND user_id = %s AND status = 'pending' ORDER BY id",
            (run["run_id"], uid),
        )
        graph = build_graph()
        for transaction in transactions:
            transaction_ids.append(transaction["id"])
            workflow_results.append(graph.invoke({"transaction": transaction, "memory": []}))

    ledger_results = [result.get("ledger_result", {}) for result in workflow_results]
    sync = {
        "posted": sum(result.get("posted", 0) for result in ledger_results),
        "failed": sum(result.get("failed", 0) for result in ledger_results),
        "adapter": "quickbooks_sandbox",
    }
    db.add_audit(
        None,
        "run_completed",
        {
            "run_id": run["run_id"],
            "ingestion": run,
            "ledger": sync,
            "transaction_ids": transaction_ids,
            "actions": [result.get("decision", {}).get("action") for result in workflow_results],
        },
        user_id=uid,
    )
    return {**run, "ledger": sync}


@app.post("/runs/synthetic_demo")
def trigger_synthetic_demo_run(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = current_user["id"]
    source = "synthetic_demo"
    
    # 1. Create a run record
    with db.connection() as conn:
        cursor = conn.execute(
            "INSERT INTO runs(user_id, source, status, transaction_count, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (uid, source, "ingesting", 8, db.now()),
        )
        run_row = cursor.fetchone()
        run_id = run_row["id"] if isinstance(run_row, dict) or hasattr(run_row, "keys") else run_row[0]

    # 2. Insert synthetic transactions
    # 2-3 clear vendors, 1 ambiguous, 1 unrecognizable, 1 near duplicate
    import json
    import uuid
    synthetic_txs = [
        {"amount": 14.50, "date": "2026-09-01", "merchant_name": "STARBUCKS STORE #12345"}, # Clear
        {"amount": 120.00, "date": "2026-09-02", "merchant_name": "AMAZON WEB SERVICES AWS.AMAZON.CO"}, # Clear
        {"amount": -4500.00, "date": "2026-09-03", "merchant_name": "GUSTO PAYROLL"}, # Clear
        {"amount": 45.00, "date": "2026-09-04", "merchant_name": "SQ *LOCAL CAFE"}, # Ambiguous (needs info for middle confidence, maybe wait, Groq might be very confident here. Let's make it borderline)
        {"amount": 89.99, "date": "2026-09-05", "merchant_name": "FIVERR INC."}, # Ambiguous
        {"amount": 250.00, "date": "2026-09-06", "merchant_name": "TXN*892348-ABC"}, # Unrecognizable
        {"amount": 14.50, "date": "2026-09-01", "merchant_name": "STARBUCKS STORE #12345"}, # Duplicate of #1
        {"amount": 12.00, "date": "2026-09-07", "merchant_name": "UBER EATS"}, # Clear
    ]
    
    inserted = 0
    with db.connection() as conn:
        for tx in synthetic_txs:
            external_id = f"synth-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO transactions
                (user_id, run_id, source, external_id, raw_payload, parsed_amount, parsed_date, parsed_vendor_raw, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                """,
                (
                    uid, run_id, source, external_id, json.dumps(tx),
                    tx["amount"], tx["date"], tx["merchant_name"], db.now()
                )
            )
            inserted += 1
            
        conn.execute("UPDATE runs SET status = 'completed', transaction_count = %s WHERE id = %s", (inserted, run_id))

    # 3. Run the workflow
    transaction_ids: list[int] = []
    workflow_results: list[dict[str, Any]] = []

    transactions = db.rows(
        "SELECT * FROM transactions WHERE run_id = %s AND user_id = %s AND status = 'pending' ORDER BY id",
        (run_id, uid),
    )
    graph = build_graph()
    for transaction in transactions:
        transaction_ids.append(transaction["id"])
        workflow_results.append(graph.invoke({"transaction": transaction, "memory": []}))

    ledger_results = [result.get("ledger_result", {}) for result in workflow_results]
    sync = {
        "posted": sum(result.get("posted", 0) for result in ledger_results),
        "failed": sum(result.get("failed", 0) for result in ledger_results),
        "adapter": "local_demo",
    }
    
    db.add_audit(
        None,
        "run_completed",
        {
            "run_id": run_id,
            "ingestion": {"run_id": run_id, "inserted": inserted, "source": source},
            "ledger": sync,
            "transaction_ids": transaction_ids,
            "actions": [result.get("decision", {}).get("action") for result in workflow_results],
        },
        user_id=uid,
    )
    
    return {
        "run_id": run_id,
        "source": source,
        "inserted": inserted,
        "duplicates": 0,
        "parse_errors": 0,
        "status": "completed",
        "ledger": sync
    }


@app.post("/runs/upload_csv")
async def upload_csv(
    file: UploadFile = File(...),
    current_user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, Any]:
    import csv
    import io
    import hashlib
    import uuid

    uid = current_user["id"]
    source = "csv_upload"
    
    content = await file.read()
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    
    transactions = []
    for row in reader:
        # Normalize keys
        row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
        amount_raw = row.get("amount")
        date = row.get("date")
        vendor = row.get("merchant_name") or row.get("vendor") or row.get("name")
        tx_id = row.get("transaction_id")
        
        try:
            amount = float(amount_raw) if amount_raw else None
        except ValueError:
            amount = None # Will trigger validation error in the loop
            
        if not tx_id and amount is not None and date and vendor:
            h = hashlib.sha256(f"{date}|{vendor}|{amount}".encode()).hexdigest()
            tx_id = f"csv-{h[:12]}"
        elif not tx_id:
            tx_id = f"csv-unk-{uuid.uuid4().hex[:8]}"
            
        transactions.append({
            "transaction_id": tx_id,
            "amount": amount,
            "date": date,
            "merchant_name": vendor,
            "raw_csv_row": row
        })

    with db.connection() as conn:
        cursor = conn.execute(
            "INSERT INTO runs(user_id, source, status, transaction_count, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (uid, source, "ingesting", len(transactions), db.now()),
        )
        run_row = cursor.fetchone()
        run_id = run_row["id"] if isinstance(run_row, dict) or hasattr(run_row, "keys") else run_row[0]

        inserted = 0
        duplicates = 0
        parse_errors = 0
        audit_events: list[tuple[int | None, str, dict[str, Any]]] = []

        for transaction in transactions:
            external_id = transaction.get("transaction_id", "unknown")
            try:
                parsed_amount = transaction.get("amount")
                parsed_date = transaction.get("date")
                parsed_vendor = transaction.get("merchant_name")

                if parsed_amount is None or not parsed_date or not parsed_vendor:
                    raise ValueError(
                        f"Missing or invalid fields: amount={parsed_amount}, date={parsed_date}, vendor={parsed_vendor}"
                    )

                existing = conn.execute(
                    "SELECT id FROM transactions WHERE user_id = %s AND external_id = %s",
                    (uid, external_id)
                ).fetchone()
                
                if existing:
                    duplicates += 1
                    audit_events.append((None, "duplicate_skipped", {"external_id": external_id}))
                    continue

                tx_cur = conn.execute(
                    """
                    INSERT INTO transactions
                    (user_id, run_id, source, external_id, raw_payload, parsed_amount, parsed_date, parsed_vendor_raw, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                    RETURNING id
                    """,
                    (
                        uid, run_id, source, external_id,
                        json.dumps(transaction),
                        parsed_amount, parsed_date, parsed_vendor, db.now(),
                    ),
                )
                tx_row = tx_cur.fetchone()
                tx_id = tx_row["id"] if isinstance(tx_row, dict) or hasattr(tx_row, "keys") else tx_row[0]
                audit_events.append((tx_id, "transaction_ingested", {"source": source, "external_id": external_id}))
                inserted += 1
            except Exception as parse_exc:
                parse_errors += 1
                error_detail = str(parse_exc)
                try:
                    err_cur = conn.execute(
                        """
                        INSERT INTO transactions (user_id, run_id, source, external_id, raw_payload, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, 'flagged', %s)
                        RETURNING id
                        """,
                        (uid, run_id, source, external_id, json.dumps(transaction), db.now()),
                    )
                    err_row = err_cur.fetchone()
                    err_id = err_row["id"] if isinstance(err_row, dict) or hasattr(err_row, "keys") else err_row[0]
                    audit_events.append(
                        (err_id, "parse_error", {"error": error_detail, "exception_type": type(parse_exc).__name__})
                    )
                except Exception:
                    audit_events.append((None, "parse_error_unrecoverable", {"external_id": external_id, "error": error_detail}))

        status_str = "completed"
        if duplicates > 0: status_str = "completed_with_duplicates"
        if parse_errors > 0: status_str = "completed_with_parse_errors"
        if parse_errors > 0 and duplicates > 0: status_str = "completed_with_duplicates_and_parse_errors"

        conn.execute("UPDATE runs SET status = %s, transaction_count = %s WHERE id = %s", (status_str, inserted, run_id))

    for transaction_id, event_type, detail in audit_events:
        db.add_audit(transaction_id, event_type, detail, user_id=uid)
        
    db.add_audit(
        None, "csv_upload_completed",
        {
            "run_id": run_id,
            "added": len(transactions),
            "inserted": inserted,
            "duplicates": duplicates,
            "parse_errors": parse_errors
        },
        user_id=uid,
    )

    # 3. Run the workflow
    transaction_ids: list[int] = []
    workflow_results: list[dict[str, Any]] = []

    pending_txs = db.rows(
        "SELECT * FROM transactions WHERE run_id = %s AND user_id = %s AND status = 'pending' ORDER BY id",
        (run_id, uid),
    )
    graph = build_graph()
    for tx in pending_txs:
        transaction_ids.append(tx["id"])
        workflow_results.append(graph.invoke({"transaction": tx, "memory": []}))

    ledger_results = [res.get("ledger_result", {}) for res in workflow_results]
    sync = {
        "posted": sum(res.get("posted", 0) for res in ledger_results),
        "failed": sum(res.get("failed", 0) for res in ledger_results),
        "adapter": "local_demo",
    }
    
    db.add_audit(
        None, "run_completed",
        {
            "run_id": run_id,
            "ingestion": {"run_id": run_id, "inserted": inserted, "source": source},
            "ledger": sync,
            "transaction_ids": transaction_ids,
            "actions": [res.get("decision", {}).get("action") for res in workflow_results],
        },
        user_id=uid,
    )
    
    return {
        "run_id": run_id,
        "source": source,
        "inserted": inserted,
        "duplicates": duplicates,
        "parse_errors": parse_errors,
        "status": status_str,
        "ledger": sync
    }


@app.get("/runs")
def list_runs(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM runs WHERE user_id = %s ORDER BY id DESC", (current_user["id"],))


@app.get("/transactions")
def list_transactions(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM transactions WHERE user_id = %s ORDER BY id DESC", (current_user["id"],))


@app.get("/reviews")
def list_reviews(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    uid = current_user["id"]
    return db.rows(
        """
        SELECT t.id, t.parsed_amount, t.parsed_date, t.parsed_vendor_raw, t.status,
               md.proposed_vendor, md.category, md.confidence_score, md.reasoning
        FROM transactions t
        LEFT JOIN match_decisions md ON md.id = (
            SELECT MAX(id) FROM match_decisions WHERE transaction_id = t.id AND user_id = %s
        )
        WHERE t.user_id = %s AND t.status IN ('flagged', 'needs_info')
        ORDER BY t.id DESC
        """,
        (uid, uid),
    )


@app.post("/corrections/{transaction_id}")
def review_transaction(
    transaction_id: int,
    request: CorrectionRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    uid = current_user["id"]
    try:
        if request.action == "provide_info":
            return corrections.provide_transaction_info(
                transaction_id, request.additional_info or "", user_id=uid
            )
        if request.approve:
            if request.corrected_vendor or request.corrected_category:
                raise ValueError("approve cannot be combined with correction fields")
            return corrections.approve_transaction(transaction_id, user_id=uid)
        if not request.corrected_vendor or not request.corrected_category:
            raise ValueError("corrected_vendor and corrected_category are required")
        return corrections.record_correction(
            transaction_id,
            request.corrected_vendor,
            request.corrected_category,
            user_id=uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/ledger")
def list_ledger(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return db.rows(
        """
        SELECT ledger_entries.*, transactions.parsed_vendor_raw
        FROM ledger_entries
        JOIN transactions ON transactions.id = ledger_entries.transaction_id
        WHERE ledger_entries.user_id = %s
        ORDER BY ledger_entries.id DESC
        """,
        (current_user["id"],),
    )


@app.get("/audit")
def list_audit(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM audit_log WHERE user_id = %s ORDER BY id DESC", (current_user["id"],))


@app.post("/corrections/bulk")
def bulk_review(
    request: BulkCorrectionRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    uid = current_user["id"]
    try:
        return corrections.bulk_action(
            transaction_ids=request.transaction_ids,
            approve=request.approve,
            corrected_vendor=request.corrected_vendor,
            corrected_category=request.corrected_category,
            user_id=uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/admin/reset")
def reset_demo(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Clear demo data and reset auto-increment counters for the authenticated user."""
    deleted = db.reset_demo_data(user_id=current_user["id"])
    return {"status": "reset", "deleted": deleted}


@app.get("/audit-log")
def audit_log(current_user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    uid = current_user["id"]
    return db.rows(
        """
        SELECT 'match_decision' AS record_type, md.id AS record_id, md.transaction_id,
               md.action AS decision, md.reasoning, md.created_at AS timestamp,
               NULL AS event_type, NULL AS actor, NULL AS detail_text
        FROM match_decisions md
        WHERE md.user_id = %s
        UNION ALL
        SELECT 'audit_log' AS record_type, al.id AS record_id, al.transaction_id,
               al.event_type AS decision, al.detail_text AS reasoning, al.created_at AS timestamp,
               al.event_type, al.actor, al.detail_text
        FROM audit_log al
        WHERE al.user_id = %s
        ORDER BY timestamp DESC, record_id DESC
        """,
        (uid, uid),
    )