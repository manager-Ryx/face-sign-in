"""Compare lock-screen frame strategies on recorded recognition tests.

Replays every recorded five-frame test (the "Test face recognition" button,
or ``tools.bench``) through the progressive strategy and the original fixed
"two matches out of three" vote, and reports how often each would have
signed you in and how many frames it needed.

Limits: tests are taken while you sit in front of the camera, so this
measures how reliably *you* are recognised, not how often someone else
would be accepted. Frames within one test are correlated.

Usage: python -m tools.evaluate_policies
"""
from __future__ import annotations

from face_service.config import METRICS_DIR, Config
from face_service.metrics import MetricsStore
from face_service.recognizer import FrameAnalysis, Outcome
from face_service.verification import Decision, VerifyPolicy, evaluate

CODES = {o.value: o for o in Outcome}
FRAME_MS = 130.0   # measured: ~90 ms analysis + reading the next frame


def replay(outcomes: list[Outcome], policy: VerifyPolicy) -> tuple[bool, int]:
    frames: list[FrameAnalysis] = []
    for index, outcome in enumerate(outcomes):
        frames.append(FrameAnalysis(outcome, captured_at=index * FRAME_MS / 1000.0))
        step = evaluate(frames, policy)
        if step.decision is not Decision.CONTINUE:
            return step.decision is Decision.ACCEPT, len(frames)
    return False, len(frames)


def main() -> None:
    store = MetricsStore(METRICS_DIR / "service.jsonl")
    tests = [e for e in store.events({"check"}, from_disk=True) if len(e.get("outcomes", [])) >= 5]
    if not tests:
        raise SystemExit("No recorded tests yet. Run 'Test face recognition' a few times first.")
    policies = {"progressive (1, then up to 5)": VerifyPolicy.for_unlock(Config()),
                "fixed (2 of 3)": VerifyPolicy.for_unlock(Config(unlock_strategy="fixed"))}
    print(f"{len(tests)} recorded tests\n")
    for name, policy in policies.items():
        accepted, frames = 0, []
        for test in tests:
            outcomes = [CODES.get(code, Outcome.ERROR) for code in test["outcomes"]]
            ok, used = replay(outcomes, policy)
            accepted += ok
            frames.append(used)
        mean = sum(frames) / len(frames)
        print(f"{name:32} signed in {accepted}/{len(tests)} ({accepted / len(tests):.0%}); "
              f"frames used on average {mean:.1f} (~{mean * FRAME_MS:.0f} ms of analysis)")


if __name__ == "__main__":
    main()
