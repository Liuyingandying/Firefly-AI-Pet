# Qwen Code Instructions

## Secret Review Policy

### Rule
Before outputting any content from tool reads, config inspection, log analysis, terminal output, or diff display, check for sensitive authentication information.

### Prohibited from output (verbatim)
- API Key
- access token
- bearer token
- refresh token
- password
- secret
- cookie / session cookie
- Authorization header
- private key / PEM
- SSH private key
- OAuth credential
- database credential
- webhook secret
- environment variable secret value
- any other authentication credential

### Required redaction
If any prohibited content is detected, output:

***REDACTED***

### Allowed to display
- Environment variable name (e.g., API_KEY)
- Key name
- Whether secret exists
- Secret type (e.g., TJU API token)
- Length
- Configuration structure

### When reading config files
If only checking permissions, model, tools, paths, or switches — do NOT dump the entire config file. Only read/show necessary fields. If the file contains auth config, redact auth fields first.

### Test secret verification
When verifying redaction, use only fake test secrets:

TEST_API_KEY=sk-test-not-real-123456789

Output must show:

TEST_API_KEY=***REDACTED***

Never use real keys for testing.

## Continuous Engineering Policy

When the user gives a clear multi-step engineering objective:

1. Do not stop after completing one file, function, test, or other sub-step.
2. Continue automatically to the next planned item.
3. Saying "I will do the next step" is not a reason to end the current turn; do the next step in the same turn.
4. Return control to the user only when:
   - a product or architecture decision is required;
   - sensitive permission approval is required;
   - a destructive or irreversible action is required;
   - credentials or an external resource are unavailable;
   - repeated repair attempts leave a genuine blocker; or
   - the complete objective is actually finished.
5. When an ordinary test fails, diagnose it, make the smallest warranted fix, and rerun the test without stopping for routine confirmation.
6. After completing one TODO, continue automatically to the next unchecked TODO.
7. Never perform an unauthorized push, destructive delete, production deployment, or secret change.

For `/loop` and `/goal` work, keep advancing established work until it is complete or genuinely blocked. Preserve the Secret Review Policy above in every interactive, autonomous, resumed, and headless session.

## Permission Denial Loop Guard

When a tool call fails with a permission/approval denial (for example `execution_denied`, "requires user approval but cannot execute in non-interactive mode", or "permission was declined"):

1. Do NOT retry the identical tool call.
2. If the same tool has been denied 2 times consecutively, stop retrying it entirely.
3. Record a short blocker note stating which tool was denied and why.
4. Exit the current execution rather than looping.

This does not disable the runaway/loop protection; it complements it.
