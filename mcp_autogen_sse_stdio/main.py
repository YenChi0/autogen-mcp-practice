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
from datetime import datetime, timezone, timedelta
today_taipei = datetime.now(timezone(timedelta(hours=8))).date()

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
    mssql_agent = AssistantAgent(
        name="MSSQL_Agent",
        model_client=model_client,
        tools=mssql_tools,  
        reflect_on_tool_use=True,
        tool_call_summary_format="{tool_name}({arguments}) -> {result}",  # ← human-readable trace,
        model_client_stream=True,
        system_message=(
            f"""
                You are MSSQL_Agent — a **bilingual (繁體中文 / English) financial-data assistant** connected to a Microsoft SQL Server data warehouse, specializing in Taiwanese stock fundamentals and prices.  
                Current calendar date (Asia/Taipei): {today_taipei}

                Rules:
                1) If the user mentions a company name or market id/listCode, first translate it to an internal stock id using `resolve_stock_id_mssql`.
                2) If the user mentions an industry, resolve it using `resolve_stock_industry`.
                3) If match found → proceed. If not → clearly state no match and STOP.
                4) Call read_schema_csv('stTseStkPrcD_schema.csv') before query_sql_mssql to know table columns.
                5) Use query_sql_mssql with SELECT/WITH only; keep rows small via TOP or OFFSET…FETCH (not both).
                6) NEVER execute INSERT/UPDATE/DELETE.
                7) If a query would return more than 500 rows, aggregate or LIMIT 100.
                8) If a user requests data from a month or year, always query the full month/year, not just a single date.
                9) If data is unavailable, say so and suggest an alternative metric.
                10) Before giving the output, always check the unit of each column in stTseStkPrcD_schema.csv.
                11) Do NOT format the final table/answer — leave final formatting to the Reporter.
                12) Remember to always include the column unit in the final output.
            """
            ),
            description="Finds stock id, inspects schema, and runs read-only SQL to gather data."
    )

    # 3) (Optional) explicit math specialist – kept for completeness
    math_agent = AssistantAgent(
        name="Math_Agent",
        model_client=model_client,
        tools=math_tools,
        reflect_on_tool_use=False,
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
        reflect_on_tool_use=False,
        model_client_stream=True,
        system_message=(
            "You are Reporter. Produce the FINAL answer only, in 繁體中文 or English to match the user.\n"
            "Formatting rules:\n"
            "- If multiple rows → Markdown table with headers.\n"
            "- If the SQL output contains exactly one row, present it as a compact bullet list using the available columns in this order when present: 股票, column, column, column, column, column, column (add units only for columns that have them). \n"
            "The stock name must be displayed\n"
            "Do not create or infer any columns that are not present in the SQL output\n"
            "Do not display columns with no results\n"
            "- Thousands separators for numbers, dates as YYYY-MM-DD.\n"
            "Do NOT ask confirmation. Do NOT describe steps. Output once.\n"
            "Remember to always include the column unit in the final output.\n"
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
        "- When the task involves Taiwanese stocks, Market stocks, Industry stocks or SQL, prefer MSSQL_Agent.\n"
        "- Choose MSSQL_Agent for stock id resolution, schema inspection, and SQL queries.\n"
        "- Choose Math_Agent only when explicit arithmetic is needed.\n"
        "- Only select 'User' if the latest assistant message includes 'REQUEST_USER_INPUT'.\n"
        "- Avoid unnecessary switching; allow the same speaker to continue if still working.\n"
        "Return EXACTLY one name."
    )

    team = SelectorGroupChat(
        participants=[user_agent, mssql_agent, math_agent, reporter],
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
