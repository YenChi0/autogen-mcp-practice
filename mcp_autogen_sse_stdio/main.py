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
    menu_server = StdioServerParams(
        command="python",
        args=[str(Path(__file__).parent / "menu_server.py")],
    )

    math_tools = await mcp_server_tools(math_server)
    menu_tools = await mcp_server_tools(menu_server)

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
    description = (
        "End user role. Speaks only when prompted by REQUEST_USER_INPUT. "
        "Provides order lines in the format '<品項> x<數量>, …' using exact menu names, "
        "and replies '確認'/'Y' to confirm. Selector: choose me on REQUEST_USER_INPUT, CONFIRM_ORDER, or INVALID_ORDER."
    ),
    input_func=lambda prompt: input(prompt)
)


    # 3) (Optional) explicit math specialist – kept for completeness
    menu_agent = AssistantAgent(
        name="Menu_Agent",
        model_client=model_client,
        tools=menu_tools,
        reflect_on_tool_use=False,
        model_client_stream=True,
        system_message=(
            "You are Menu_Agent for a food-ordering system.\n"
            # 菜單模式
            "- If the user asks to see the menu: call show_menu_list and then output EXACTLY two lines:\n"
            "  REPORTER_READY\n"
            "  <PASTE THE RAW JSON ARRAY HERE>\n"
            "(A real JSON array: [{\"item\":\"魯肉便當\",\"price\":80.0}, ...]. No quotes. No prose.)\n"
            "\n"
            # 訂單模式（關鍵修補）
            "- If the user sends an ORDER in natural language:\n"
            "  • Do NOT call show_menu_list.\n"
            "  • Call ONLY parse_and_bind(<user_text>) to validate by the CSV menu and get {items, prices, quantities}.\n"
            "  • If result.items is empty → output EXACTLY:\n"
            "      INVALID_ORDER\n"
            "      example=魯肉便當 x2, 雞排便當 x1\n"
            "      REQUEST_USER_INPUT\n"
            "    (No other text.)\n"
            "  • If parse_and_bind tool result.items is non-empty → output EXACTLY:\n"
            "      CONFIRM_ORDER\n"
            "      請輸入「確認」或「Y」來確認訂單!\n"
            "      items=<result.items as compact JSON>\n"
            "      REQUEST_USER_INPUT\n"
            "    (No totals. No tables. No extra lines.)\n"
            "  • If the latest USER message is '確認' or 'Y' → output EXACTLY:\n"
            "      op=total_cost\n"
            "      prices=<result.prices>\n"
            "      quantities=<result.quantities>\n"
            "      items=<result.items>\n"
            "      REQUEST_MATH\n"
            "\n"
            "Hard rules:\n"
            "- NEVER rename/normalize/translate item names or prices.\n"
            "- Do NOT compute totals. Do NOT render tables.\n"
            "- Do NOT print tool stack traces or error dumps; only use the fixed tokens above."
        ),
        description = (
            # "Menu retrieval + order validation. Tools: show_menu_list → outputs REPORTER_READY + raw JSON array; "
            # "Menu retrieval + order validation. When the user types '菜單'/'menu' or sends order text, select me."
            # "After calling show_menu_list I HAVE TO POST EXACTLY TWO LINES: "
            # "REPORTER_READY"
            # "<raw JSON array>"
            # "Do not switch away until I post those two lines. For orders I emit: INVALID_ORDER + REQUEST_USER_INPUT,"
            # "or CONFIRM_ORDER + items + REQUEST_USER_INPUT; after user '確認'/'Y' I emit REQUEST_MATH with prices/quantities/items."
            # "parse_and_bind(text) → returns {items, prices, quantities}. "
            # "Emits: REPORTER_READY (menu), INVALID_ORDER (with one example) + REQUEST_USER_INPUT, "
            # "CONFIRM_ORDER + items JSON + REQUEST_USER_INPUT, and after user '確認'/'Y' → REQUEST_MATH with prices/quantities/items. "
            # "If the user replies with '確認' or 'Y' (confirming an order) → select me"
            # "Selector: choose me when the user asks for the menu ('菜單'/'menu') or submits order text (e.g., 'x2/×2/2份'). "
            # "Never compute totals or render tables; never change item names or prices."
            """
            Menu & order handler.\n
            Reads menu from CSV and returns the menu as a raw JSON array string; parses natural-language orders and binds exact names, prices, quantities from the CSV; never computes totals or changes names/prices.\n
            Tools: show_menu_list, parse_order, parse_and_bind. \n
            Output tokens are produced by its system prompt (e.g., REPORTER_READY, INVALID_ORDER, CONFIRM_ORDER, REQUEST_MATH).
            """
        )   
    )
    math_agent = AssistantAgent(
        name="Math_Agent",
        model_client=model_client,
        tools=math_tools,
        reflect_on_tool_use=False,
        model_client_stream=True,
        system_message=(
            "You are Math_Agent. Respond only when the latest assistant message contains 'REQUEST_MATH'.\n"
            "- Inputs: accept only prices=[...], quantities=[...]; both must be numeric arrays of equal length. Ignore other fields.\n"
            "- If inputs are missing/invalid/mismatched → output exactly: ERROR: missing_or_invalid_inputs.\n"
            "- Otherwise compute the grand total using your math tools.\n"

            "Output (success) must be EXACTLY TWO LINES and nothing else:\n"
            "Line 1: REPORTER_READY\n"
            "Line 2: <raw number>     (no units, no prose, no commas)\n"

            "Hard rules:\n"
            "- Do not call non-math tools.\n"
            "- Do not echo inputs or add explanations.\n"
            "- Do not output any extra whitespace/lines beyond the two lines on success.\n"
        ),
        description = (
            # "Arithmetic worker. Respond ONLY when the latest assistant message contains REQUEST_MATH. "
            # "Input: prices=[...], quantities=[...]. Output: single number or 'ERROR: missing_or_invalid_inputs'. "
            # "Selector: choose me only on REQUEST_MATH."
            """
            Arithmetic worker. Computes numeric results (e.g., total price) from numeric arrays via math tools; outputs a single number (no units/prose).\n
            Tools: add, multiply, subtract, divide, mean.
            """
        )
    )

    fallback_agent = AssistantAgent(
        name="Fallback",
        model_client=model_client,
        tools=[],
        reflect_on_tool_use=False,
        model_client_stream=True,
        system_message=(
            "You are Fallback_Agent. Handle greetings, off-topic requests, and light guidance.\n"
            "- Reply briefly in 繁體中文 or English to match the user.\n"
            "- Proactively inform: 本系統可協助『訂便當』；若需要查看品項與價格，請輸入「菜單」（or type 'menu' to see the items and prices).\n"
            #   "- If the user likely wants to order but info is missing, show ONE short example of the order format, then output on separate lines:\n"
            #   "  REQUEST_USER_INPUT\n"
            "- NEVER call tools, NEVER compute, and NEVER alter item names or prices.\n"
            "- Append TERMINATE after your reply."
        ),
        description = (
            """Greetings & guidance. Briefly explains the system helps order bentos; to view menu, type「菜單」(or “menu”). No tools, no calculations."""
        )
    )

    # 4) Final output agent: formats the one-shot answer only.
    reporter = AssistantAgent(
        name="Reporter",
        model_client=model_client,
        tools=[],  # no tools; just format the end result
        reflect_on_tool_use=False,
        model_client_stream=True,
       system_message=(
             "You are Reporter. Output once per turn, in 繁體中文 or English to match the user.\n"
            # 菜單模式
            # "- MENU MODE: If you receive a JSON array where each element is {item, price} (no qty), render a Markdown table:\n"
            "- MENU MODE: Render ONLY if the latest assistant message has EXACTLY two lines:"
            "    line1 == 'REPORTER_READY'"
            "   line2 == a valid JSON array string like [{""item"":..., ""price"":...}, ...]"
            "- Otherwise output exactly: REQUEST_USER_INPUT and stop."
            "- If you do NOT receive a valid JSON array (menu) nor items/total for a receipt in the latest context, output exactly: REQUEST_USER_INPUT and stop.\n"
            "  zh-TW headers: 品項 | 單價(元)   /   EN: Item | Price (NT$). Do NOT change names or prices.\n"
            "- After the table, print ONE short line with the order format example (e.g., '魯肉便當2份'), then output only: REQUEST_USER_INPUT. Do not append TERMINATE in menu mode.\n"
            # 訂單模式
            "- ORDER MODE: If items include quantities (or you have prices[] + quantities[]), render a receipt table using EXACT names/prices:\n"
            "  zh-TW: 品項 | 數量 | 單價(元) | 小計(元)    /    EN: Item | Qty | Unit price (NT$) | Subtotal (NT$).\n"
            "- If per-row subtotal is missing, show price × qty for display only (formatting-only; do not change prices).\n"
            "- Total:\n"
            "    • If a total number from Math_Agent is present, show it (zh-TW: **總金額：<金額> 元** / EN: **Total: NT$ <amount>**), then append: TERMINATE.\n"
            "    • Otherwise, you MUST NOT compute it yourself. Output exactly:\n"
            "      REQUEST_MATH\\n"
            "      op=total_cost\\n"
            "      prices=[...]\\n"
            "      quantities=[...]\n"
            "      and STOP.\n"
            # 共同約束
            "- Never modify/normalize/translate names or prices. Do not invent fields. Use thousands separators; currency unit: 元 / NT$."
        ),

        description = (
            # "Formatter for menu and receipts. Respond ONLY when the latest assistant message contains REPORTER_READY. "
            # "Formatter. Select me ONLY when the latest assistant message includes REPORTER_READY (from Menu_Agent)."
            # "I render the menu table using exact names/prices when message include REPORTER_READY, then REQUEST_USER_INPUT. "
            # "For receipts, after Math_Agent returns a number, I output the final receipt and TERMINATE. No calculations."
            # "Menu: render table using exact names/prices, then emit REQUEST_USER_INPUT. "
            # "Order: when items and total are available, render receipt and append TERMINATE. "
            # "Selector: choose me when the latest assistant message includes REPORTER_READY (menu), "
            # "If the message DOES NOT include REPORTER_READY DO NOT choose me."
            # "or after Math_Agent returns the total number to finalize the receipt. "
            # "Never compute; never alter names/prices."
            """
            Token-gated presenter. Select me ONLY when the latest assistant message includes an explicit trigger (e.g., 'REPORTER_READY') or immediately after Math_Agent outputs a single number.\n
            I do not call tools or perform any calculations.
            """
        )
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

        Rules (pick EXACTLY one):

        # High-priority routing tokens
        - If the latest assistant message includes 'REQUEST_USER_INPUT' → select User.
        - If the latest assistant message includes 'CONFIRM_ORDER' or 'INVALID_ORDER' → select User.
        - If the latest assistant message includes 'REPORTER_READY' → select Reporter.
        - Reporter must be selected when the latest assistant message includes 'REPORTER_READY'.
        - If the latest assistant message includes 'REQUEST_MATH' → select Math_Agent.
        - Math_Agent must be selected ONLY when the latest assistant message includes 'REQUEST_MATH'.

        # Menu viewing
        - If the user asks to see the menu (e.g., '菜單', 'menu', '有哪些便當') → select Menu_Agent.

        # Ordering flow (must validate BEFORE math)
        - If the latest HUMAN user message looks like an order (mentions item names and quantities;
        e.g., patterns like 'x2', '×2', '2份', '2個', comma/、-separated items) → select Menu_Agent.
        - If the user replies with '確認' or 'Y' (confirming an order) → select Math_Agent.
        - If the user DOES NOT replies with '確認' or 'Y' (confirming an order) → select Fallback.

        # Very important guardrail
        - Do NOT select Math_Agent for a natural-language order message.
        Math_Agent should be selected ONLY after a prior assistant message emitted the literal token 'REQUEST_MATH'.

        # Fallback
        - If greeting/small talk or off-topic → select Fallback.

        - Avoid unnecessary switching; let the same agent continue if still working.
        Return EXACTLY one name.
        """
    # selector_prompt = """
    #     You are the team selector. Roles:
    #         {roles}

    #     Conversation so far:
    #         {history}

    #     Select an agent from: {participants}

    #     Select Rule:
    #     - If latest assistant message includes 'REQUEST_USER_INPUT' → select User.
    #     - If it includes 'REQUEST_MATH' → select Math_Agent.
    #     - If it includes 'REPORTER_READY' → select Reporter.
    #     - Reporter must be selected ONLY when 'REPORTER_READY' is present.
    #     - If the last event was a tool call/summary by Menu_Agent and it has NOT yet posted its two-line output → keep Menu_Agent.
    #     - If the user asks '菜單'/'menu' and no 'REPORTER_READY' yet → select Menu_Agent.
    #     - Otherwise greeting/off-topic → Fallback.
    #     - Return EXACTLY one name.
    #     """

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
        participants=[user_agent, menu_agent, math_agent,fallback_agent, reporter],
        model_client=model_client,
        termination_condition=TextMentionTermination("TERMINATE"),
        selector_prompt=selector_prompt,
        allow_repeated_speaker=True,
        max_turns=12,
        #selector_func= debug_selector_func
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
