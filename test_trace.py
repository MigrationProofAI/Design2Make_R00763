"""Verify run_with_trace: full step capture + session log + serper-in-pipeline.
Preview-only (no SAP write). Safe to delete."""
import asyncio
import main
from google.adk.runners import Runner
from google.genai import types
from create_pipeline import build_create_pipeline


async def run():
    agent = build_create_pipeline()
    runner = Runner(app_name=main.APP_NAME, agent=agent,
                    artifact_service=main.artifacts_service,
                    session_service=main.session_service)
    sid = "trace"
    await main.session_service.create_session(
        app_name=main.APP_NAME, user_id=sid, session_id=sid, state={})
    q = ("Find the net weight of material 11056 (AMD Ryzen 7 9800X3D) from the web and "
         "set it in KG. PREVIEW ONLY, do not write.")
    content = types.Content(role="user", parts=[types.Part(text=q)])
    final, trace = await main.run_with_trace(runner, sid, content)
    print("=== FINAL ANSWER ===\n", (final or "")[:400])
    print("\n=== TRACE ===\n", trace)
    await runner.close()


asyncio.run(run())
