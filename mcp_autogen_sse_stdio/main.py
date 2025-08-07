import asyncio
from pathlib import Path
import os
from dotenv import load_dotenv

from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent

# Load .env credentials
load_dotenv()
endpoint = os.environ["AOAI_URL"]
api_key = os.environ["AOAI_KEY"]
model_name = os.environ["AOAI_MODEL_NAME"]
api_version = os.environ["OPENAI_API_VERSION"]

async def main() -> None:
    # Initialize tool servers
    math_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "math_server.py")]
    )
    mssql_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "mssql_server.py")]
    )

    math_tools = await mcp_server_tools(math_server)
    mssql_tools = await mcp_server_tools(mssql_server)
    all_tools = math_tools + mssql_tools

    # Azure OpenAI client
    model_client = AzureOpenAIChatCompletionClient(
        model=model_name,
        azure_deployment=model_name,
        azure_endpoint=endpoint,
        api_version=api_version,
        api_key=api_key,
    )

    # Assistant agent
    assistant = AssistantAgent(
        name="financial_assistant",
        model_client=model_client,
        tools=all_tools,
        reflect_on_tool_use=True,
        system_message=(
            "You are a financial assistant specializing in stock market analysis and data interpretation. "
            "Use 'mssql_tool' to query SQL Server for real financial data. "
            "Use 'math_tool' to calculate averages, ratios, or percentages from data. "
            "Always base answers on tool results. Keep answers short and data-driven."
        )
    )
    # Async input function for UserProxyAgent
    # This allows the user to input queries asynchronously.
    # async def async_input_func(prompt, _=None):
    #     return await asyncio.to_thread(input, prompt)

    # # User proxy agent
    # user = UserProxyAgent(
    #     name="user",
    #     input_func=async_input_func
    # )

    print("=== Autogen Assistant Ready ===")
    print("Type your query below. Type 'exit' to quit.\n")

    while True:
        user_input = input(">>> ")
        if user_input.strip().lower() == "exit":
            print("👋 Exiting. Goodbye!")
            break

        from autogen_agentchat.messages import TextMessage
        from autogen_core import CancellationToken

        token = CancellationToken()

        response = await assistant.on_messages(
            [TextMessage(content=user_input, source="user")],
            cancellation_token=token
        )

        if hasattr(response.chat_message, "content"):
            print(f"\n💬 Assistant: {response.chat_message.content}\n")
        else:
            print("\n⚠️ Assistant did not return a valid message.\n")

if __name__ == "__main__":
    asyncio.run(main())
