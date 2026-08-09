import threading
import time

from eirven_ai.llm_arbiter import LLMArbiter


def test_interactive_request_preempts_background_lease():
    arbiter = LLMArbiter()
    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def background():
        with arbiter.acquire('background') as lease:
            seen['lease'] = lease
            entered.set()
            release.wait(2)

    bg = threading.Thread(target=background)
    bg.start()
    assert entered.wait(1)

    foreground_done = threading.Event()
    def foreground():
        with arbiter.acquire('interactive'):
            foreground_done.set()

    fg = threading.Thread(target=foreground)
    fg.start()
    deadline = time.time() + 1
    while time.time() < deadline and not seen['lease'].preempt.is_set():
        time.sleep(0.01)
    assert seen['lease'].preempt.is_set()
    release.set()
    assert foreground_done.wait(1)
    bg.join(1); fg.join(1)
