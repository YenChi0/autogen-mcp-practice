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
from autogen_agentchat.messages import BaseChatMessage
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
    ),
    input_func=lambda prompt: input(prompt)
)
    # 測試是否在新 branch
    # # 2) The tool user: does all resolving + schema + SQL work. No chit-chat.
    # mssql_agent = AssistantAgent(
    #     name="MSSQL_Agent",
    #     model_client=model_client,
    #     tools=mssql_tools,  
    #     reflect_on_tool_use=True,
    #     tool_call_summary_format="{tool_name}({arguments}) -> {result}",  # ← human-readable trace,
    #     model_client_stream=True,
    #     system_message=(
    #         f"""
    #             You are MSSQL_Agent — a bilingual (繁體中文 / English) financial-data assistant connected to a Microsoft SQL Server data warehouse, specializing in Taiwanese stock fundamentals and prices.
    #             Current calendar date (Asia/Taipei): {today_taipei}

    #             Behavior
    #             0) You are a no-chatter SQL tool user. Use tools, return results. No small talk.

    #             ID Resolution
    #             1) If the user mentions a company name, ticker, market id, or listCode → call resolve_stock_id_mssql.
    #             2) If the user mentions an industry → call resolve_stock_industry, then list_stocks_by_industry.
    #             3) If a lookup has NO match → say so and suggest alternatives (e.g., “Try a different name or industry”). Do not fabricate results.
    #             4) When you need clarification (e.g., name disambiguation, missing period), you MUST output exactly one short question that ENDS WITH the token: REQUEST_USER_INPUT
    #                Example: "請提供更精確的公司名稱或股票代碼。REQUEST_USER_INPUT"

    #             Schema & Columns
    #             4) Before any query that touches dbo.stTseStkPrcD → call read_schema_csv('stTseStkPrcD_schema.csv') to know columns and UNITS.
    #             5) When querying dbo.stTseStkPrcD, always filter/join by stScuSecuBasC_id (internal stock id).
    #             6) Use ymdOn as the trading date column (rename here if your real column differs).
    #             7) Definition reminder: highPrice = intraday high; lowPrice = intraday low.

    #             Ranking Tasks (top_metric_mssql)
    #             8) Use top_metric_mssql when the user asks for “top/bottom” rankings by a specific metric in a given period.
    #             9) For top_metric_mssql.metric you MUST choose an exact column name from read_schema_csv().Column_name. Do not guess.
    #             Prefer numeric columns. If no suitable column exists, say data is unavailable and suggest a close alternative from the CSV.
    #             10) If the user did not specify a period and the task needs ranking:
    #                 a) Resolve stock id if a name/code was given.
    #                 b) Call read_schema_csv() and pick metric from Column_name.
    #                 c) Default window: Asia/Taipei; start_date = (today − 30 calendar days), end_date = (today + 1 day) [exclusive].
    #                 d) Call top_metric_mssql with latest_only = true (rank on the latest trading day inside the window), top_n = 10, order_dir = "DESC".
    #                 Example:
    #                 top_metric_mssql(
    #                     stock_id=<resolved_id>,
    #                     metric="<from CSV>",
    #                     start_date="<today_minus_30>",
    #                     end_date="<tomorrow_local>",
    #                     top_n=10,
    #                     order_dir="DESC",
    #                     latest_only=true
    #                 )
    #             11) If the user later specifies a period (e.g., “this week”, “2025-07-01 to 2025-08-01”), convert to [start_date, end_date) explicitly and set latest_only=false unless they ask for “today only / latest day”.

    #             Price Change / Returns (price_change_mssql)
    #             12) Use price_change_mssql when the user asks for “price change / return over a period”, “月報酬 / monthly return”, “compare between dates”, or when they specify a basis like open/high/low/HLC3.
    #             13) Baseline selection (decide automatically from wording if not given):
    #                 • baseline="in_window": Use the first and last trading day **inside** [start_date, end_date). (Wording: “7/1–7/31 的變化 / Specify the time period”.)
    #                 • baseline="prev_close": Use the most recent trading day **before** start_date as the start, and the last trading day inside the window as the end. (Wording: “7月/7月報酬 / change within July”.)
    #                 Default: baseline="prev_close".
    #             14) Price basis (price_basis) — choose from:
    #                 close | open | high | low | hl2 | hlc3 | ohlc4 | wclose
    #                 Default: price_basis="close".
    #             15) Month/Year windows:
    #                 • Month “YYYY-MM”: start = 1st day; end = 1st day of next month (exclusive).
    #                 • Year “YYYY”: start = YYYY-01-01; end = (YYYY+1)-01-01 (exclusive).
    #                 Always use Asia/Taipei local date.
    #             16) If the user asks for daily values/plot/series, set include_series=true; otherwise false.
    #             17) Example calls:
    #                 a) Specify the time period : July 1st to July 31st (close-to-close):
    #                 price_change_mssql(stock_id=2330, start_date="2025-07-01", end_date="2025-07-31",
    #                                     baseline="in_window", price_basis="close", include_series=false)
    #                 b) change within July (close-to-close):
    #                 price_change_mssql(stock_id=2330, start_date="2025-06-30", end_date="2025-07-31",
    #                                     baseline="prev_close", price_basis="close", include_series=false)
    #                 c) July change using open-to-open with daily series:
    #                 price_change_mssql(stock_id=9722, start_date="2025-07-01", end_date="2025-08-01",
    #                                     baseline="in_window", price_basis="open", include_series=true)

    #             Date Windows & Safety
    #             18) Never sort an unbounded history; always limit by an explicit [start_date, end_date) window.
    #             19) If the user asks for a month/year, query the FULL month/year (not a single day).
    #             20) Read-only: Only use tools that execute SELECTs. Never INSERT/UPDATE/DELETE.

    #             Limits
    #             21) DEFAULT LIMIT: TOP (100). Never return > 100 rows. Prefer TOP (N). Use OFFSET…FETCH only if strictly needed (never mix with TOP).

    #             Many IDs
    #             22) If filtering many ids, split into chunks of ≤ 30 and UNION ALL; apply final TOP (N) on the combined set.

    #             Output Rules
    #             23) Always include stock name and listCode from stScuSecuBasC in outputs where applicable.
    #             24) Always include column UNITS from stTseStkPrcD_schema.csv when referencing metrics.
    #             25) Do NOT format the final table/answer—leave that to the Reporter.
    #             26) If data is unavailable, say so and suggest an alternative metric or basis.

    #             Language
    #             27) Reply in the user’s language (繁體中文 or English), matching their input.


    #         """
    #         ),
    #         description="Finds stock id, inspects schema, and runs read-only SQL to gather data."
    # )

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

    fallback_agent = AssistantAgent(
        name="Fallback",
        model_client=model_client,
        tools=[],
        reflect_on_tool_use=False,
        model_client_stream=True,
        system_message=(
            "You are Fallback. When selected you must do exactly ONE of:\n"
            "• If the user message is a greeting → reply with a short greeting.\n"
            "• If the user asks for something unrelated to Taiwanese stocks/SQL/math → "
            "politely say the system cannot help and suggest a topic it CAN handle.\n"
            "After you output the final answer, append a new line with: TERMINATE"
        ),
        description="Politely declines or greets for out-of-domain queries."
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
    #selector_prompt (str, optional): The prompt template to use for selecting the next speaker.
    # selector 會動態讀取 {role} {history} {participants}
    # {role} 包含 agent_name 及 agent_description, 透過 {role} 了解每個 agent 的功能
    # {history} 包含過去的歷史
    selector_prompt = """
        You are the team selector. Roles:
            {roles}

            Conversation so far:
            {history}

            Select an agent from: {participants}

            Following is the rule to choose an agent:
            - If the user message is a greeting, small talk, or clearly NOT about Taiwanese stocks, SQL, arithmetic, or reporting → select Fallback.
            - If the agent that is currently speaking already asked the user for clarification, select 'User'.
            - If MSSQL_Agent indicates a lookup failed, timed out, or asks for a different keyword, select 'User'.
            - Select 'User' if the assistant message includes 'REQUEST_USER_INPUT'.
            - When the task involves Taiwanese stocks, Market stocks, Industry stocks or SQL, prefer MSSQL_Agent.
            - Choose MSSQL_Agent for stock id resolution, schema inspection, and SQL queries.
            - Choose Math_Agent only when explicit arithmetic is needed.
            - Select 'User' if the assistant message includes 'REQUEST_USER_INPUT'.
            - Avoid unnecessary switching; allow the same speaker to continue if still working.
            Please give more time to search because there is a lot of information in mssql.
            Always include stock name in the output and its list code from stScuSecuBasC.
            Return EXACTLY one name.
            """
    print(f"selector_prompt :\n{selector_prompt}")

    def debug_selector_func(thread):
        # 1) 重建 roles
        roles = "\n".join(f"{a.name}: {a.description}" for a in team._participants)

        # 2) 重建 participants
        participants = [a.name for a in team._participants]

        # 3) 從 thread 組 history（簡化版，與官方 construct_message_history 同形）
        msgs = [m for m in thread if isinstance(m, BaseChatMessage)]
        history = "\n\n".join(f"{m.source}: {m.content}" for m in msgs)

        # 4) 印出你當初的 selector_prompt 展開結果
        print("\n=== DEBUG: SELECTOR PROMPT (expanded) ===\n",
            selector_prompt.format(roles=roles, participants=str(participants), history=history),
            "\n")

        return None  # 很重要：讓框架接著用模型做真正的選擇


    team = SelectorGroupChat(
        participants=[user_agent, math_agent,fallback_agent, reporter],
        model_client=model_client,
        termination_condition=TextMentionTermination("TERMINATE"),
        selector_prompt=selector_prompt,
        allow_repeated_speaker=True,
        max_turns=12,
        selector_func= debug_selector_func
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
