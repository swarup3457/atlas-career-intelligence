"""Phase 1C-A FINAL stabilization — token/phrase-aware query matching, per-hop
redirect revalidation, and truthful final-status persistence (build spec 12/13/15).
"""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.models import ErrorCategory
from atlas.orchestration.production_state import ProductionTerminalState
from atlas.planning.query_compiler import CompiledQuery, SearchQueryCompiler
from atlas.policy.loader import load_policy
from atlas.runtime.production import ProductionSearchRuntime
from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient, _Headers, _RawResponse

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# TEST L — token/phrase-aware lane matching (Java != JavaScript)
# ---------------------------------------------------------------------------
def _cq(positive, negative=()):
    return CompiledQuery(lane_key="L", geography_group="PRIMARY", mode="DELTA", query_terms=tuple(positive),
                         negative_terms=tuple(negative), positive_signals=tuple(positive), geography_terms=())


def test_java_does_not_match_javascript():
    q = _cq(["java"])
    assert q.is_relevant("Java Backend Developer")
    assert not q.is_relevant("JavaScript Frontend Engineer")
    assert not q.is_relevant("Senior JavaScript Developer")


def test_react_does_not_match_reactive():
    q = _cq(["react"])
    assert q.is_relevant("React Developer")
    assert not q.is_relevant("Reactive Systems Engineer")


def test_dotnet_and_csharp_match_despite_punctuation():
    q = _cq([".net", "c#"])
    assert q.is_relevant("C# / .NET Developer")
    assert q.is_relevant(".NET Backend Engineer")
    assert not q.is_relevant("Node.js Developer")     # not a .net/c# token


def test_negative_terms_use_token_semantics():
    # A negative "senior" rejects "Senior Java" but NOT "Java Seniority" (token,
    # not substring), which still matches the positive java signal.
    q = _cq(["java"], negative=["senior"])
    assert not q.is_relevant("Senior Java Engineer")
    assert q.is_relevant("Java Seniority Framework Developer")


def test_policy_java_lane_is_token_aware():
    b = load_policy()
    c = SearchQueryCompiler(b.lanes, b.geography, policy_version="pol")
    jb = c.compile("JAVA_BACKEND", "PRIMARY")
    assert jb.is_relevant("Java Developer")
    assert not jb.is_relevant("JavaScript Developer")   # the corrective invariant


# ---------------------------------------------------------------------------
# TEST M — same-origin 307/308 to a non-CXS path is rejected
# ---------------------------------------------------------------------------
class _Scripted(ReadOnlyHttpClient):
    def __init__(self, script, **kw):
        super().__init__(**kw)
        self._script = list(script)
        self.calls = []

    def _open_raw(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "body": body})
        return self._script.pop(0)


def _raw(status, *, headers=None, body=b"", url="https://x.local/"):
    return _RawResponse(status, _Headers((headers or {}).items()), body, url)


_CXS = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"


def test_same_origin_307_to_non_cxs_path_is_rejected():
    # A same-origin 307 that would PRESERVE the Workday CXS POST to a non-CXS path
    # must be refused — the body is never posted to an unintended endpoint.
    client = _Scripted([_raw(307, headers={"Location": "https://acme.wd5.myworkdayjobs.com/en-US/External/login"},
                             url=_CXS)])
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest(_CXS, method="POST", body=b'{"appliedFacets":{}}'))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE
    # The POST was never re-issued to the non-CXS target.
    assert all(c["method"] == "GET" or c["url"] == _CXS for c in client.calls)


def test_same_origin_308_to_another_cxs_path_is_allowed():
    # A same-origin 308 to ANOTHER valid CXS endpoint may preserve the POST.
    other_cxs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External2/jobs"
    client = _Scripted([_raw(308, headers={"Location": other_cxs}, url=_CXS),
                        _raw(200, body=b"{}", url=other_cxs)])
    resp = client.fetch(HttpRequest(_CXS, method="POST", body=b"{}"))
    assert resp.status == 200
    assert client.calls[1]["method"] == "POST" and client.calls[1]["body"] == b"{}"


# ---------------------------------------------------------------------------
# TEST P — a final-status persistence failure is NOT a silent COMPLETE
# ---------------------------------------------------------------------------
def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "s.sqlite", checkpoint_db=tmp_path / "state" / "c.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def test_final_status_persistence_failure_is_not_complete(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runP", fixture_mode=True,
                                 simulate_final_status_persist_failure=True)
    res = rt.run()
    # The pipeline finished, but the final run status could NOT be persisted:
    # the result truthfully reports this and does NOT claim a durable COMPLETE.
    assert res.final_status_persisted is False
    assert res.terminal_state == ProductionTerminalState.PARTIAL.value   # downgraded from COMPLETE
    assert res.manifest.get("final_status_persisted") is False


def test_final_status_persists_normally_when_healthy(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runP2", fixture_mode=True)
    res = rt.run()
    assert res.final_status_persisted is True
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
