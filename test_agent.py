"""Headless end-to-end test of the agent (no browser). Shows tool calls.
Exercises create_agent() exactly as the app does — all 3 MCP servers + GPT-4o.
Safe to delete.  Run:  python test_agent.py
"""
import asyncio
import main
from google.adk.runners import Runner
from google.genai import types

QUERIES = ["find TG10", "show me anything with pump in it"]


async def run():
    agent = main.create_agent()
    runner = Runner(
        app_name=main.APP_NAME,
        agent=agent,
        artifact_service=main.artifacts_service,
        session_service=main.session_service,
    )
    sid = "diag"
    await main.session_service.create_session(
        app_name=main.APP_NAME, user_id=sid, session_id=sid, state={})

    for q in QUERIES:
        print("\n" + "=" * 70 + f"\n>>> USER: {q}\n" + "=" * 70)
        content = types.Content(role="user", parts=[types.Part(text=q)])
        async for event in runner.run_async(session_id=sid, user_id=sid, new_message=content):
            ec = event.content
            if not ec:
                continue
            for p in (ec.parts or []):
                if p.text:
                    print(f"[{ec.role}] {p.text.strip()[:400]}")
                fc = getattr(p, "function_call", None)
                if fc:
                    print(f"  ⮡ TOOL CALL: {fc.name}({dict(fc.args)})")
                fr = getattr(p, "function_response", None)
                if fr:
                    print(f"  ⮡ TOOL RESULT [{fr.name}]: {str(fr.response)[:220]}")
    await runner.close()


asyncio.run(run())
