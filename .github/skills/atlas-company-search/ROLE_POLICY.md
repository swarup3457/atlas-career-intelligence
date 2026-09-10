# ROLE_POLICY

Permitted primary lanes and the mandatory stack anchor each requires. Deterministic
Python re-derives the lane from captured evidence and rejects mislabels.

## Lanes and anchors

- **JAVA_BACKEND** — must have a Java/JVM anchor (Java, Spring, Spring Boot, JVM,
  Jakarta EE, J2EE).
- **JAVA_FULLSTACK** — must have a Java anchor **and** a named frontend technology
  (React, Angular, Vue, TypeScript/JavaScript).
- **REACT_FRONTEND** — must be genuinely frontend-focused (React / front-end / UI).
- **DOTNET** — must have a C#/.NET anchor (C#, .NET, .NET Core, ASP.NET).
- **ENTERPRISE_HR_PAYROLL_INTEGRATION** — must have a payroll/HCM/HRIS/workforce
  domain anchor.

## Hard rejections

- A Python/Node/Ruby/Go-only "full stack" role is **not** a Java or .NET full-stack
  role; do not label it as one.
- Architecture-advisory and tech-lead/managerial roles are out of scope.
- A role whose only mandatory backend is unsupported (and no supported backend is
  present) is rejected.
- A card without an opened detail page is never accepted.

Propose the lane you believe fits; Python has the final say.
