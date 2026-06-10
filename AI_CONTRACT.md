# AI Development Contract

Claude must operate autonomously.

Rules:

* Follow REQUIREMENTS.md strictly
* Create, modify, and delete files as needed
* Maintain scalable and modular architecture
* Use best practices for FastAPI and vector databases
* Run tests and fix failures automatically
* Keep APIs stable and consistent
* Use environment variables for secrets/API keys
* Commit changes incrementally

Allowed:

* Create project structure
* Add dependencies
* Create config files
* Add vector DB integrations
* Add embedding pipelines
* Add admin tools/utilities

Do not:

* Ask for confirmation unless destructive action is required
* Hardcode secrets
* Break API contracts
* Introduce unnecessary complexity

---

# Database Rules

All database tables must include:

```sql
id              BIGINT PRIMARY KEY AUTO_INCREMENT
status          SMALLINT NOT NULL DEFAULT 1
added_time      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
last_modified   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
```

Status values:

| Value | Meaning             |
| ----- | ------------------- |
| 0     | Inactive   |
| 1     | Active              |
| 5     | Deleted |

Requirements:

* Prefer soft delete using `status = 0`
* Use reusable ORM base model
* All APIs should respect active/inactive status filtering

---

# Code Quality Requirements

* Follow Python best practices
* Use type hints
* Separate business logic from API routes
* Maintain reusable services/modules
* Add logging and error handling

---

# Security Requirements

* Validate uploaded files
* Prevent unsafe uploads
* Protect API secrets
* Sanitize user inputs
* Handle LLM rate limits safely
