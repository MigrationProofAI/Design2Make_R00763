"""Headless test of the create/update pipeline (Intake -> Validate -> Writer).

PREVIEW-ONLY: the requests below do NOT authorise a write, so the Writer must
stop at confirm=false and nothing is committed to SAP. Watch the output to see
each step + every tool call (look for create_material/update_material confirm=false).
Run:  python test_pipeline.py     |   Safe to delete.
"""
import asyncio
import main
from google.adk.runners import Runner
from google.genai import types
from create_pipeline import build_create_pipeline

REQUESTS = [
    # Preview a CREATE (explicitly NOT authorised -> must stop at preview)
    "Prepare to create a raw material: industry sector mechanical engineering, "
    "base unit each, description 'Pipeline Test Gasket 01'. PREVIEW ONLY, do not write.",
    # Preview an UPDATE of an existing material
    "Change the description of material TG10 to 'Trad.Good 10 - pipeline test'. "
    "Preview only, do not write yet.",
]


async def run():
    agent = build_create_pipeline()
    runner = Runner(app_name=main.APP_NAME, agent=agent,
                    artifact_service=main.artifacts_service,
                    session_service=main.session_service)
    sid = "pipe"
    await main.session_service.create_session(
        app_name=main.APP_NAME, user_id=sid, session_id=sid, state={})

    for q in REQUESTS:
        print("\n" + "=" * 72 + f"\n>>> USER: {q}\n" + "=" * 72)
        content = types.Content(role="user", parts=[types.Part(text=q)])
        async for ev in runner.run_async(session_id=sid, user_id=sid, new_message=content):
            who = ev.author or (ev.content.role if ev.content else "?")
            for p in ((ev.content.parts if ev.content else None) or []):
                if p.text:
                    print(f"[{who}] {p.text.strip()[:600]}")
                fc = getattr(p, "function_call", None)
                if fc:
                    print(f"  >>{who} CALLS {fc.name}({dict(fc.args)})")
                fr = getattr(p, "function_response", None)
                if fr:
                    print(f"  >>{fr.name} -> {str(fr.response)[:220]}")
    await runner.close()


asyncio.run(run())
