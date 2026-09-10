# EXPERIENCE_POLICY

The mandatory minimum-experience gate is applied by deterministic Python after
HTML/Unicode normalization. Report the verbatim experience text; do not compute
years yourself.

## Rules

- A **mandatory** minimum of **4 or more years** is rejected, unless the real
  private candidate profile satisfies it (Python decides; never assume).
- Title seniority (Senior, Lead, Staff, etc.) does **not** invent years of
  experience.
- A technology **version** is not a year count. "Java 8 or above" means the Java
  8 language version, **not** eight years of experience. The same holds for
  "Java 11/17", ".NET 6", "Angular 12", etc.
- Ranges parse on the minimum ("3–5 years" -> 3). "5+", "7.5+", "8+", "12+" years
  are all rejected as 5/7.5/8/12.
- Closed/inactive postings ("no longer accepting", "position filled", "posting
  has expired") are rejected regardless of experience.

Capture the exact experience sentence into `experience_text` and, when present,
as an `evidence_snippets` quote.
