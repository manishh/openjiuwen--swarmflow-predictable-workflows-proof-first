"""Three sequential agents, used to show SwarmFlow's two token ceilings."""
from swarmflow import agent, phase, budget, log

META = {
    "name": "budget-demo",
    "description": "outline -> draft -> headline",
    "phases": [{"title": "Outline"}, {"title": "Draft"}, {"title": "Headline"}],
    # Per-run token ceiling for this run only (the team-wide ceiling is set separately).
    "workflow_token_limit": 30000,
}


async def run(args):
    phase("Outline")
    outline = await agent("List 3 bullet points for a short note about URL slugs.", label="outline", phase="Outline")
    # budget.* reads the per-run layer: total is META["workflow_token_limit"] (None if unset).
    log(f"after outline: spent={budget.spent()} total={budget.total}")
    phase("Draft")
    draft = await agent(f"Turn these bullets into one short paragraph:\n{outline}", label="draft", phase="Draft")
    log(f"after draft: spent={budget.spent()} total={budget.total}")
    phase("Headline")
    headline = await agent(f"Write a headline of at most 8 words for:\n{draft}", label="headline", phase="Headline")
    return {"outline": outline, "draft": draft, "headline": headline}
