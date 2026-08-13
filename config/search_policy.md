# Search Policy

This tracker is a recurring market-monitoring system, not a one-time job search.

## Search order
1. Daily company delta watch.
2. Due retry and blocked companies.
3. Rolling deep company sweep.
4. Employer ATS sweep.
5. Specialist sources such as Wellfound, Talent500, Instahyre, Cutshort, Hirist, TopHire, and Weekday.
6. LinkedIn, Naukri, Foundit, and Indeed public or indexed results.
7. Official verification of strong portal leads.

## ATS families
Search Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Oracle Recruiting, SAP SuccessFactors, Phenom, Eightfold, and employer-owned career systems.

## Verification rules
- Canonical identity is official job ID, otherwise normalized company + title + location.
- Do not treat a generic landing page as a verified job.
- Portal leads stay unverified until a specific official live page exists.
- Never show an unchanged known job as new.
- Keep candidate-confirmed statuses truthful.

## Evidence rules
- Use public professional evidence only for contacts.
- If no verified recruiting contact exists, record: No verified recruiting email found.
- Do not guess salary, work authorization, sponsorship, or recruiter identities.
