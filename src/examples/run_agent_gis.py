"""
==================================================
Example: GIS Agent with MCP Code Execution
==================================================

1. Agent auto-discovers the GIS MCP server from server_registry.yaml
2. The GIS server provides code execution with geospatial packages pre-installed
3. Agent uses the GIS domain prompt for guided geospatial analysis
4. Domain skills are discovered on demand via ``query_state_graph``
   and loaded with ``load_skill``
5. Results such as interactive maps are saved to /tmp/maps/ for the user to open
"""

import asyncio
from agora import AgoraAgent
from dotenv import load_dotenv
from log_config import setup_logging

setup_logging(__file__)


load_dotenv(verbose=True, override=True)

agent = AgoraAgent(
    llm="gpt-5.2_2025-12-11",
    domain_prompt_path="domains/gis/domain_prompt/gis.jinja",
)


async def main():
    prompt = """Can you visualize the data center location on the map?
"""

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
