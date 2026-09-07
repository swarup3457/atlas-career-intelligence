"""Atlas source test doubles + contract harness (Phase 1A).

These are deterministic, network-free adapters used to validate the source
framework and to give every future real adapter a reusable contract test
suite. They are clearly test doubles (``SourceType.FAKE`` / ``FIXTURE``) and
are NOT real job sources. Following the existing ``atlas.runtime.demo_workload``
precedent, they live inside the package so tests (and the adapter-contract
harness) can import them.
"""
