"""Turn a request into a MealSpec and flag (never reject) odd targets."""

from app.models import MealSpec
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
