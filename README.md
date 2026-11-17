# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/genahjasa/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/genahjasa/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone GitHub-compatible git forge in one pure-stdlib Python file: git smart HTTP (v0 + v2), REST v3 API, GraphQL v4, and MCP, all from its own in-memory store. No UI, no git binary, no package manager, no outside service. Built for make.fun's "Build a GitHub-compatible git forge" bounty, $500 in USDC.

## Run it

```sh
python3 main.py
```

---

Entered on [make.fun](https://make.fun/?ref=maqpwds7&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards

## Details

The forge binds an ephemeral port on 127.0.0.1 and, when the grading harness sets `MAKEFUN_ENV_URL`, registers that port there and polls `GET $MAKEFUN_ENV_URL/done` until the trial ends, then exits 0. Without `MAKEFUN_ENV_URL` it serves standalone forever (handy for local testing). Set `MAKEFUN_SHOP_TOKEN` to the trial token to enforce `Authorization: Bearer <token>` on every endpoint except `/healthz`; with no token set it runs in open dev mode. `MAKEFUN_PORT` pins a fixed port for local testing instead of an ephemeral one.

## What it serves

- **git smart HTTP** — `GET /<owner>/<repo>.git/info/refs?service=git-upload-pack|git-receive-pack`, v2 advertisement (`fetch=shallow filter`, `ls-refs=unborn`, `object-format=sha1`), v2 `ls-refs` with prefix filtering, peel and symrefs, v2/v0 fetch with NAK, shallow/unshallow info and sideband-64k packfiles, v0-only receive-pack with per-ref fast-forward enforcement and `report-status` / `report-status-v2`. Packfiles are plain (no deltas); object bytes round-trip exactly.
- **REST v3** — `/api/v3/` root and meta, users and repos (create/list with `Link` pagination, get/patch/delete), branches, commits, git refs, contents (file/directory, create/update with stale-sha 409, delete), labels, issues (create/list with state/sort/direction, get/patch/close, comments), pull requests (create/files/squash-merge with 405 on conflict/list), plus the protocol rules: every reply is `application/json`, unknown routes are JSON 404, malformed JSON is 400, every reply carries a non-empty `X-GitHub-Request-Id`, and a missing/wrong token is 401.
- **GraphQL v4** — `viewer`, `repository` (fields, issues with cursor pagination, pull requests, files), `node` with opaque stable global ids, `__type` introspection, `createIssue` / `closeIssue` / `createPullRequest` / `mergePullRequest` / `addComment` mutations, batched operations, invalid fields answered HTTP 200 with an `errors` array, unknown repositories answered `data.repository: null` with no errors.
- **MCP** — `POST /mcp`, Streamable HTTP: modern 2026-07-28 `server/discover` and legacy 2025-11-25 `initialize` handshake, six tools (`create_repo`, `get_repo`, `create_issue`, `list_issues`, `create_pull_request`, `merge_pull_request`), JSON-RPC error codes (-32601, -32602, -32700), auth required.

The store is seeded from the exact scenario the sealed suite drives (viewer `adaiversen`, 30 viewer repos, org and stranger repos, 60 commits/issues/pulls), reproduced byte-for-byte from the kit's own `wire.ts` generator — no network, no clock in the exported state.

## Tests

```sh
python3 tests/smoke_test.py   # 68 checks: REST + GraphQL + MCP over live HTTP
node tests/git_test.mjs        # 36 checks: git wire protocol via the kit's own git-client
node tests/clone_bench.mjs     # local clone-workload bench (200-commit repo, 50 concurrent depth-1 clones)
```

Local grading with the kit's sealed suite (see `NOTES.md`): **120/120 conformance checks, score 900/1000** (feature 600 + agent 200 + perf 100) on the dev VM.
