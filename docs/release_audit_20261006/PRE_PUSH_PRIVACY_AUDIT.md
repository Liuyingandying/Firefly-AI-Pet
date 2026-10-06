# Pre-push privacy audit — 2026-10-06

## Scope and results

The entire candidate file tree was exported from the exact Git tracked/untracked allowlist; runtime/ignored directories were not exported. Gitleaks 8.30.1 was downloaded from its official release and its archive SHA-256 verified (`d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e`). No project dependency stack was upgraded.

- Raw baseline scan: 6 findings; all inspected synthetic test credentials/JWT red-line inputs.
- Three Provider test credential literals were anonymized in the release worktree.
- Candidate raw scan: 3 remaining synthetic JWT guard fixtures. These test that token-like content is rejected before writes. No gitleaks ignore/blanket suppression was added.
- Reviewed real credential values: **0**.
- Private nickname, private export identifier, personal user-directory and local-app-data path matches in candidate source tree: **0**.
- Runtime DBs, raw exports, credentials, private provider payloads, model weights and local audit evidence: **not in the candidate tree**.

## Individually reviewed gitleaks findings

| File | Line at audit | Rule | Disposition |
|---|---:|---|---|
| memory/suggestion/tests/test_suggestion_service_p5a.py | 198 | jwt | Fixed synthetic secret-rejection input; asserts no suggestion/write |
| memory/suggestion/tests/test_suggestion_service_p5a3.py | 292 | jwt | Fixed synthetic secret-rejection input; asserts no suggestion/write |
| tests/test_p6_e2e.py | 431 | jwt | Fixed synthetic red-line input; asserts no memory persistence |

The baseline Provider fixture findings were tests/test_provider_manager_manual_flow.py:32–33 and tests/test_provider_manager_ui.py:37. Temporary credential stores and mocked reloads prove these were test values; their replacement preserves the tested behavior.

## Keyword scan

Every pre-documentation candidate keyword hit is listed in [PRIVACY_TEXT_FINDINGS.csv](PRIVACY_TEXT_FINDINGS.csv) with source location and explanation. Source identifiers, protocol header names and synthetic rejection inputs are distinguished from secrets. No matched line or credential value is reproduced in the audit.

- `API_KEY`: 198 reference hits.
- `Bearer`: 37 reference hits.
- `password`: 36 reference hits.
- `sk-`: 95 reference hits.
- `token`: 451 reference hits.

## Final candidate scan

The fresh pre-commit export contains 919 paths, including public audit documentation. All Python source compiled. The full export again has exactly the three synthetic JWT fixtures above; private-pattern matches remain zero. Reference counts including audit tables and their per-hit CSV are: API_KEY 508, Bearer 87, sk- 212, password 74, token 907. Additional documentation hits are scan-pattern labels, repeated per-hit index rows and public command/configuration references; they do not embed source line values or live secrets.

## Committed and remote verification

The eight published commits through `4f0ae81a74e99189c85a1fe947ca789af7469a9b` were scanned with `gitleaks git --log-opts=origin/main..HEAD`: exit 0, no delta findings. After fetch, the remote recovery branch equals that local HEAD and main still equals the frozen baseline.

The Git archive of that remotely verified tree was scanned in full again: 919 paths, no private-pattern matches, no Python compile errors, exactly the same three synthetic JWT guard fixtures. Raw full-tree scanner exit remains 1 for those reviewed fixtures; it is not reported as an empty raw scan. Pattern counts match the final candidate counts above.

This publication receipt is a documentation-only follow-up. The final receipt commit is scanned once more before and after its normal push; the final exact tip is available from Git history. The raw reports and exact local receipts remain private. The branch remains unmerged.
