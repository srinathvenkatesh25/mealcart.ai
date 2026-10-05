"""intake → plan → solve → validate ⇄ repair (≤3) → consolidate | fail.

Phase 6 adds plan_approval (interrupt) → shop after consolidate.
"""

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import RunState


def build_graph():
    g = StateGraph(RunState)
    g.add_node("intake", nodes.intake)
    g.add_node("plan", nodes.plan)
    g.add_node("solve", nodes.solve)
    g.add_node("validate", nodes.validate)
    g.add_node("repair", nodes.repair)
    g.add_node("consolidate", nodes.consolidate)
    g.add_node("fail", nodes.fail)

    g.add_edge(START, "intake")
    g.add_edge("intake", "plan")
    g.add_conditional_edges("plan", nodes.stop_on_error, {"continue": "solve", "stop": END})
    g.add_edge("solve", "validate")
    g.add_conditional_edges("validate", nodes.after_validate, {"done": "consolidate", "repair": "repair", "fail": "fail"})
    g.add_conditional_edges("repair", nodes.stop_on_error, {"continue": "solve", "stop": END})
    g.add_edge("consolidate", END)
    g.add_edge("fail", END)
    return g.compile()
