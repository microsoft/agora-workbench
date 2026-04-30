import asyncio
import logging
from agent_bot.agora import AgoraAgent
from dotenv import load_dotenv

# ── Logging setup ──────────────────────────────────────────────
# All agent logs go to agent_run.log; only high-level user messages go to console.
LOG_FILE = "agent_run.log"

# File handler: captures everything (DEBUG+)
file_handler = logging.FileHandler(LOG_FILE, mode="w")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
)

# Console handler: only user-facing messages (INFO+)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))

# Root logger → file (catch-all for all modules)
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.handlers.clear()
root.addHandler(file_handler)

# "user" logger → console + file (high-level progress)
user_logger = logging.getLogger("user")
user_logger.addHandler(console_handler)

# "status" logger → console + file (phase updates)
status_logger = logging.getLogger("status")
status_logger.addHandler(console_handler)

logging.getLogger("httpx").setLevel(logging.WARNING)  # Quiet noisy HTTP client
logging.getLogger("httpcore").setLevel(logging.WARNING)
# ────────────────────────────────────────────────────────────────


load_dotenv(verbose=True, override=True)

# Create agent with no code interpreter and no explicit tool registry.
# The agent auto-discovers tool registries from all MCP servers in server_registry.yaml.
agent = AgoraAgent(
    llm="gpt-5.2_2025-12-11",
)


async def main():
    prompt = """
I've placed a file "29_VA_Post_Summary_N-1_v5_BRK2_DVP.xlsx" in the data lake, which I would like to understand more about.

Would you please examine the file contents and return to me a summary of the file structure.
"""

    async with agent:
        result = await agent.go(prompt)
        print(f"\n✅ Final answer: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
