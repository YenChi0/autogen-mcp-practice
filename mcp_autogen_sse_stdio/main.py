import asyncio
from pathlib import Path
import os
from dotenv import load_dotenv
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import TextMentionTermination
from autogen_agentchat.ui import Console

# ---------- Env ----------
load_dotenv()
endpoint = os.environ["AOAI_URL"]
api_key = os.environ["AOAI_KEY"]
model_name = os.environ["AOAI_MODEL_NAME"]
api_version = os.environ["OPENAI_API_VERSION"]

async def main() -> None:
    # ---------- MCP tool servers ----------
    math_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "math_server.py")],
    )
    mssql_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "mssql_server.py")],
    )

    math_tools = await mcp_server_tools(math_server)
    mssql_tools = await mcp_server_tools(mssql_server)

    # ---------- LLM client ----------
    model_client = AzureOpenAIChatCompletionClient(
        model=model_name,
        azure_deployment=model_name,
        azure_endpoint=endpoint,
        api_version=api_version,
        api_key=api_key,
    )

    # ---------- Agents ----------
    # 1) The user agent: represents the end user, only speaks when selected by the selector.
    user_agent = UserProxyAgent(
    name="User",
    description=(
        "Represents the end user. Only speak when explicitly selected by the selector, "
        "typically after another agent output includes the token 'REQUEST_USER_INPUT'."
    )
)
    # 2) The tool user: does all resolving + schema + SQL work. No chit-chat.
    sql_agent = AssistantAgent(
        name="SQL_Agent",
        model_client=model_client,
        tools=mssql_tools,  # give it everything it might need
        reflect_on_tool_use=True,
        tool_call_summary_format="{tool_name}({arguments}) -> {result}",  # ← human-readable trace,
        model_client_stream=True,
        system_message=(
            "You are SQL_Agent. Role: find stock id, read schema, and query stTseStkPrcD.\n"
            "Rules:\n"
            "1) If user mentions a company/listCode, FIRST call resolve_stock_id_mssql(raw text).\n"
            "2) If match found -> proceed. If not -> clearly state no match and STOP.\n"
            "3) Call read_schema_csv('stTseStkPrcD_schema.csv') to know columns.\n"
            "4) Use query_sql_mssql with SELECT/WITH only; keep rows small via TOP or OFFSET…FETCH (not both).\n"
            "5) Do NOT format the final table/answer. Leave final formatting to Reporter."
        ),
        description="Finds stock id, inspects schema, and runs read-only SQL to gather data."
    )

    # 3) (Optional) explicit math specialist – kept for completeness
    math_agent = AssistantAgent(
        name="Math_Agent",
        model_client=model_client,
        tools=math_tools,
        reflect_on_tool_use=True,
        tool_call_summary_format="{tool_name}({arguments}) -> {result}",
        model_client_stream=True,
        system_message=(
            "You are Math_Agent. Perform small arithmetic ONLY when asked by SQL_Agent or Reporter. "
            "Be concise; return plain numbers or very short calculations."
        ),
        description="Does small arithmetic when requested.",
    )

    # 4) Final output agent: formats the one-shot answer only.
    reporter = AssistantAgent(
        name="Reporter",
        model_client=model_client,
        tools=[],  # no tools; just format the end result
        reflect_on_tool_use=True,
        tool_call_summary_format="{tool_name}({arguments}) -> {result}",
        model_client_stream=True,
        system_message=(
            "You are Reporter. Produce the FINAL answer only, in 繁體中文 or English to match the user.\n"
            "Formatting rules:\n"
            "- If multiple rows → Markdown table with headers.\n"
            "- If exactly one row → compact bullet list (股票/日期/開盤/高/低/收/成交量 with units when present).\n"
            "- Thousands separators for numbers, dates as YYYY-MM-DD.\n"
            "Do NOT ask confirmation. Do NOT describe steps. Output once.\n"
            "After you output the final answer, append a new line with: TERMINATE"
        ),
        description="Formats and emits the final one-shot answer."
    )

    # ---------- Team (SelectorGroupChat) ----------
    selector_prompt = (
        "You are the team selector. Roles:\n"
        "{roles}\n\n"
        "Conversation so far:\n"
        "{history}\n\n"
        "Select an agent from: {participants}\n\n"
        "Following is the rule to choose an agent:\n"
        "- When the task involves Taiwanese stocks or SQL, prefer SQL_Agent.\n"
        "- Choose SQL_Agent for stock id resolution, schema inspection, and SQL queries.\n"
        "- Choose Math_Agent only when explicit arithmetic is needed.\n"
        "- Only select 'User' if the latest assistant message includes 'REQUEST_USER_INPUT'.\n"
        "- Avoid unnecessary switching; allow the same speaker to continue if still working.\n"
        "Return EXACTLY one name."
    )

    team = SelectorGroupChat(
        participants=[user_agent, sql_agent, math_agent, reporter],
        model_client=model_client,
        termination_condition=TextMentionTermination("TERMINATE"),
        selector_prompt=selector_prompt,
        allow_repeated_speaker=True,
        max_turns=12,
        # candidate_func=candidate_func,  # <— important
    )


    # ---------- REPL (streaming; prints process + final) ----------
    print("=== Autogen Assistant Ready ===")
    print("Type your query below. Type 'exit' to quit.\n")

    
    while True:
        user_input = input(">>> ")
        if user_input.strip().lower() == "exit":
            print("👋 Exiting. Goodbye!")
            break

        await team.reset()
        # Console prints the whole streaming run for you
        await Console(team.run_stream(task=user_input))



if __name__ == "__main__":
    asyncio.run(main())
