"""Headless integration test of the ROUTER + specialists, mirroring main.py's
dispatch. Read-only prompts only (search / ontology / validate) so NOTHING is
written. Run: python test_router.py    |   Safe to delete.
"""
import asyncio
import main
from google.adk.runners import Runner
from google.genai import types

PROMPTS = [
    "find a gaming processor",                              # -> search (semantic / vector)
    "what does a FERT need to be created?",                 # -> search (ontology / graph)
    "validate this proposed material: type FERT, description 'Brk', "
    "net weight 500, gross weight 400",                     # -> validate (rule engine)
]


async def run():
    builders = {"search": main.build_search_agent,
                "create_change": main.build_create_pipeline,
                "validate": main.build_validation_pipeline}
    runners: dict = {}

    def get_runner(intent):
        if intent not in runners:
            runners[intent] = Runner(app_name=main.APP_NAME, agent=builders[intent](),
                                     artifact_service=main.artifacts_service,
                                     session_service=main.session_service)
        return runners[intent]

    sid = "rtest"
    await main.session_service.create_session(
        app_name=main.APP_NAME, user_id=sid, session_id=sid, state={})

    for p in PROMPTS:
        intent = main.classify_intent(p, False)
        print("\n" + "=" * 72 + f"\nUSER: {p}\n  ROUTER -> {intent}\n" + "=" * 72)
        content = types.Content(role="user", parts=[types.Part(text=p)])
        runner = get_runner(intent)
        if intent == "validate":
            out = await main.run_rule_engine(runner, main.session_service,
                                             main.APP_NAME, sid, content)
        else:
            final, _ = await main.run_with_trace(runner, sid, content)
            out = final
        # strip emoji so the Windows console doesn't choke
        print((out or "(no answer)").encode("ascii", "ignore").decode()[:700])

    for r in runners.values():
        await r.close()


asyncio.run(run())
