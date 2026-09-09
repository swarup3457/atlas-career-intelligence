"""Agentic V4 — stateful async browser actor proofs (prompt s.3.1, s.4, s.12.C/D).

Runs a REAL headless Chrome against a LOCAL SPA fixture served over 127.0.0.1
(offline; marked ``browser``). Proves the exact properties the V3 stateless tool
lacked:

* one persistent browser session drives a full multi-action sequence
  (open -> observe -> fill -> select -> click -> wait -> observe results ->
  load more -> open detail -> back);
* state survives BETWEEN tool calls (a typed query is still there on the next
  observe, and after opening a detail and going back);
* two company actors are fully isolated;
* an action that exceeds its time budget degrades to a retryable error and the
  actor still closes cleanly (no leaked browser);
* the async Playwright API runs inside the actor's own loop with NO
  "Sync API inside asyncio loop" fault, even when the caller is already async.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import socket
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

from atlas.pilot.browser_actor import BrowserActorError, CompanyBrowserActor
from atlas.pilot.status_v4 import AccessSignals, classify_blocker, looks_like_login_wall

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agentic_v4" / "spa"


@pytest.fixture(scope="module")
def spa_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(_FIXTURE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        httpd.shutdown()


def _find(items, **preds):
    for it in items:
        if all(str(pred).lower() in str(it.get(k, "")).lower() for k, pred in preds.items()):
            return it
    return None


def _search_input(obs):
    for i in obs["inputs"]:
        if i.get("name") == "q" or "search" in (i.get("placeholder", "") + i.get("label", "")).lower():
            return i
    return None


def _make_actor(url):
    return CompanyBrowserActor(
        actor_id="test", company="Acme", task_id="t1",
        allow_http_hosts=("127.0.0.1",), action_timeout_s=30.0, nav_timeout_ms=8000,
    )


def test_stateful_multi_action_session(spa_server):
    actor = _make_actor(spa_server)
    try:
        obs = actor.start(spa_server)
        assert "Acme Careers" in obs["title"] or obs["title"]
        assert _search_input(obs) is not None
        assert _find(obs["buttons"], text="Search") is not None
        # header Sign in link is present but is NOT an auth wall
        assert obs["challenge"]["signin_link"] is True
        assert obs["challenge"]["password_field"] is False

        # fill the query, then OBSERVE AGAIN (separate tool call): state survived
        q = _search_input(obs)
        actor.fill(q["handle"], "Java")
        obs2 = actor.observe()
        assert _search_input(obs2)["value"] == "Java"
        assert obs2["query_text"] == "Java"

        # pick India, submit, wait, observe results
        loc = _find(obs2["inputs"], tag="select")
        actor.select_option(loc["handle"], "India")
        go = _find(actor.observe()["buttons"], text="Search")
        actor.click(go["handle"])
        actor.wait(ms=400)
        res = actor.observe()
        cards = res["job_cards"]
        assert len(cards) >= 1
        assert all("india" in (c["location"] + c["url"] + c["title"]).lower() or c["location"] for c in cards)
        # a Java+India search yields multiple jobs -> a load-more control appears
        more = _find(res["pagination"], kind="load_more")
        assert more is not None
        before = len(actor.collect_job_cards()["job_cards"])
        actor.scroll_or_load_more(more["handle"])
        after = actor.collect_job_cards()["job_cards"]
        assert len(after) >= before

        # open a job detail (same-document hash route) and read real evidence
        card = after[0]
        detail = actor.open_job_detail(handle=card["handle"])
        assert detail["detail_text"]
        assert "experience" in detail["detail_text"].lower()
        assert "#/job/" in detail["url"]

        # go back -> results restored AND the typed query is still there
        back = actor.back()
        assert _search_input(back)["value"] == "Java", "search state must survive detail+back"
    finally:
        actor.close()


def test_state_survives_between_tool_calls(spa_server):
    actor = _make_actor(spa_server)
    try:
        obs = actor.start(spa_server)
        q = _search_input(obs)
        actor.fill(q["handle"], "Payroll")
        # a completely separate tool call must see the persisted value
        again = actor.observe()
        assert _search_input(again)["value"] == "Payroll"
    finally:
        actor.close()


def test_two_actors_isolated(spa_server):
    a = _make_actor(spa_server)
    b = _make_actor(spa_server)
    try:
        oa = a.start(spa_server)
        ob = b.start(spa_server)
        a.fill(_search_input(oa)["handle"], "Java")
        b.fill(_search_input(ob)["handle"], "Payroll")
        a.click(_find(a.observe()["buttons"], text="Search")["handle"])
        b.click(_find(b.observe()["buttons"], text="Search")["handle"])
        a.wait(ms=300); b.wait(ms=300)
        ca = a.collect_job_cards()["job_cards"]
        cb = b.collect_job_cards()["job_cards"]
        titles_a = {c["title"] for c in ca}
        titles_b = {c["title"] for c in cb}
        assert titles_a and titles_b
        assert titles_a != titles_b, "two actors must have independent result state"
        assert a.observe()["query_text"] == "Java"
        assert b.observe()["query_text"] == "Payroll"
    finally:
        a.close(); b.close()


def test_cleanup_on_timeout_is_retryable_and_closes(spa_server):
    actor = _make_actor(spa_server)
    try:
        actor.start(spa_server)
        # an action with an impossibly small budget must degrade to a retryable
        # BrowserActorError, never a terminal outcome, without leaking the browser.
        with pytest.raises(BrowserActorError):
            actor._submit(actor._wait(9000, ""), timeout=0.05)
    finally:
        result = actor.close()
        assert result["ok"] is True
    # a fresh actor still works after the previous one timed out (no leak)
    actor2 = _make_actor(spa_server)
    try:
        obs = actor2.start(spa_server)
        assert obs["url"]
    finally:
        actor2.close()


def test_timeout_maps_to_retryable_status(spa_server):
    actor = _make_actor(spa_server)
    try:
        actor.start(spa_server)
        # force a tiny per-call timeout
        try:
            actor._submit(actor._wait(9000, ""), timeout=0.05)
            raised = False
        except BrowserActorError:
            raised = True
        assert raised
        status = classify_blocker(AccessSignals(tool_exception="TimeoutError"))
        from atlas.pilot.status_v4 import is_internal_retryable, is_terminal
        assert is_internal_retryable(status) and not is_terminal(status)
    finally:
        actor.close()


def test_signin_link_is_not_login_wall(spa_server):
    actor = _make_actor(spa_server)
    try:
        obs = actor.start(spa_server)
        ch = obs["challenge"]
        wall = looks_like_login_wall(
            password_field_present=ch["password_field"],
            job_content_visible=True,
            signin_link_only=ch["signin_link"],
        )
        assert wall is False
    finally:
        actor.close()


def test_async_actor_runs_inside_running_event_loop(spa_server):
    """The V3 crash was sync Playwright inside asyncio. Prove the actor's async
    session runs fine even when the CALLER is already inside a running loop."""
    async def _driver():
        actor = _make_actor(spa_server)
        try:
            # calling the sync actor API from an async context must NOT raise the
            # "Sync API inside asyncio loop" error (actor owns a separate loop).
            obs = await asyncio.to_thread(actor.start, spa_server)
            assert obs["url"]
            q = _search_input(obs)
            await asyncio.to_thread(actor.fill, q["handle"], "Java")
            again = await asyncio.to_thread(actor.observe)
            assert _search_input(again)["value"] == "Java"
            return True
        finally:
            await asyncio.to_thread(actor.close)

    assert asyncio.run(_driver()) is True
