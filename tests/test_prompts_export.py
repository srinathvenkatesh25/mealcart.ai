"""prompts/ is a readable copy of the prompts, settings and schemas in the code; it must not go stale."""

from app.planner import export_prompts


def test_prompts_folder_matches_the_code():
    stale = [rel for rel, text in export_prompts.render().items()
             if not (export_prompts.OUT / rel).is_file() or (export_prompts.OUT / rel).read_text() != text]
    assert not stale, f"prompts/ is out of date ({stale}): run python -m app.planner.export_prompts from backend/"


def test_rendered_planner_prompt_carries_the_users_rules():
    text = export_prompts.render()["system_prompts.md"]
    assert "peanut (allergy)" in text and "at most 30 minutes" in text and "{exclusions}" in text
