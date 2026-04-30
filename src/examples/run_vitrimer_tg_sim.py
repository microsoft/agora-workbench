"""
==================================================
Example: Vitrimer Tg Estimation via MD Simulation
==================================================

Demonstrates the vitrimer_tg_sim domain tools by estimating the glass
transition temperature (Tg) of a single vitrimer system: adipic acid
+ bisphenol A diglycidyl ether (BADGE), the reference compound from
the publication protocol.

This runs a **reduced** simulation for quick testing:
  - ~2000 atoms (2 chains instead of 4)
  - Full equilibration protocol (minimize → NVT → NPT → anneal)
  - 5 parallel production cooling replicas

Expected wall time: ~30-60 min depending on hardware.
For a production-quality result, increase ntotal to ~4000.
"""

import asyncio

from agora import AgoraAgent
from dotenv import load_dotenv
from log_config import setup_logging

setup_logging(__file__)
load_dotenv(verbose=True, override=True)

agent = AgoraAgent(
    llm="gpt-5.2_2025-12-11",
)


async def main():
    prompt = """
    Estimate the glass transition temperature (Tg) of a vitrimer made from
    adipic acid and bisphenol A diglycidyl ether (BADGE) using the
    vitrimer_tg_sim domain tools.

    Use the following SMILES (with connection points for polymerization):
      - Acid: *C(=O)CCCCC(=O)*
      - Epoxide: *C(O)COc1ccc(C(C)(C)c2ccc(OCC(*)O)cc2)cc1

    To keep runtime short, use ntotal=2000 (roughly 2 chains).

    Run the full pipeline:
      1. build_vitrimer_box
      2. run_equilibration
      3. run_tg_production (all 5 replicas in parallel)
      4. compute_tg

    Report the final Tg (mean ± std), coefficient of variation, and
    per-replica Tg values.
    """

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
