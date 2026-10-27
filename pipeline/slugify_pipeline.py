"""A four-stage SwarmFlow pipeline with a human checkpoint before the docs stage.

The Team Leader runs it with:
    swarmflow(script_path="<abs path to this file>", args='{"feature": "..."}')

Topology (fixed by this file, not decided by a model at run time):
    spec (agent) -> codegen (agent) -> review (human) -> docs (agent)
"""
import json

from swarmflow import agent, human, phase

# META must be a pure literal: SwarmFlow reads it statically before running anything.
# Phase titles must match the phase() calls below.
META = {
    "name": "slugify-pipeline",
    "description": "Spec -> code generation -> human review -> docs",
    "phases": [
        {"title": "Spec"},
        {"title": "Codegen"},
        {"title": "Review"},
        {"title": "Docs"},
    ],
}

# The one line a reviewer's edit will change. Every agent() call is journaled under
# its position in the script plus a hash of its prompt, so changing this constant
# changes only the codegen call's hash (and anything downstream that embeds its output).
CODEGEN_RULES = "Use only the standard library."


async def run(args):
    # swarmflow() passes args as a string; structured input is JSON we parse ourselves.
    a = json.loads(args)

    phase("Spec")
    spec = await agent(
        f"In 3 short bullet points, specify a Python function slugify(title) for: {a['feature']}. "
        "Return only the bullets.",
        label="spec", phase="Spec",
    )

    phase("Codegen")
    code = await agent(
        "Write ONLY the Python source (no prose, no markdown fences) implementing this spec. "
        f"{CODEGEN_RULES}\n\nSpec:\n{spec}",
        label="codegen", phase="Codegen",
    )

    # Human-in-the-loop checkpoint. The run blocks here (no tokens spent, no concurrency
    # slot held) until a person replies. A reply asking for changes is classified as
    # "edit_rerun": the run pauses with a resumable journal instead of continuing.
    phase("Review")
    review = await human(
        "Review the generated code. Reply 'approve' to continue, or describe the edit you want.\n\n"
        + str(code)
    )

    phase("Docs")
    docs = await agent(
        f"Write a two-sentence docstring summary for this code:\n{code}",
        label="docs", phase="Docs",
    )
    return {"spec": spec, "code": code, "review": review, "docs": docs}
