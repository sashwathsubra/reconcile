from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app import db
from app.services.categorization import categorize_transaction


class ReconcileState(TypedDict, total=False):
    transaction: dict[str, Any]
    memory: list[dict[str, Any]]
    decision: dict[str, Any]
    ledger_result: dict[str, Any]


def _persist_decision(
    transaction: dict[str, Any],
    decision: dict[str, Any],
    user_id: int | None = None,
) -> None:
    transaction_id = transaction.get("id")
    if not transaction_id:
        return

    uid = user_id or transaction.get("user_id") or 1
    with db.connection() as conn:
        latest = db.one(
            "SELECT id FROM match_decisions WHERE transaction_id = %s AND user_id = %s ORDER BY id DESC LIMIT 1",
            (transaction_id, uid),
        )
        if latest:
            conn.execute(
                "UPDATE match_decisions SET action = %s, reasoning = %s WHERE id = %s",
                (decision.get("action"), decision.get("reasoning", ""), latest["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO match_decisions
                (user_id, transaction_id, proposed_vendor, category, confidence_score, reasoning, action, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uid,
                    transaction_id,
                    decision.get("proposed_vendor", "UNKNOWN"),
                    decision.get("category", "ERROR"),
                    float(decision.get("confidence_score", 0)),
                    decision.get("reasoning", ""),
                    decision.get("action"),
                    db.now(),
                ),
            )


def categorize_node(state: ReconcileState) -> ReconcileState:
    uid = state["transaction"].get("user_id") or 1
    result = categorize_transaction(
        state["transaction"],
        state.get("memory", []),
        user_id=uid,
    )
    return {"decision": result}


def evaluate_node(state: ReconcileState) -> ReconcileState:
    """
    Evaluate the categorization decision and check for duplicates.
    Scoped by user_id.
    """
    decision = dict(state["decision"])
    confidence = float(decision.get("confidence_score", 0))
    transaction = state["transaction"]
    uid = transaction.get("user_id") or 1

    # Check if categorization failed
    if "error" in decision:
        decision["action"] = "escalate"
        decision["escalation_reason"] = f"Categorization error: {decision.get('reasoning', 'unknown error')}"
        _persist_decision(transaction, decision, user_id=uid)
        return {"decision": decision}

    # Check for duplicate transaction (same amount + date + vendor) within the user's data
    if transaction.get("id"):
        dup_check = db.one(
            """
            SELECT id FROM transactions
            WHERE id != %s AND user_id = %s AND status != 'flagged' 
              AND parsed_amount = %s AND parsed_date = %s AND parsed_vendor_raw = %s
            LIMIT 1
            """,
            (
                transaction.get("id"),
                uid,
                transaction.get("parsed_amount"),
                transaction.get("parsed_date"),
                transaction.get("parsed_vendor_raw"),
            ),
        )
        if dup_check:
            decision["action"] = "escalate"
            decision["escalation_reason"] = f"Duplicate of transaction {dup_check['id']}"
            _persist_decision(transaction, decision, user_id=uid)
            return {"decision": decision}

    # Middle-confidence unmatched vendors need one more human detail before routing.
    if 0.6 <= confidence < 0.85:
        proposed_vendor = str(decision.get("proposed_vendor", "")).strip().lower()
        known_rule = (
            db.one(
                """
                SELECT id FROM vendor_rules_memory
                WHERE user_id = %s
                  AND (LOWER(vendor_raw_pattern) = %s
                       OR LOWER(canonical_vendor) = %s
                       OR %s LIKE '%%' || LOWER(vendor_raw_pattern) || '%%')
                LIMIT 1
                """,
                (uid, proposed_vendor, proposed_vendor, proposed_vendor),
            )
            if proposed_vendor
            else None
        )
        if not known_rule:
            decision["action"] = "request_more_info"
            decision["escalation_reason"] = "Middle-confidence proposal has no learned vendor rule"
            decision["reasoning"] = f"{decision.get('reasoning', '')} Requesting a human detail before matching."
            with db.connection() as conn:
                conn.execute(
                    "UPDATE transactions SET status = 'needs_info' WHERE id = %s",
                    (transaction.get("id"),),
                )
            _persist_decision(transaction, decision, user_id=uid)
            db.add_audit(transaction.get("id"), "request_more_info", decision, user_id=uid)
            return {"decision": decision}

    # Confidence-based routing
    decision["action"] = "auto_post" if confidence >= 0.85 else "escalate"
    _persist_decision(transaction, decision, user_id=uid)
    return {"decision": decision}


def route_after_evaluation(state: ReconcileState) -> str:
    return state["decision"]["action"]


def request_more_info_node(state: ReconcileState) -> ReconcileState:
    transaction = state["transaction"]
    uid = transaction.get("user_id") or 1
    with db.connection() as conn:
        conn.execute(
            "UPDATE transactions SET status = 'needs_info' WHERE id = %s",
            (transaction.get("id"),),
        )
    db.add_audit(
        transaction.get("id"),
        "human_info_required",
        state["decision"],
        user_id=uid,
    )
    return state


def auto_post_node(state: ReconcileState) -> ReconcileState:
    """
    Attempt to auto-post the transaction to the ledger.
    """
    from app.services.ledger import sync_pending_transactions

    transaction = state["transaction"]
    uid = transaction.get("user_id") or 1
    adapter = "local_demo" if transaction.get("source") in ["synthetic_demo", "csv_upload"] else "quickbooks_sandbox"

    try:
        result = sync_pending_transactions([transaction["id"]], user_id=uid, adapter=adapter)
        state["ledger_result"] = result

        db.add_audit(
            transaction.get("id"),
            "auto_posted_to_ledger",
            {
                "decision": state["decision"],
                "ledger_result": result,
            },
            user_id=uid,
        )
    except Exception as exc:
        # Sync itself failed — mark transaction as flagged
        error_msg = str(exc)
        with db.connection() as conn:
            conn.execute(
                "UPDATE transactions SET status = 'flagged' WHERE id = %s",
                (transaction.get("id"),),
            )

        db.add_audit(
            transaction.get("id"),
            "ledger_sync_failed",
            {
                "error": error_msg,
                "exception_type": type(exc).__name__,
            },
            user_id=uid,
        )

    return state


def escalate_node(state: ReconcileState) -> ReconcileState:
    transaction = state["transaction"]
    decision = state["decision"]
    uid = transaction.get("user_id") or 1

    with db.connection() as conn:
        conn.execute(
            "UPDATE transactions SET status = 'flagged' WHERE id = %s AND status != 'posted'",
            (transaction.get("id"),),
        )

    db.add_audit(
        transaction.get("id"),
        "human_review_required",
        decision,
        user_id=uid,
    )

    return state


def audit_node(state: ReconcileState) -> ReconcileState:
    transaction = state["transaction"]
    decision = state["decision"]
    uid = transaction.get("user_id") or 1

    db.add_audit(
        transaction.get("id"),
        "agent_decision",
        decision,
        user_id=uid,
    )

    return state


def build_graph():
    graph = StateGraph(ReconcileState)

    graph.add_node("categorize", categorize_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("auto_post", auto_post_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("request_more_info", request_more_info_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "categorize")
    graph.add_edge("categorize", "evaluate")

    graph.add_conditional_edges(
        "evaluate",
        route_after_evaluation,
        {
            "auto_post": "auto_post",
            "escalate": "escalate",
            "request_more_info": "request_more_info",
        },
    )

    graph.add_edge("auto_post", "audit")
    graph.add_edge("escalate", "audit")
    graph.add_edge("request_more_info", "audit")
    graph.add_edge("audit", END)

    return graph.compile()