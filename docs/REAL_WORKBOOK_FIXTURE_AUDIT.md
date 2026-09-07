# Real Workbook Fixture Audit

This document records only what was observed in
`fixtures/real/Atlas_Jobs_2026-08-13.xlsx`; it does not assign unobserved
business meaning to a field or make the historical workbook authoritative.

## File integrity

| Property | Observed value |
| --- | --- |
| Filename | `Atlas_Jobs_2026-08-13.xlsx` |
| Size | 59,720 bytes |
| SHA-256 | `440B9587610FBA5B8C0E61498FE52EE92A5139749F1F29D57ED7734A2A25B8AC` |
| Sheets | 8 |
| Formulas | None observed |
| Hyperlinks | 15 observed, all on `All_Jobs` `Source_URL` cells |

The workbook contains formatted sparse ranges reporting `max_column=702`;
the effective header counts below stop at the final non-blank header.

## Observed sheets and headers

| Sheet | Effective headers | Non-blank data rows |
| --- | ---: | ---: |
| `All_Jobs` | 23 | 25 |
| `New_Companies` | 11 | 9 |
| `Company_Coverage` | 16 | 16 |
| `Source_Coverage` | 12 | 22 |
| `Closed_or_Rejected` | 11 | 1 |
| `Resume_Tailoring` | 12 | 8 |
| `Recruiter_Contacts` | 13 | 0 |
| `Run_Summary` | 28 | 1 |

### `All_Jobs`

`Company`, `Role`, `Job_ID`, `Location`, `Work_Mode`, `Experience`,
`Search_Lane`, `Company_Type`, `Discovery_Source`, `Source_URL`,
`Official_Apply_URL`, `Posted_Date`, `Freshness`, `Verification_Status`,
`Live_Status`, `Match_Score`, `Requirements_Matched`,
`Missing_Requirements`, `Why_It_Fits`, `Application_Recommendation`,
`First_Seen`, `Last_Verified`, `Notes`.

Observed Excel date fields: `Posted_Date` (16), `First_Seen` (24), and
`Last_Verified` (24). `Match_Score` has 16 numeric values. Three `Role`
cells are numeric-looking. No duplicate-looking rows were detected using
the first eight raw values.

### Remaining sheets

- `New_Companies`: `Company`, `Official_Domain`, `Careers_Domain`, `ATS`,
  `Company_Type`, `India_Locations`, `Source_Discovered_From`,
  `Reason_Added`, `Suggested_Tier`, `Next_Delta_Check`, `Next_Deep_Check`.
  The two final fields contain observed dates.
- `Company_Coverage`: `Company`, `Tier`, `Company_Type`,
  `Official_Careers_Domain`, `ATS`, `Check_Type`, `Checked_At`, `Result`,
  `Relevant_Jobs`, `New_Jobs`, `Known_Unchanged`, `Closed`,
  `Manual_Verification`, `Access_Status`, `Next_Delta_Check`,
  `Next_Deep_Check`. Observed dates are `Checked_At`, `Next_Delta_Check`,
  and `Next_Deep_Check`; the five count fields are numeric.
- `Source_Coverage`: `Source`, `Attempted`, `Completed`,
  `Searches_Performed`, `Raw_Results`, `Relevant_Results`,
  `Verified_Official`, `Portal_Only`, `Manual_Verification`, `Closed`,
  `Access_Limited`, `Notes`. The eight count fields from
  `Searches_Performed` through `Access_Limited` are numeric where present.
- `Closed_or_Rejected`: `Company`, `Role`, `Job_ID`, `Location`, `Source`,
  `Reason`, `Verification_Status`, `Live_Status`, `Experience`,
  `Posted_Date`, `Notes`. `Posted_Date` is an observed date field.
- `Resume_Tailoring`: `Company`, `Role`, `Job_ID`, `Match_Score`,
  `Summary_Changes`, `Skills_Order`, `Experience_Changes`,
  `Project_Changes`, `Supported_Keywords`, `Do_Not_Claim`, `Genuine_Gaps`,
  `Suggested_Resume_Filename`. `Match_Score` is numeric where present.
- `Recruiter_Contacts`: `Company`, `Role`, `Job_ID`, `Contact_Name`,
  `Contact_Title`, `Email`, `Profile_URL`, `Contact_Type`, `Source`,
  `Confidence`, `Safe_To_Contact`, `Usage_Restrictions`, `Notes`.
- `Run_Summary`: `Run_ID`, `Run_Date`, `Start_Time`, `End_Time`, `Runtime`,
  `Run_Status`, `Companies_Checked`, `New_Companies_Discovered`,
  `Official_Sites_Checked`, `ATS_Checked`, `Workday_Checked`,
  `LinkedIn_Checked`, `Naukri_Checked`, `Foundit_Checked`, `Indeed_Checked`,
  `Wellfound_Checked`, `Talent500_Checked`, `Specialist_Sources_Checked`,
  `Raw_Discoveries`, `Relevant_Discoveries`, `Duplicates_Suppressed`,
  `Verified_Official`, `Portal_Only`, `Manual_Verification`, `Closed`,
  `GitHub_Events_Written`, `GitHub_Readback`, `Remaining_Work`.
  `Run_Date`, `Start_Time`, `End_Time`, and `Runtime` are stored as Excel
  date/time values where populated; the listed count fields are numeric
  where populated.

## Observed quality characteristics

Empty fields are frequent because the historical sheets have sparse data
regions. The import layer preserves blanks, raw values, unknown columns,
sheet name, row number, source file, run identifier, and observed timestamp
without inferring missing values. Cross-sheet links are inferable only from
shared company/role/job-ID-style columns; the audit does not claim a final
relationship policy.

The reproducible machine-readable audit is
`output/phase09/PHASE09_AUDIT.json`. See `docs/PHASE09_AUDIT.md` for the
Phase 0.9 run results.
