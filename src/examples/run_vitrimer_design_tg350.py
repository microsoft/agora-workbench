"""
===========================================================
Example: Design a Vitrimer with Target Tg = 350 K
===========================================================

Demonstrates a two-stage vitrimer design workflow combining the
vitrimer_vae (AI-guided generative design) and vitrimer_tg_sim
(physics-based MD validation) domain servers, with structured plan
management from the ``planning`` package.

  Stage 1 — AI-guided candidate generation (vitrimer_vae, ~minutes)
    • Bayesian optimization in the HierVAE latent space targeting Tg = 350 K
    • Predict Tg for the top candidates via the VAE property head

  Stage 2 — MD validation of the best candidate (vitrimer_tg_sim, ~hours)
    • Build a simulation box with EMC + PCFF force field
    • Equilibrate via LAMMPS (minimize → NVT → NPT → anneal → 800 K)
    • Run 5 parallel production cooling replicas (800 → 100 K)
    • Compute Tg from bilinear fit of density–temperature profiles
    • Calibrate the MD Tg against experimental scale using GP regression

The agent uses the planning tools to build a structured plan before
execution and tracks progress through each step.

Expected wall time:
  - Stage 1:  ~5–10 min (CPU inference)
  - Stage 2:  ~30–60 min for ~1000 atoms (single CPU core)

Prerequisites:
  - vitrimer-vae-server running on port 8011
  - vitrimer-tg-sim-server running on port 8010
  - VITRIMER_VAE_CHECKPOINT_URL set (for model auto-download)
"""

import asyncio

from agora import AgoraAgent
from dotenv import load_dotenv
from log_config import setup_logging
from planning import PlanStore, create_plan_tools

setup_logging(__file__)
load_dotenv(verbose=True, override=True)


class PlanningAgoraAgent(AgoraAgent):
    """AgoraAgent extended with structured planning tools from the ``planning`` package."""

    def __init__(self, plan_store: PlanStore | None = None, **kwargs):
        super().__init__(**kwargs)
        self.plan_store = plan_store or PlanStore()

    def _build_tools(self) -> tuple[list, list[str]]:
        tools, errors = super()._build_tools()
        tools.extend(create_plan_tools(self.plan_store))
        return tools, errors


agent = PlanningAgoraAgent(
    llm="gpt-5.4",
)


async def main():
    prompt = """
    Design a vitrimer polymer with a target glass transition temperature (Tg) of 350 K.
    Leverage the available tools before making your own approximations.

    Use the plan management tools to build a structured plan before execution,
    then track your progress through each step.

    For the MD validation step, use a small system to keep runtime under ~1 hour:
      - ntotal=500 in build_vitrimer_box

    The build_vitrimer_box tool accepts standard molecule SMILES directly
    (e.g. 'O=C(O)CCCCC(=O)O') — no need to manually add * connection points.

    DO NOT ask the user for assistance. If you encounter a difficulty, proceed using your best judgement.

    Summarize the results:
    - Top 3 candidates from AI search (SMILES + predicted Tg)
    - Selected candidate for MD validation
    - MD-computed Tg (mean ± std across replicas)
    - Calibrated Tg (experimental scale estimate)
    - How close the final result is to the 350 K target
    """

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")
        print(f"\n📋 Final plan state:\n{agent.plan_store.view()}")


if __name__ == "__main__":
    asyncio.run(main())
