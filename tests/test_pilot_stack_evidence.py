"""Failing-first typed stack-evidence + React full-stack gate tests (audit 3.4, prompt s.8)."""

from __future__ import annotations

from atlas.hunt.stack_evidence import (
    StackVerdict,
    analyze_technology_evidence,
    react_fullstack_verdict,
)


def _verdict(title, desc, mand=(), pref=()):
    ev = analyze_technology_evidence(title, desc, mand, pref)
    return react_fullstack_verdict(ev)[0], ev


def test_python_react_fullstack_rejected():
    v, ev = _verdict("Full Stack Developer", "Python Django backend with React frontend.")
    assert v == StackVerdict.REJECT
    assert "Python" in ev.unsupported_mandatory_backend
    assert not ev.supported_backend


def test_node_react_fullstack_rejected():
    v, ev = _verdict("Full Stack Engineer", "Node.js backend, React frontend, TypeScript, MongoDB.")
    assert v == StackVerdict.REJECT
    assert "Node.js" in ev.unsupported_mandatory_backend


def test_ruby_react_fullstack_rejected():
    v, ev = _verdict("Full Stack Developer", "Ruby on Rails backend and React frontend.")
    assert v == StackVerdict.REJECT
    assert "Ruby" in ev.unsupported_mandatory_backend


def test_pure_react_frontend_accepted():
    v, ev = _verdict("React Developer", "React, ReactJS, TypeScript, HTML, CSS, Redux.")
    assert v == StackVerdict.ACCEPT
    assert not ev.unsupported_mandatory_backend
    assert "react" in ev.frontend


def test_java_react_fullstack_accepted():
    v, ev = _verdict("Java Full Stack Developer", "Java, Spring Boot backend with React frontend.")
    assert v == StackVerdict.ACCEPT
    assert "Java" in ev.supported_backend


def test_dotnet_react_fullstack_accepted():
    v, ev = _verdict("Full Stack Developer", "C#, ASP.NET Core backend with React frontend.")
    assert v == StackVerdict.ACCEPT
    assert ".NET" in ev.supported_backend


def test_api_sql_only_has_no_backend_language():
    ev = analyze_technology_evidence("Backend Engineer", "Build REST APIs, SQL, microservices.")
    assert not ev.supported_backend
    assert not ev.unsupported_mandatory_backend


def test_optional_backend_is_not_mandatory():
    v, ev = _verdict(
        "React Frontend Engineer",
        "Build React UIs. Familiarity with Python is a nice to have.",
    )
    assert v == StackVerdict.ACCEPT
    assert "Python" in ev.unsupported_optional_backend
    assert "Python" not in ev.unsupported_mandatory_backend


def test_mandatory_requirement_list_backend_is_mandatory():
    v, ev = _verdict(
        "Full Stack Developer",
        "Own our product UI and services.",
        mand=("React", "Node.js", "TypeScript"),
    )
    assert v == StackVerdict.REJECT
    assert "Node.js" in ev.unsupported_mandatory_backend
