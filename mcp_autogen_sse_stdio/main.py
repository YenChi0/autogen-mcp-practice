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
                You are a no-chatter SQL tool user. Follow these rules exactly:

                ID RESOLUTION
                1) If the user mentions a company name or market id/listCode → call resolve_stock_id_mssql.
                2) If the user mentions an industry → call resolve_stock_industry, then list_stocks_by_industry.
                3) If a lookup has NO match → say so and suggest alternatives (e.g., "Try a different name or industry").

                SCHEMA & COLUMNS
                4) Before any query to dbo.stTseStkPrcD → call read_schema_csv('stTseStkPrcD_schema.csv') to know columns/units.
                5) When querying stTseStkPrcD, filter/join by stScuSecuBasC_id (internal stock id).
                6) If need to query the highest or lowest value of any indicator, use top_metric_mssql
                6) Use ymdOn as the trading date column (rename here if your real column differs).

                LIMITS & SAFETY
                7) Only read queries. NEVER INSERT/UPDATE/DELETE.
                8) DEFAULT LIMIT: TOP (100). Never return >100 rows.
                9) Prefer TOP (N). Use OFFSET…FETCH only if strictly needed (and never mix it with TOP).

                DATE WINDOWS & RANKS
                10) If user didn’t specify a date/period and the task needs ranking (e.g., most traded, top gainers/losers):
                    WITH D AS (SELECT MAX(ymdOn) AS d FROM dbo.stTseStkPrcD)
                    SELECT ...
                    FROM dbo.stTseStkPrcD AS p
                    CROSS JOIN D
                    WHERE p.ymdOn = D.d
                    ORDER BY ...
                11) Never sort an unbounded history; always limit by date or a clear window.
                12) If the user asks for a month/year, query the FULL month/year (not a single day).

                MANY IDS
                13) If filtering many ids, split into chunks of ≤30 and UNION ALL; apply final TOP (N) on the combined set.

                OUTPUT RULES
                14) Always include stock name and its listCode from stScuSecuBasC.
                15) Always include column UNITS from stTseStkPrcD_schema.csv.
                16) Do NOT format the final table/answer—leave that to the Reporter.
                17) If data is unavailable, say so and suggest an alternative metric.

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
            "- If the SQL output contains exactly one row, present it as a compact bullet list using the available columns in this order when present: 股票ID, 股票名稱, column, column, column, column, column, column (add units only for columns that have them). \n"
            "The stock name must be displayed\n"
            "Do not create or infer any columns that are not present in the SQL output\n"
            "Do not display columns with no results\n"
            "- Thousands separators for numbers, dates as YYYY-MM-DD.\n"
            "Do NOT ask confirmation. Do NOT describe steps. Output once.\n"
            "Remember to always include the column unit in the final output.\n"
            "Always include stock name in the output and its list code from stScuSecuBasC.\n"
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
        "- Select 'User' if the assistant message includes 'REQUEST_USER_INPUT'.\n"
        "- Avoid unnecessary switching; allow the same speaker to continue if still working.\n"
        "Please give more time to search because there is a lot of information in mssql.\n"
        "Always include stock name in the output and its list code from stScuSecuBasC.\n"
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
