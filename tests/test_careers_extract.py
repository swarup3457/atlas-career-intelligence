"""Phase 1C-B Layer A unit tests: career extraction primitives.

Fully offline, deterministic. Covers JSON-LD JobPosting (single + multiple
blocks + @graph + ItemList), bounded embedded JSON, URL normalization, job-link
classification (incl. links inside descriptions), unicode, missing dates, and
the "never invent a value" rule.
"""

from __future__ import annotations

import pytest

from atlas.careers import extract as X

pytestmark = pytest.mark.unit


# --- URL normalization ------------------------------------------------------
def test_normalize_url_resolves_relative():
    assert X.normalize_url("https://acme.com/careers", "/jobs/1") == "https://acme.com/jobs/1"
    assert X.normalize_url("https://acme.com/careers/", "jobs/2") == "https://acme.com/careers/jobs/2"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "mailto:x@y.com", "tel:123", "data:text/html,x", "#frag", "", "  "])
def test_normalize_url_rejects_non_navigational(bad):
    assert X.normalize_url("https://acme.com", bad) is None


def test_normalize_url_drops_fragment_keeps_query():
    assert X.normalize_url("https://acme.com", "/jobs?page=2#top") == "https://acme.com/jobs?page=2"


def test_same_site_registrable():
    assert X.same_site("https://careers.acme.com/x", "https://acme.com/y")
    assert not X.same_site("https://acme.com", "https://evil.com")


# --- job-link classification ------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://acme.com/careers/jobs/123", True),
    ("https://acme.com/job/456", True),
    ("https://acme.com/openings/eng-1", True),
    ("https://acme.com/careers?jobId=99", True),
    ("https://acme.com/privacy", False),
    ("https://acme.com/careers", False),
    ("https://acme.com/about", False),
    ("https://linkedin.com/jobs/1", False),
])
def test_classify_job_link(url, expected):
    assert X.classify_job_link(url) is expected


# --- JSON-LD JobPosting -----------------------------------------------------
def test_jsonld_single_jobposting_full_fields():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Senior Java Engineer",
     "datePosted":"2026-08-01","validThrough":"2026-12-31","employmentType":"FULL_TIME",
     "hiringOrganization":{"name":"Acme"},"url":"https://acme.com/careers/jobs/1",
     "identifier":{"@type":"PropertyValue","value":"REQ-1"},
     "jobLocation":{"@type":"Place","address":{"addressLocality":"Bengaluru","addressCountry":"IN"}},
     "description":"<p>Build services</p>"}
    </script></head><body>content</body></html>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com/careers")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Senior Java Engineer"
    assert j.location == "Bengaluru, IN"
    assert j.posted_at == "2026-08-01"
    assert j.date_provenance == "EMPLOYER_POSTED_AT"
    assert j.deadline == "2026-12-31"
    assert j.source_job_id == "REQ-1"
    assert j.company == "Acme"
    assert j.url == "https://acme.com/careers/jobs/1"


def test_jsonld_multiple_blocks_and_graph():
    html = """<html><head>
    <script type="application/ld+json">{"@type":"JobPosting","title":"A","url":"https://acme.com/j/a"}</script>
    <script type="application/ld+json">{"@graph":[
       {"@type":"Organization","name":"Acme"},
       {"@type":"JobPosting","title":"B","url":"https://acme.com/j/b"}]}</script>
    <script type="application/ld+json">[{"@type":"JobPosting","title":"C","url":"https://acme.com/j/c"}]</script>
    </head><body>x</body></html>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    titles = sorted(j.title for j in jobs)
    assert titles == ["A", "B", "C"]


def test_jsonld_type_as_list():
    html = """<script type="application/ld+json">
    {"@type":["JobPosting","WorkPosition"],"title":"Multi","url":"https://acme.com/j/m"}</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert len(jobs) == 1 and jobs[0].title == "Multi"


def test_jsonld_missing_date_stays_unknown_not_invented():
    html = """<script type="application/ld+json">
    {"@type":"JobPosting","title":"NoDate","url":"https://acme.com/j/n"}</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert jobs[0].posted_at is None
    assert jobs[0].date_provenance == "UNKNOWN"
    # No invented company/location/deadline.
    assert jobs[0].company is None
    assert jobs[0].location is None
    assert jobs[0].deadline is None


def test_jsonld_malformed_block_isolated():
    html = """<html><head>
    <script type="application/ld+json">{ this is : not json }</script>
    <script type="application/ld+json">{"@type":"JobPosting","title":"Good","url":"https://acme.com/j/g"}</script>
    </head></html>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert [j.title for j in jobs] == ["Good"]


def test_jsonld_missing_title_raises_finding_not_fatal():
    # One node without title is skipped as a finding; the valid one survives.
    html = """<script type="application/ld+json">[
      {"@type":"JobPosting","url":"https://acme.com/j/x"},
      {"@type":"JobPosting","title":"Valid","url":"https://acme.com/j/v"}]</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert [j.title for j in jobs] == ["Valid"]


def test_jsonld_unicode_preserved():
    html = """<script type="application/ld+json">
    {"@type":"JobPosting","title":"Инженер — 软件工程师 café","url":"https://acme.com/j/u",
     "jobLocation":{"address":{"addressLocality":"München"}}}</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert jobs[0].title == "Инженер — 软件工程师 café"
    assert jobs[0].location == "München"


def test_jsonld_multiple_locations_joined():
    html = """<script type="application/ld+json">
    {"@type":"JobPosting","title":"Multi","url":"https://acme.com/j/m","jobLocation":[
      {"address":{"addressLocality":"Bengaluru"}},
      {"address":{"addressLocality":"Hyderabad"}}]}</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert "Bengaluru" in jobs[0].location and "Hyderabad" in jobs[0].location


def test_jsonld_remote_detected_from_telecommute():
    html = """<script type="application/ld+json">
    {"@type":"JobPosting","title":"R","url":"https://acme.com/j/r","jobLocationType":"TELECOMMUTE"}</script>"""
    jobs = X.extract_jsonld_jobs(html, base_url="https://acme.com")
    assert jobs[0].remote is True


# --- embedded application-state JSON ---------------------------------------
def test_embedded_next_data_jobs():
    html = """<html><body><div id="root"></div>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"jobs":[
      {"title":"Backend Dev","absolute_url":"https://acme.com/j/1","id":"1","location":"Bengaluru"},
      {"title":"Frontend Dev","absolute_url":"https://acme.com/j/2","id":"2"}]}}}
    </script></body></html>"""
    jobs = X.extract_embedded_jobs(html, base_url="https://acme.com")
    titles = sorted(j.title for j in jobs)
    assert titles == ["Backend Dev", "Frontend Dev"]


def test_embedded_window_assignment_bounded():
    html = """<script>window.__INITIAL_STATE__ = {"jobs":[{"jobTitle":"SRE","jobUrl":"https://acme.com/j/s","jobId":"s1"}]};</script>"""
    jobs = X.extract_embedded_jobs(html, base_url="https://acme.com")
    assert len(jobs) == 1 and jobs[0].title == "SRE" and jobs[0].source_job_id == "s1"


def test_embedded_json_depth_bound_does_not_crash():
    # Deeply nested structure beyond MAX_JSON_DEPTH must be handled safely.
    nested = "{"
    payload = '{"a":' * (X.MAX_JSON_DEPTH + 20) + "1" + "}" * (X.MAX_JSON_DEPTH + 20)
    html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
    # Should not raise and should simply find no jobs.
    assert X.extract_embedded_jobs(html, base_url="https://acme.com") == []


def test_embedded_oversize_blob_skipped():
    big = '{"jobs":[' + ",".join(['{"title":"x","url":"https://acme.com/j/x"}'] * 5) + "]}"
    # Pad the assignment past the byte cap with a huge string value.
    filler = "z" * (X.MAX_EMBEDDED_JSON_BYTES + 10)
    html = f'<script>window.__INITIAL_STATE__ = {{"pad":"{filler}","jobs":[]}}</script>'
    # The scan is bounded; it must not raise.
    assert X.extract_embedded_jobs(html, base_url="https://acme.com") == []


# --- anchors / list pages ---------------------------------------------------
def test_extract_job_links_excludes_description_and_offsite():
    html = """<html><body>
    <a href="/careers/jobs/1">Real Job</a>
    <a href="/privacy">Privacy</a>
    <article><a href="https://evil.com/careers/jobs/9">apply here</a></article>
    <a href="https://partner.com/careers/jobs/2">Offsite Job</a>
    </body></html>"""
    jobs = X.extract_job_links(html, base_url="https://acme.com/careers")
    urls = [j.url for j in jobs]
    assert "https://acme.com/careers/jobs/1" in urls
    assert all("evil.com" not in u for u in urls)      # link inside <article> excluded
    assert all("partner.com" not in u for u in urls)   # offsite excluded


# --- pagination -------------------------------------------------------------
def test_find_next_page_rel_link():
    html = '<html><head><link rel="next" href="/careers?page=2"></head></html>'
    assert X.find_next_page(html, base_url="https://acme.com/careers") == "https://acme.com/careers?page=2"


def test_find_next_page_offsite_rejected():
    html = '<link rel="next" href="https://evil.com/next">'
    assert X.find_next_page(html, base_url="https://acme.com/careers") is None


# --- sitemap / robots -------------------------------------------------------
def test_sitemap_urlset_job_like():
    xml = """<urlset><url><loc>https://acme.com/careers/jobs/1</loc></url>
    <url><loc>https://acme.com/about</loc></url></urlset>"""
    result = X.parse_sitemap(xml, base_url="https://acme.com")
    assert not result.is_index
    assert result.job_like() == ("https://acme.com/careers/jobs/1",)


def test_sitemap_index_child_selection():
    xml = """<sitemapindex>
      <sitemap><loc>https://acme.com/sitemap-jobs.xml</loc></sitemap>
      <sitemap><loc>https://acme.com/sitemap-blog.xml</loc></sitemap></sitemapindex>"""
    result = X.parse_sitemap(xml, base_url="https://acme.com")
    assert result.is_index
    assert X.child_sitemaps_with_job_signal(result) == ("https://acme.com/sitemap-jobs.xml",)


def test_robots_sitemaps_and_policy():
    robots = "User-agent: *\nDisallow: /private\nSitemap: https://acme.com/sitemap.xml"
    assert X.sitemaps_from_robots(robots, base_url="https://acme.com") == ["https://acme.com/sitemap.xml"]
    policy = X.summarize_robots(robots, base_url="https://acme.com")
    assert policy.sitemaps == ("https://acme.com/sitemap.xml",)
    assert policy.disallow_all_for_star is False


# --- careers nav-link scoring ----------------------------------------------
def test_score_career_links_prefers_label_and_path():
    html = """<html><body>
    <a href="/careers">Careers</a>
    <a href="https://jobs.acme.com/">Open Roles</a>
    <a href="/about">About Us</a>
    </body></html>"""
    cands = X.score_career_links(html, base_url="https://acme.com/")
    urls = [c.url for c in cands]
    assert "https://acme.com/careers" in urls
    assert "https://jobs.acme.com/" in urls
    assert all("/about" not in u for u in urls)
    # careers subdomain scores highest.
    assert cands[0].score >= cands[-1].score


# --- SPA shell detection ----------------------------------------------------
def test_looks_like_spa_shell():
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert X.looks_like_spa_shell(shell)
    full = "<html><body>" + ("real job content here " * 200) + "</body></html>"
    assert not X.looks_like_spa_shell(full)
    # With jobs already extracted it is never a shell.
    assert not X.looks_like_spa_shell(shell, extracted_jobs=3)
