"""
LangGraph pipeline definition.

Graph topology:
  START
    → extract_node
    → [route_after_extract] → decide_node | dead_letter
    → [route_after_decide]  → notify_node | execute_node | dead_letter
    → [interrupt]           ← human approval pause point
    → [route_after_human]   → execute_node | dead_letter
    → execute_node
    → END

Interview point — LangGraph interrupt vs. polling:
────────────────────────────────────────────────────
LangGraph `interrupt()` suspends the graph at a specific node and
serialises the entire state to the Postgres checkpointer. The graph
thread ID is stored on the Invoice row. When the Slack callback arrives,
we call `graph.ainvoke({"human_action": "approve"}, config)` with the
same thread_id — LangGraph rehydrates the state from Postgres and resumes
exactly where it left off. No polling loop needed, and the state survives
a process restart because it lives in the DB, not in RAM.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.graph.nodes import (
    decide_node,
    execute_node,
    notify_node,
    route_after_decide,
    route_after_extract,
    route_after_human,
    extract_node,
)
from app.graph.state import InvoiceState


def dead_letter_node(state: InvoiceState) -> dict:
    """Terminal node for failed/rejected invoices."""
    import structlog
    structlog.get_logger().warning(
        "invoice_dead_lettered",
        invoice_id=state.get("invoice_id"),
        decision=state.get("decision"),
        error=state.get("error") or state.get("extraction_error"),
    )
    return {}


def human_approval_node(state: InvoiceState) -> dict:
    """
    Interrupt node — execution pauses here waiting for human input.

    LangGraph's interrupt() raises a special exception that the framework
    catches; it serialises state to the checkpointer and returns a
    'interrupted' run status to the caller. The graph resumes when
    ainvoke() is called again with the same thread_id.
    """
    from langgraph.types import interrupt
    # This call never returns on first pass — it's the suspend point.
    human_action = interrupt(
        value={
            "prompt": "Approve or reject this invoice?",
            "invoice_id": state.get("invoice_id"),
            "decision_reasons": state.get("decision_reasons"),
        }
    )
    return {"human_action": human_action}


def build_graph(checkpointer=None) -> StateGraph:
    """
    Build and compile the invoice processing graph.

    Pass a checkpointer (AsyncPostgresSaver) for production use.
    Pass None for tests (in-memory, no persistence).
    """
    builder = StateGraph(InvoiceState)

    # ── Register nodes ────────────────────────────────────────────────
    builder.add_node("extract", extract_node)
    builder.add_node("decide", decide_node)
    builder.add_node("notify", notify_node)
    builder.add_node("human_approval", human_approval_node)
    builder.add_node("execute", execute_node)
    builder.add_node("dead_letter", dead_letter_node)

    # ── Edges ─────────────────────────────────────────────────────────
    builder.add_edge(START, "extract")

    builder.add_conditional_edges(
        "extract",
        route_after_extract,
        {"decide": "decide", "dead_letter": "dead_letter"},
    )

    builder.add_conditional_edges(
        "decide",
        route_after_decide,
        {
            "notify": "notify",
            "execute": "execute",
            "dead_letter": "dead_letter",
        },
    )

    # After notify → human_approval (interrupt point)
    builder.add_edge("notify", "human_approval")

    builder.add_conditional_edges(
        "human_approval",
        route_after_human,
        {"execute": "execute", "dead_letter": "dead_letter"},
    )

    builder.add_edge("execute", END)
    builder.add_edge("dead_letter", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_approval"],  # pause BEFORE human_approval runs
    )


async def get_graph_with_checkpointer():
    """
    Factory for the production graph with Postgres checkpointer.
    Call once at app startup and reuse.
    """
    from app.core.config import settings

    # AsyncPostgresSaver uses the sync DB URL (psycopg3 under the hood).
    # The checkpointer creates its own tables (langgraph_checkpoints etc.)
    # on first use — no manual migration needed.
    checkpointer = await AsyncPostgresSaver.from_conn_string(
        settings.database_url_sync
    )
    await checkpointer.setup()  # creates checkpoint tables if not present
    return build_graph(checkpointer=checkpointer)
