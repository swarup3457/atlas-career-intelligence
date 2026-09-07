---
name: career-page-check
description: Perform a lightweight, read-only public career-page reachability and keyword-search check for a single company, using the Atlas BrowserManager.
status: SCAFFOLDED
version: 0.1.0
---

# career-page-check

Wraps `atlas.workers.career_page.CareerPageWorker` for use as a
standalone skill. Proven mechanism; no business search-lane rules
included.

## Usage

```python
from atlas.browser.manager import BrowserManager
from atlas.workers.career_page import CareerPageWorker

with BrowserManager(profile_dir) as manager:
    worker = CareerPageWorker(manager, entry_urls={"Example": "https://example.com"})
    outcome = worker.attempt("Example", attempt_number=1)
```
