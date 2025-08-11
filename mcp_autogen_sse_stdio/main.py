import asyncio
from pathlib import Path
import os
from dotenv import load_dotenv
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_agentchat.agents import AssistantAgent

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

    # for t in all_tools:
    #     # `t.schema` is usually a dict with name/description/parameters
    #     try:
    #         print(t.schema["function"]["name"], "→", t.schema["function"]["description"])
    #         print(t.schema["function"]["parameters"])
    #     except Exception:
    #         print(getattr(t, "schema", t))

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
            """
            You are a **bilingual (繁體中文 / English) financial-data assistant** connected to a Microsoft SQL Server data-warehouse.
            Current calendar date (Asia/Taipei): {today_taipei}

            ## Your mission
            • Help users explore Taiwanese equities: prices, volumes, market value, and related metrics.

            ## Available tools
            • `resolve_stock_id_mssql(keyword: str)` — Resolve a company name/abbr/listCode to an internal stock id.
                - If no match is found, politely ask the user for clarification before continuing.
                - If a match is found, proceed with the next steps without asking for clarification.
            • `read_schema_csv(file: str | None)` — Read the `stTseStkPrcD_schema.csv` (column names & meanings for daily prices table).
            • `query_sql_mssql(sql: str, limit: int=500)` — Run **read-only** T-SQL (SELECT/WITH only) against SQL Server; auto-limits large results.
            • `add(a: int, b: int)` / `multiply(a: int, b: int)` — Basic arithmetic only (no other math ops).

            ## Workflow you MUST follow
            1) Identify the data
            • When a user mentions a company name / abbreviation / listCode (e.g., 台積電, 2330), first call **`resolve_stock_id_mssql`** with the raw text.
            • If no match is found, politely ask for clarification.
            • If a match is found, proceed directly with the next steps — do not ask the user for confirmation or clarification.

            2) Query column definitions
            • Call **`read_schema_csv`** to load `stTseStkPrcD_schema.csv`.
            • This describes the columns for **`stTseStkPrcD`** (daily stock prices & market data). Use it to choose the right fields.

            3) Query market data
            • Use **`query_sql_mssql`** to read from **`stTseStkPrcD`**.
            • SQL must be **read-only**: **SELECT / WITH only** (never INSERT/UPDATE/DELETE).
            • Column names are case-sensitive; wrap identifiers containing capitals in **square brackets**, e.g. `[AskPrice1]`, `[MktDate]`.
            • Keep result sets small:
                – Prefer aggregation when appropriate, and
                – Limit rows to **≤ 500** using **either**:
                    * `TOP(n)`  
                    * `ORDER BY … OFFSET … FETCH NEXT n ROWS ONLY`  
                **Never use TOP and OFFSET … FETCH in the same query.**
            • If you need arithmetic beyond SQL expressions, you may call **`add`** or **`multiply`** (only those two).

            4) Explain & format
            • When presenting a stock, always prefix with 4-digit id and a representative alias, e.g., **“2330 台積電”**.
            • For numeric answers, include the figure **and** its unit (e.g., “成交量 23 萬張”, “市值 12 兆元”).
            • Be crisp and data-driven; show calculations when helpful; reply in Chinese or English to match the user.
            

            5) OUTPUT RULES — VERY IMPORTANT
            • Use as many tools as needed; do not print intermediate outputs.
            • Speak once with the **final answer only**.
            • If multiple rows → render a Markdown table with headers.
            • If exactly one row → render a compact bullet list.
            • Numbers use thousands separators; dates: YYYY-MM-DD.
            • Numbers: use thousands separators; for volumes add '張' or '股' only if provided by data; otherwise keep raw numbers.
            
            Dates: YYYY-MM-DD.
            TABLE HEADER EXAMPLE (OHLCV):
            | 股票 | 日期 | 開盤 | 高 | 低 | 收 | 成交量 |

            SINGLE-ROW LIST EXAMPLE:
            - 股票：0001 台灣水泥
            - 日期：2025-08-08
            - 開盤：24.35
            - 最高：24.60
            - 最低：24.10
            - 收盤：24.50
            - 成交量：12,345

            ## Safety & etiquette
            • **Never** guess an id — always rely on `resolve_stock_id_mssql`.
            • **Never** modify data (no INSERT/UPDATE/DELETE).
            • If a query would return too many rows, aggregate or restrict with `TOP` / `OFFSET … FETCH`.
            • **Never** use TOP and OFFSET … FETCH in the same query.
            • If data is missing or no rows are returned, say so and suggest a practical alternative (e.g., previous close, recent average).
            • Always ground answers in actual tool outputs; do not fabricate values.
            """
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

    # Add the assistant to the agent chat
    def extract_text(chat_message) -> str:
        # (your existing normalizer — unchanged)
        parts = getattr(chat_message, "content", None)
        if isinstance(parts, str):
            return parts
        if isinstance(parts, list):
            out = []
            for p in parts:
                if hasattr(p, "text") and isinstance(p.text, str):
                    out.append(p.text)
                elif isinstance(p, dict) and isinstance(p.get("text"), str):
                    out.append(p["text"])
                elif isinstance(p, str):
                    out.append(p)
            return "\n".join(t for t in out if t)
        if isinstance(parts, dict):
            if isinstance(parts.get("markdown"), str):
                return parts["markdown"]
            if isinstance(parts.get("text"), str):
                return parts["text"]
        return "" if parts is None else str(parts)

    def needs_continuation(text: str) -> bool:
        """
        Heuristics: return True if the assistant is narrating 'next I'll do X'
        instead of giving the final result (no table / no bullet list yet).
        """
        if not text:
            return True
        t = text.strip()
        # Has markdown table?
        if "|" in t and "---" in t:
            return False
        # Has a bullet-list style final output?
        if any(prefix in t for prefix in ["- 股票：", "• 股票：", "— 股票："]):
            return False
        # Looks like 'I will … please wait/ok'?
        cues = ["接下來我會", "我將查詢", "請稍候", "請稍等", "是否繼續", "ok?"]
        return any(cue in t for cue in cues)

    async def ask_assistant(messages):
        token = CancellationToken()
        return await assistant.on_messages(messages, cancellation_token=token)

    
    print("=== Autogen Assistant Ready ===")
    print("Type your query below. Press exit to exit.\n")
    # ---- Main REPL loop ----
    while True:
        try:
            user_input = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Exiting. Goodbye!")
            break

        if user_input.strip().lower() == "exit":
            print("👋 Exiting. Goodbye!")
            break

        # 1) Send the user's message
        response = await ask_assistant([TextMessage(content=user_input, source="user")])
        text = extract_text(getattr(response, "chat_message", None))

        # 2) If it looks like a mid-task narration, auto-continue (no user 'ok')
        max_auto_steps = 3
        auto_steps = 0
        while needs_continuation(text) and auto_steps < max_auto_steps:
            auto_steps += 1
            # Strong nudge to finish in the SAME turn
            nudge = (
                "不要詢問確認，直接完成所有需要的工具調用，並在同一則回覆中輸出最終結果。"
                "請輸出最終答案（表格或條列），不要再描述接下來要做什麼。"
            )
            response = await ask_assistant([TextMessage(content=nudge, source="system")])
            text = extract_text(getattr(response, "chat_message", None))

        if text.strip():
            print(f"\n💬 Assistant: \n{text}\n")
        else:
            print("\n⚠️ No text content in response.\n")


if __name__ == "__main__":
    asyncio.run(main())
