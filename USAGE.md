# Using Reconcile

Welcome to Reconcile, an agentic bookkeeping control room.

## Getting Started

### Creating an Account
You can create an account in two ways:
1. **Google Sign-In**: Click the "Sign in with Google" button on the login page for an instant, secure login.
2. **Email & Password**: Use the "Sign Up" tab to register with an email and password. We will send a 6-digit One-Time Password (OTP) to your email (currently logged to the server console during local development). Enter this code to activate your account.

### Persistent Login
Once logged in, your session is remembered securely. You can close the tab and return later without needing to sign in again. To clear your session, use the **Log out** button in the top right of the Control Room.

> **Note**: For local development or zero-friction access, you can set `REQUIRE_LOGIN=false` in your `.env` file to bypass all authentication screens. To re-enable full auth, set it to `true` or remove the flag.

## The Control Room

### Running the Sandbox Demo
Click the **"Run Plaid sandbox demo"** button to simulate fetching transactions from a bank account and running them through the categorization pipeline.
This simulates:
- Fetching 10-20 raw transactions.
- Invoking the AI agent to extract clean vendor names and appropriate ledger categories.
- Applying any saved rules for known vendors.

### The Human Review Queue
The AI agent is designed to confidently categorize common transactions. However, if it encounters an ambiguous transaction, it flags it for human review rather than guessing.

1. Review the flagged transactions in the queue.
2. Update the "Vendor" or "Category" fields if the agent's guess needs correction.
3. Check the "Approve" box to confirm the details are correct.
4. Use **Submit approved transactions** to post them to the ledger.

### Agent Decision Evidence (Audit Log)
Transparency is a core feature of Reconcile. Scroll to the **Agent decision evidence** section to see exactly *why* the AI categorized a transaction the way it did, or why it chose to flag it for human review.

## Data Privacy
Your financial data is processed in accordance with India's Digital Personal Data Protection Act, 2023 (DPDP Act). Your data is fully isolated from other users.
