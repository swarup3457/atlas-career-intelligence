"""Stateful, asynchronous, read-only career-browser actor (V4 architecture s.4, prompt s.3.1/s.4).

The V3 ``bounded_browser_action`` opened a NEW browser/context/page for every
single action and closed it immediately, so search text, filters, cookies,
navigation history and loaded results never survived between tool calls — and it
ran the *sync* Playwright API inside the SDK's asyncio loop, which crashes with
``Playwright Sync API inside the asyncio loop``.

:class:`CompanyBrowserActor` fixes both problems (prompt s.4, design B): ONE
browser/context/page is opened per company and retained for the whole task, and
the async Playwright API runs inside a DEDICATED actor thread that owns its own
event loop. The SDK's loop is never touched, so there is no sync-in-async fault,
and two companies get two fully isolated browsers.

Every action references a stable HANDLE minted by the latest
:meth:`observe` — the model can never invent a raw CSS selector or JavaScript.
Everything is read-only: navigate / observe / type a query / pick a location /
submit / paginate / scroll / open an official detail / go back. It never logs
in, fills credentials, clicks Apply, or bypasses a challenge; a detected login
wall is reported truthfully.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

__all__ = ["CompanyBrowserActor", "BrowserActorError", "Observation", "actor_registrable_domain",
           "is_hard_navigation_block"]

_DEFAULT_NAV_TIMEOUT_MS = 15000
_DEFAULT_ACTION_TIMEOUT_S = 30.0
_MAX_CARDS = 60
_MAX_LINKS = 80
_MAX_INPUTS = 40
_MAX_JSON_ENDPOINTS = 25

_TRUSTED_ATS_SUFFIXES = (
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com", "avature.net",
    "icims.com", "successfactors.com", "successfactors.eu", "taleo.net", "oraclecloud.com",
    "eightfold.ai", "smartrecruiters.com", "workday.com", "brassring.com", "jobvite.com",
)


class BrowserActorError(RuntimeError):
    """A browser actor fault (retryable). NEVER a terminal company outcome."""


# Substrings in a Playwright error that indicate the SERVER refused the browser
# (a real access block), distinct from a soft interaction timeout. Used to
# classify a browser-confirmed external block vs a retryable tool hiccup.
_HARD_NAV_MARKERS = (
    "err_http_response_code_failure", "err_too_many_redirects", "err_connection_refused",
    "err_connection_reset", "err_name_not_resolved", "err_cert", "err_ssl", "err_aborted",
    "net::err", "status code 403", "status code 401", "http 403", "http 401",
)


def is_hard_navigation_block(message: object) -> bool:
    low = str(message or "").lower()
    return any(m in low for m in _HARD_NAV_MARKERS)


def actor_registrable_domain(host: str) -> str:
    labels = (host or "").lower().split(".")
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host or ""


def _host(url: str) -> str:
    return (urlsplit(url or "").netloc or "").split("@")[-1].split(":")[0].lower().rstrip(".")


def _same_document(a: str, b: str) -> bool:
    """True when two URLs differ only by fragment (a same-document hash route)."""
    pa, pb = urlsplit(a or ""), urlsplit(b or "")
    return (pa.scheme, pa.netloc, pa.path, pa.query) == (pb.scheme, pb.netloc, pb.path, pb.query)


@dataclass
class Observation:
    version: int
    url: str
    title: str
    headings: tuple[str, ...] = ()
    inputs: tuple[dict, ...] = ()
    buttons: tuple[dict, ...] = ()
    links: tuple[dict, ...] = ()
    job_cards: tuple[dict, ...] = ()
    pagination: tuple[dict, ...] = ()
    challenge: dict = field(default_factory=dict)
    query_text: str = ""
    selected_location: str = ""
    json_endpoints: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "version": self.version, "url": self.url, "title": self.title,
            "headings": list(self.headings), "inputs": list(self.inputs),
            "buttons": list(self.buttons), "links": list(self.links),
            "job_cards": list(self.job_cards), "pagination": list(self.pagination),
            "challenge": dict(self.challenge), "query_text": self.query_text,
            "selected_location": self.selected_location,
            "json_endpoints": list(self.json_endpoints),
        }


# JavaScript that (1) clears prior handles, (2) tags interactive elements and
# likely job cards with data-atlas-handle, and (3) returns a bounded structured
# observation. Read-only DOM inspection only.
_OBSERVE_JS = r"""
() => {
  const MAX_CARDS = %d, MAX_LINKS = %d, MAX_INPUTS = %d;
  document.querySelectorAll('[data-atlas-handle]').forEach(e => e.removeAttribute('data-atlas-handle'));
  let n = 0;
  const tag = (el) => { const h = 'h' + (n++); el.setAttribute('data-atlas-handle', h); return h; };
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return s && s.visibility !== 'hidden' && s.display !== 'none' && (r.width + r.height) > 0;
  };
  const txt = (el) => (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();

  const jobRe = /(job|career|requisition|posting|vacanc|opening|position|req[-_]?\d)/i;
  const locRe = /(india|bengaluru|bangalore|hyderabad|pune|chennai|noida|gurugram|gurgaon|mumbai|delhi|remote|,\s*[A-Z]{2}\b|United States|USA|London|Singapore)/i;

  const headings = [];
  document.querySelectorAll('h1,h2,h3').forEach(h => { if (vis(h) && headings.length < 20) { const t = txt(h); if (t) headings.push(t.slice(0,140)); } });

  const inputs = [];
  document.querySelectorAll('input,textarea,select').forEach(el => {
    if (inputs.length >= MAX_INPUTS || !vis(el)) return;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (type === 'hidden') return;
    let label = '';
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = txt(l); }
    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
    const h = tag(el);
    inputs.push({handle: h, tag: el.tagName.toLowerCase(), type,
      label: (label||'').slice(0,80), placeholder: (el.getAttribute('placeholder')||'').slice(0,80),
      name: (el.getAttribute('name')||'').slice(0,60), role: (el.getAttribute('role')||''),
      value: (el.value||'').slice(0,120)});
  });

  const buttons = [];
  document.querySelectorAll("button,[role=button],input[type=submit],input[type=button]").forEach(el => {
    if (buttons.length >= 40 || !vis(el)) return;
    const t = txt(el) || el.value || el.getAttribute('aria-label') || '';
    const h = tag(el);
    buttons.push({handle: h, text: (t||'').slice(0,80), role: (el.getAttribute('role')||'button')});
  });

  const pagination = [];
  const links = [];
  const seenHref = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    if (!vis(a)) return;
    const t = txt(a); const href = a.href || '';
    const low = (t + ' ' + href).toLowerCase();
    if (/(next|load more|show more|see more|more results|page \d)/i.test(t) && pagination.length < 12) {
      const h = tag(a);
      pagination.push({handle: h, text: t.slice(0,60), kind: /load more|show more|see more/i.test(t) ? 'load_more' : 'next'});
      return;
    }
    if (links.length < MAX_LINKS && href.startsWith('http')) {
      if (seenHref.has(href)) return; seenHref.add(href);
      const h = tag(a);
      links.push({handle: h, text: t.slice(0,100), href});
    }
  });
  document.querySelectorAll("button,[role=button]").forEach(b => {
    if (pagination.length >= 12 || !vis(b)) return;
    const t = txt(b) || b.value || '';
    if (/load more|show more|see more|more results/i.test(t)) {
      const h = b.getAttribute('data-atlas-handle') || tag(b);
      pagination.push({handle: h, text: t.slice(0,60), kind: 'load_more'});
    }
  });

  // Job cards: an anchor that looks job-like, read from its CONTAINING card so a
  // sibling location is captured even when the anchor text has no city.
  const cards = [];
  const seenCard = new Set();
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  for (const a of anchors) {
    if (cards.length >= MAX_CARDS) break;
    const href = a.href || ''; const atext = txt(a);
    if (!href.startsWith('http')) continue;
    if (!(jobRe.test(href) || jobRe.test(atext))) continue;
    if (!atext || atext.length < 3) continue;
    if (seenCard.has(href)) continue; seenCard.add(href);
    let card = a;
    for (let up = 0; up < 4 && card.parentElement; up++) {
      const p = card.parentElement;
      const cls = (p.className && p.className.toString ? p.className.toString() : '') + ' ' + (p.getAttribute && p.getAttribute('role') || '');
      if (/job|card|result|position|opening|listing|posting|vacanc|tile|row/i.test(cls) || p.tagName === 'LI' || p.tagName === 'TR' || p.tagName === 'ARTICLE') { card = p; break; }
      card = p;
    }
    let loc = '';
    const locEl = card.querySelector('[class*=location i],[data-testid*=location i],[aria-label*=location i]');
    if (locEl) loc = txt(locEl);
    if (!loc) { const m = txt(card).match(locRe); if (m) loc = m[0]; }
    const h = a.getAttribute('data-atlas-handle') || tag(a);
    cards.push({handle: h, title: atext.slice(0,160), location: (loc||'').slice(0,120), url: href});
  }

  // Challenge / login-wall evidence (a header "Sign in" link is NOT a wall).
  const pw = document.querySelector('input[type=password]');
  const bodyTxt = (document.body ? txt(document.body) : '').toLowerCase().slice(0, 4000);
  const signinLink = Array.from(document.querySelectorAll('a,button')).some(e => /sign in|log ?in/i.test(txt(e)) && txt(e).length < 24);
  const captcha = /captcha|are you a robot|verify you are human|unusual traffic|cf-challenge|px-captcha|incapsula/i.test(bodyTxt);
  const challenge = {password_field: !!(pw && vis(pw)), signin_link: !!signinLink, captcha: !!captcha,
    login_headline: /please (sign in|log ?in)|sign in to continue|login required/i.test(bodyTxt)};

  // Current query / location echo (best-effort from the first search-ish input).
  let query_text = '', selected_location = '';
  for (const i of inputs) {
    const hint = (i.placeholder + ' ' + i.label + ' ' + i.name).toLowerCase();
    if (!query_text && /search|keyword|title|role|what/.test(hint) && i.value) query_text = i.value;
    if (!selected_location && /location|where|city/.test(hint) && i.value) selected_location = i.value;
  }

  return {url: location.href, title: document.title, headings, inputs, buttons, links, pagination, job_cards: cards,
    challenge, query_text, selected_location};
}
""" % (_MAX_CARDS, _MAX_LINKS, _MAX_INPUTS)


class CompanyBrowserActor:
    """One persistent async browser session for ONE company, driven by a private
    actor thread + event loop. Public methods are synchronous and thread-safe."""

    def __init__(
        self,
        *,
        actor_id: str,
        company: str = "",
        task_id: str = "",
        headless: bool = True,
        trusted_hosts: tuple[str, ...] = (),
        allow_http_hosts: tuple[str, ...] = (),
        action_budget: int = 60,
        action_timeout_s: float = _DEFAULT_ACTION_TIMEOUT_S,
        nav_timeout_ms: int = _DEFAULT_NAV_TIMEOUT_MS,
        click_timeout_ms: int = 6000,
    ) -> None:
        self.actor_id = actor_id
        self.company = company
        self.task_id = task_id
        self.headless = headless
        self.action_budget = action_budget
        self.action_timeout_s = action_timeout_s
        self.nav_timeout_ms = nav_timeout_ms
        # Interaction (click/fill/select/press) timeout is deliberately SHORTER
        # than the navigation timeout: a fragile control that does not respond in
        # a few seconds should fail FAST and let the agent recover (Enter / URL /
        # next lane) instead of burning ~15s per stuck click (pass-2 lesson).
        self.click_timeout_ms = click_timeout_ms
        self.trusted_hosts = tuple(h.lower() for h in trusted_hosts)
        # Test-only seam: hosts (e.g. 127.0.0.1) allowed over plain HTTP for
        # offline fixture tests. Empty in production, so real runs stay HTTPS-only.
        self.allow_http_hosts = tuple(h.lower() for h in allow_http_hosts)

        self.action_count = 0
        self.observation_version = 0
        self.redirect_chain: list[str] = []
        self.query_history: list[dict] = []
        self.diagnostics: list[str] = []
        self._json_endpoints: list[str] = []
        self._entry_domain = ""
        self._closed = False

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name=f"browser-actor-{actor_id}", daemon=True)
        self._thread.start()

    # -- infra ---------------------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, *, timeout: Optional[float] = None):
        if self._closed:
            raise BrowserActorError("actor is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout if timeout is not None else self.action_timeout_s)
        except Exception as exc:  # noqa: BLE001
            fut.cancel()
            name = type(exc).__name__
            self.diagnostics.append(f"{name}: {str(exc)[:160]}")
            raise BrowserActorError(f"{name}: {exc}") from exc

    def _trusted(self, url: str) -> bool:
        h = _host(url)
        if not h:
            return False
        if h in self.allow_http_hosts:
            return True
        if h.endswith(".myworkdayjobs.com") or h == "myworkdayjobs.com":
            return True
        for suf in _TRUSTED_ATS_SUFFIXES:
            if h == suf or h.endswith("." + suf):
                return True
        if self._entry_domain and (h == self._entry_domain or h.endswith("." + self._entry_domain)
                                   or actor_registrable_domain(h) == actor_registrable_domain(self._entry_domain)):
            return True
        for th in self.trusted_hosts:
            if h == th or h.endswith("." + th):
                return True
        return False

    # -- tool 1: start -------------------------------------------------------
    def start(self, url: str) -> dict:
        host = _host(url)
        if not url.lower().startswith("https://") and host not in self.allow_http_hosts:
            raise BrowserActorError(f"refused non-HTTPS start URL: {url}")
        self._entry_domain = host
        self.action_count += 1
        obs = self._submit(self._start(url), timeout=max(self.action_timeout_s, self.nav_timeout_ms / 1000 + 10))
        return obs.to_dict()

    async def _start(self, url: str) -> Observation:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launched = None
        for kw in ({"channel": "chrome", "headless": self.headless}, {"headless": self.headless}):
            try:
                launched = await self._pw.chromium.launch(**kw)
                break
            except Exception:  # noqa: BLE001
                continue
        if launched is None:
            raise BrowserActorError("no launchable Chromium/Chrome for read-only browsing")
        self._browser = launched
        self._context = await self._browser.new_context(
            user_agent="Atlas-Career-Intelligence/1.0 (personal job-search research; read-only)",
            viewport={"width": 1280, "height": 900},
            java_script_enabled=True,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.nav_timeout_ms)
        self._page.on("response", self._on_response)
        self._page.on("framenavigated", self._on_nav)
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        return await self._observe()

    def _on_response(self, response) -> None:
        try:
            req = response.request
            if req.method != "GET":
                return
            ct = (response.headers or {}).get("content-type", "")
            if "json" not in ct.lower():
                return
            u = response.url
            if not u.startswith("https://"):
                return
            if _host(u) and self._entry_domain and actor_registrable_domain(_host(u)) != actor_registrable_domain(self._entry_domain) and not self._trusted(u):
                return
            if u not in self._json_endpoints and len(self._json_endpoints) < _MAX_JSON_ENDPOINTS:
                self._json_endpoints.append(u)
        except Exception:  # noqa: BLE001
            pass

    def _on_nav(self, frame) -> None:
        try:
            if frame is self._page.main_frame:
                if not self.redirect_chain or self.redirect_chain[-1] != frame.url:
                    self.redirect_chain.append(frame.url)
        except Exception:  # noqa: BLE001
            pass

    # -- tool 2: observe -----------------------------------------------------
    def observe(self) -> dict:
        self.action_count += 1
        return self._submit(self._observe()).to_dict()

    async def _observe(self) -> Observation:
        if self._page is None:
            raise BrowserActorError("no page; call start() first")
        data = await self._page.evaluate(_OBSERVE_JS)
        self.observation_version += 1
        obs = Observation(
            version=self.observation_version,
            url=data.get("url", ""), title=data.get("title", ""),
            headings=tuple(data.get("headings", [])),
            inputs=tuple(data.get("inputs", [])), buttons=tuple(data.get("buttons", [])),
            links=tuple(data.get("links", [])), job_cards=tuple(data.get("job_cards", [])),
            pagination=tuple(data.get("pagination", [])),
            challenge=dict(data.get("challenge", {})),
            query_text=data.get("query_text", ""), selected_location=data.get("selected_location", ""),
            json_endpoints=tuple(self._json_endpoints),
        )
        self._last_cards = {c["handle"]: c for c in obs.job_cards}
        return obs

    def _locator(self, handle: str):
        if not handle or not str(handle).startswith("h"):
            raise BrowserActorError(f"invalid handle {handle!r}; reference a handle from the latest observation")
        return self._page.locator(f'[data-atlas-handle="{handle}"]').first

    # -- tool 3: fill --------------------------------------------------------
    def fill(self, handle: str, value: str) -> dict:
        self.action_count += 1
        return self._submit(self._fill(handle, value),
                            timeout=min(self.action_timeout_s, self.click_timeout_ms / 1000 + 3))

    async def _fill(self, handle: str, value: str) -> dict:
        loc = self._locator(handle)
        await loc.fill(str(value), timeout=self.click_timeout_ms)
        return {"ok": True, "handle": handle, "value": value}

    # -- tool 4: click -------------------------------------------------------
    def click(self, handle: str, *, timeout_ms: Optional[int] = None) -> dict:
        self.action_count += 1
        eff = timeout_ms if timeout_ms is not None else self.click_timeout_ms
        return self._submit(self._click(handle, eff),
                            timeout=min(self.action_timeout_s, eff / 1000 + 3))

    async def _click(self, handle: str, timeout_ms: Optional[int] = None) -> dict:
        loc = self._locator(handle)
        await loc.click(timeout=timeout_ms if timeout_ms is not None else self.click_timeout_ms)
        return {"ok": True, "handle": handle}

    # -- direct search-URL navigation (reliable for query-param SPAs) --------
    def goto_search(self, url: str) -> dict:
        """Navigate the persistent page to a trusted search-results URL (e.g. a
        careers site's own ?q=&location= URL). Read-only; a hard server refusal
        surfaces as a BrowserActorError classified as a navigation block."""
        self.action_count += 1
        return self._submit(self._goto_search(url),
                            timeout=max(self.action_timeout_s, self.nav_timeout_ms / 1000 + 8)).to_dict()

    async def _goto_search(self, url: str) -> Observation:
        if not self._trusted(url):
            raise BrowserActorError(f"refused untrusted search URL: {url}")
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        await self._page.wait_for_timeout(600)
        return await self._observe()

    # -- tool 5: press -------------------------------------------------------
    def press(self, key: str, handle: str = "") -> dict:
        self.action_count += 1
        return self._submit(self._press(key, handle),
                            timeout=min(self.action_timeout_s, self.click_timeout_ms / 1000 + 3))

    async def _press(self, key: str, handle: str) -> dict:
        if handle:
            await self._locator(handle).press(key, timeout=self.click_timeout_ms)
        else:
            await self._page.keyboard.press(key)
        return {"ok": True, "key": key}

    # -- tool 6: select_option ----------------------------------------------
    def select_option(self, handle: str, value: str) -> dict:
        self.action_count += 1
        return self._submit(self._select(handle, value),
                            timeout=min(self.action_timeout_s, self.click_timeout_ms / 1000 + 3))

    async def _select(self, handle: str, value: str) -> dict:
        loc = self._locator(handle)
        try:
            await loc.select_option(value=str(value), timeout=self.click_timeout_ms)
        except Exception:  # noqa: BLE001 - fall back to label match
            await loc.select_option(label=str(value), timeout=self.click_timeout_ms)
        return {"ok": True, "handle": handle, "value": value}

    # -- tool 7: wait --------------------------------------------------------
    def wait(self, *, ms: Optional[int] = None, state: str = "") -> dict:
        self.action_count += 1
        return self._submit(self._wait(ms, state), timeout=self.action_timeout_s + (ms or 0) / 1000)

    async def _wait(self, ms: Optional[int], state: str) -> dict:
        if state in ("load", "domcontentloaded", "networkidle"):
            await self._page.wait_for_load_state(state, timeout=self.nav_timeout_ms)
        else:
            await self._page.wait_for_timeout(int(ms or 800))
        return {"ok": True}

    # -- tool 8: scroll / load more -----------------------------------------
    def scroll_or_load_more(self, handle: str = "") -> dict:
        self.action_count += 1
        return self._submit(self._scroll_or_load_more(handle))

    async def _scroll_or_load_more(self, handle: str) -> dict:
        if handle:
            try:
                await self._locator(handle).click(timeout=self.nav_timeout_ms)
                await self._page.wait_for_timeout(600)
                return {"ok": True, "action": "load_more", "handle": handle}
            except Exception as exc:  # noqa: BLE001
                self.diagnostics.append(f"load_more failed: {type(exc).__name__}")
        await self._page.mouse.wheel(0, 3000)
        await self._page.wait_for_timeout(500)
        return {"ok": True, "action": "scroll"}

    # -- tool 9: collect job cards ------------------------------------------
    def collect_job_cards(self) -> dict:
        self.action_count += 1
        obs = self._submit(self._observe())
        return {"job_cards": list(obs.job_cards), "count": len(obs.job_cards), "version": obs.version}

    # -- tool 10: open job detail -------------------------------------------
    def open_job_detail(self, handle: str = "", url: str = "") -> dict:
        self.action_count += 1
        return self._submit(self._open_job_detail(handle, url),
                            timeout=max(self.action_timeout_s, self.nav_timeout_ms / 1000 + 8))

    async def _open_job_detail(self, handle: str, url: str) -> dict:
        target = url
        if not target and handle:
            card = getattr(self, "_last_cards", {}).get(handle)
            if card:
                target = card.get("url", "")
        same_doc = bool(target) and _same_document(self._page.url, target)
        if target and not same_doc:
            if not self._trusted(target):
                raise BrowserActorError(f"refused untrusted job-detail URL: {target}")
            await self._page.goto(target, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        elif handle:
            # same-document (hash-routed SPA) or click-through detail
            await self._locator(handle).click(timeout=self.nav_timeout_ms)
            await self._page.wait_for_timeout(500)
        else:
            raise BrowserActorError("open_job_detail needs a card handle or a trusted url")
        obs = await self._observe()
        # bounded detail evidence: the visible main text
        try:
            main = await self._page.evaluate(
                "() => { const m = document.querySelector('main,[role=main],article') || document.body;"
                " return (m.innerText||'').replace(/\\s+/g,' ').trim().slice(0, 6000); }"
            )
        except Exception:  # noqa: BLE001
            main = ""
        return {
            "url": obs.url, "title": obs.title, "headings": list(obs.headings),
            "detail_text": main, "challenge": obs.challenge, "observation": obs.to_dict(),
        }

    # -- tool 11: back -------------------------------------------------------
    def back(self) -> dict:
        self.action_count += 1
        return self._submit(self._back()).to_dict()

    async def _back(self) -> Observation:
        await self._page.go_back(wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        return await self._observe()

    # -- tool 12: close ------------------------------------------------------
    def close(self) -> dict:
        if self._closed:
            return {"ok": True, "already_closed": True}
        self._closed = True
        try:
            fut = asyncio.run_coroutine_threadsafe(self._teardown(), self._loop)
            fut.result(timeout=15)
        except Exception as exc:  # noqa: BLE001
            self.diagnostics.append(f"teardown: {type(exc).__name__}: {str(exc)[:120]}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "actions": self.action_count, "diagnostics": list(self.diagnostics)}

    async def _teardown(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()

    # -- context manager -----------------------------------------------------
    def __enter__(self) -> "CompanyBrowserActor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def state_snapshot(self) -> dict:
        return {
            "actor_id": self.actor_id, "company": self.company, "task_id": self.task_id,
            "url": (self._page.url if self._page else ""),
            "action_count": self.action_count, "observation_version": self.observation_version,
            "redirect_chain": list(self.redirect_chain), "trusted_entry_domain": self._entry_domain,
            "json_endpoints": list(self._json_endpoints), "query_history": list(self.query_history),
            "diagnostics": list(self.diagnostics), "closed": self._closed,
        }
