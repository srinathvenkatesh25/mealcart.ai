"""intake → plan → solve → validate ⇄ repair (≤3) → consolidate → approve → shop
                                              └─ fail (after 3 repairs)

`approve` pauses with a LangGraph interrupt, so the graph needs a checkpointer.
"""

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import RunState


# App classes that may appear in checkpointed state.
CHECKPOINT_TYPES = [("app.models", n) for n in (
    "MealSpec", "MacroTargets", "MealPlan", "DayPlan", "Meal", "Ingredient", "ValidationResult",
    "GroceryList", "GroceryItem", "CartReport", "CartLine", "CoverageRow")]


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def build_graph(checkpointer=None):
    g = StateGraph(RunState)
    g.add_node("intake", nodes.intake)
    g.add_node("plan", nodes.plan)
    g.add_node("solve", nodes.solve)
    g.add_node("validate", nodes.validate)
    g.add_node("repair", nodes.repair)
    g.add_node("consolidate", nodes.consolidate)
    g.add_node("approve", nodes.approve)
    g.add_node("shop", nodes.shop)
    g.add_node("fail", nodes.fail)

    g.add_edge(START, "intake")
    g.add_edge("intake", "plan")
    g.add_conditional_edges("plan", nodes.stop_on_error, {"continue": "solve", "stop": END})
    g.add_edge("solve", "validate")
    g.add_conditional_edges("validate", nodes.after_validate, {"done": "consolidate", "repair": "repair", "fail": "fail"})
    g.add_conditional_edges("repair", nodes.stop_on_error, {"continue": "solve", "stop": END})
    g.add_edge("consolidate", "approve")
    g.add_conditional_edges("approve", nodes.stop_on_error, {"continue": "shop", "stop": END})
    g.add_edge("shop", END)
    g.add_edge("fail", END)
    return g.compile(checkpointer=checkpointer)
