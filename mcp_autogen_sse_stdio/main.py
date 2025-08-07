import asyncio
from pathlib import Path
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_agentchat.agents import AssistantAgent
from autogen_core import CancellationToken
from autogen_agentchat.ui import Console
import os
from dotenv import load_dotenv


# Get environment variables
load_dotenv()

endpoint = os.environ["AOAI_URL"]
api_key = os.environ["AOAI_KEY"]
model_name = os.environ["AOAI_MODEL_NAME"]
api_version = os.environ["OPENAI_API_VERSION"]

async def main() -> None:

    # Setup server params for local filesystem access
    math_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "math_server.py")]
    )
    math_tools = await mcp_server_tools(math_server)

    # Combine the tools from both servers into a single list
    all_tools = math_tools # + [adapter]

        # ── Model Client ──────────────────────────────────────
    model_client = AzureOpenAIChatCompletionClient(
        model=model_name,  # Azure 部署名稱
        azure_deployment=model_name,
        azure_endpoint=endpoint,
        api_version=api_version,
        api_key=api_key,
    )
    agent = AssistantAgent(
        name="demo_agent",
        model_client=model_client,
        tools=all_tools,
        reflect_on_tool_use=True,
        system_message=(
            "You are an intelligent assistant with access to tools such as 'adapter', "
            "which connects to Apify's rag-web-browser. If you need to search the web, "
            "use the 'adapter' tool, but always request minimal content. maxResults=1, "
            "Only fetch the most relevant and recent information. Avoid large responses "
            "that may exceed token limits by limiting page content size and page count."
        ),
    )

    await Console(
        agent.run_stream(
            task="Summarise the latest news of Iran and US negotiations in one small concise paragraph.",
            cancellation_token=CancellationToken(),
        )
    )
    await Console(
        agent.run_stream(
            task="what's (3 + 5) x 12?", cancellation_token=CancellationToken()
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
