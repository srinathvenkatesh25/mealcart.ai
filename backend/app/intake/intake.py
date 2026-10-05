"""Turn a request into a MealSpec and flag (never reject) odd targets."""

from app.models import MealSpec
from app.nutrition.maps import exclusion_hits
from app.planner.planner import StructuredLLM

INTAKE_SYSTEM = """Extract a meal-planning request into the MealSpec JSON schema.
Use only what the user said; leave everything else at its default.
Calories and protein are per day. Equipment and restrictions are snake_case."""


async def parse_request(text: str, llm: StructuredLLM, run_id: str | None = None) -> MealSpec:
    return await llm.structured(MealSpec, INTAKE_SYSTEM, text, purpose="intake", run_id=run_id, temperature=0)


def feasibility_warnings(spec: MealSpec) -> list[str]:
    warnings = []
    protein_kcal_share = spec.macros.protein_g * 4 / spec.macros.calories
    if protein_kcal_share > 0.6:
        warnings.append(
            f"Protein is {protein_kcal_share:.0%} of your calories; plans will lean very heavily on "
            "lean meat, egg whites or protein powder."
        )
    if spec.macros.calories < 1200:
        warnings.append(f"{spec.macros.calories:.0f} kcal/day is very low; meals will be small.")
    return warnings


def resolve_conflicts(spec: MealSpec) -> tuple[MealSpec, list[str]]:
    """Drop preferences that contradict your own exclusions, and say so.

    A main protein your diet or allergies rule out (chicken + vegetarian) can't be
    satisfied; sent as-is, the planner writes it anyway and every day then fails
    validation. The exclusion wins: it's the safety rule.
    """
    notes = []
    if spec.protein_source:
        hits = exclusion_hits(spec.protein_source, spec.allergies, spec.dietary_restrictions, spec.dislikes)
        if hits:
            kind, label = hits[0]
            notes.append(f"Main protein '{spec.protein_source}' conflicts with your {kind} '{label}', "
                         "so it was ignored. Meals will use other proteins.")
            spec = spec.model_copy(update={"protein_source": None})
    return spec, notes
