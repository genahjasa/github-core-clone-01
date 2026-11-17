# github-core-clone-01 — build notes

Bounty: **github-core-clone-01 — "Build a GitHub-compatible git forge"**, $500 in USDC
(1st $100, 2nd $70, 3rd $50, 4th–10th $20, 11th–20th $14).
Deadline: **21 Nov 2026 13:30 WIB** (entries close 2026-11-21T06:30:00Z).
Status: **entry built and locally graded; NOT submitted** (no public GitHub repo exists yet).

## Requirements (learned from the live API + kit + sealed suite source)

- Deliverable: a public GitHub repo owned by the user's own GitHub account, created
  after the competition opened, OSI-approved license, with `main.ts` (Node 22) or
  `main.py` (Python 3.13) in the repo root that starts the forge as **one process**,
  plus the AI coding-agent transcripts of the sessions that built it. No UI.
- Constraints: stdlib plus vendored source only — nothing installed before the program
  runs, no git binary, no package manager, no outside service. One process: binds an
  ephemeral port of its own, registers it with the suite, never binds 8080 (the suite
  listens there). Deterministic: two runs on the same instance leave the same state
  export byte for byte. Max 16 MiB source total, 8 MiB per file. Max 5 submissions/day,
  10 total; last submission before deadline is final. Entry fee: free.
- What is scored (0–1000): feature 600 (share of conformance checks passed, 7 groups)
  + agent 200 (the MCP group again) + perf 200 (four timed workloads vs the house
  reference's calibrated times). A prize needs a score **better than the best house
  entry** (the reference forge, entered as a house entry). The reference forge scores
  ~900/1000 on production (600 + 200 + ~100 perf).
- Conformance (120 checks): git 30, repos 15, issues 15, pulls 20, graphql 20, mcp 15,
  protocol 5. The suite drives the entry as a client; the scenario is fixed
  (`SCENARIO_SEED = 0x6a09e667f3bcc909` in wire.ts): viewer `adaiversen` (id 1000),
  30 viewer repos, 6 org repos, 3 stranger repos, 60 commits/issues/pulls.
  The entry receives only `Authorization: Bearer $MAKEFUN_SHOP_TOKEN` plus
  `MAKEFUN_ENV_URL` / `MAKEFUN_STATE_DIR` — no viewer-login env var; the scenario
  must be reproduced from the seed.
- Bench workloads (60 s budget each, 3 rounds after warmup, p50 is the median round):
  200 sequential pushes of a 1 KiB change; 50 concurrent shallow clones of a
  200-commit repo; 2,000 sequential issue reads; 200 sequential issue writes.
  House T_refs (calibration.json, production medians): push 1640.82 ms, clone
  266.941 ms, read 2759.715 ms, write 348.965 ms per round.
- Intake gates: repo public, OSI license, repo created after competition opened,
  commit SHA fetchable, no duplicate snapshot, no committed provider keys/secrets,
  README with the three live make.fun badges, GitHub About links make.fun
  (homepage + topic `makefun`), transcripts present, run record required for entries
  from 2026-11-17 00:00 UTC (bundle with `--mcp none --provider meta --effort
  default --harness-version <version>`).

## What was built

`main.py` — 3,218 lines, **pure Python 3 stdlib only** (base64, datetime, difflib,
hashlib, json, os, re, socket, threading, time, urllib, zlib). Single file, one
process. Contents:

1. Byte-exact port of the kit's `wire.ts` scenario generator (FNV-1a, mix64,
   xorshift64* RNG, all word lists) — verified `SCENARIO MATCH` against the Node
   ground truth on every field.
2. Git primitives: SHA-1 object ids, tree build/parse with git sort order, commit/tag
   parse, pack write (plain, no deltas) + pack read with OFS/REF delta support,
   pkt-line encode/decode, reachability, ancestor/descendant checks.
3. Forge store: repos, refs, labels, issues (shared number counter with PRs),
   pulls, contents commits (deterministic timestamps off the scenario epoch),
   tree diff + line change counts, squash merge with three-way conflict detection,
   fetch computation (have-closure, depth truncation, shallow/unshallow lists).
4. REST v3, GraphQL v4 (hand-written tokenizer/parser/executor), MCP
   (modern 2026-07-28 discover + legacy initialize), git smart HTTP (v0+v2),
   lifecycle (bind ephemeral port, register, poll `/done`, exit 0).

Performance work (all measured, see below):
- `TCP_NODELAY` on accepted sockets: keep-alive requests went 41.3 ms → 0.7 ms
  (Nagle + delayed-ACK). This alone took read from 0/50 to ~24/50 points.
- Request body pre-read in `_dispatch`: fixes a keep-alive desync where an early
  401/404 left the body in the socket and the next request line was parsed from it
  (this was the root cause of the first suite run's 2 conformance failures).
- Pack cache keyed by (refs fingerprint, object count, fetch parameters): the 50
  identical shallow clones share one pack build.
- zlib level 1 for pack object deflate; listen backlog raised 5 → 128 (the first
  concurrent clone round was ~2 s on SYN drops, now ~0.5 s).
- Read-only requests (GETs, git upload-pack, MCP non-mutating methods) run without
  the global store lock; counters are guarded by their own small lock.

## Test results

- `tests/smoke_test.py`: **68 passed, 0 failed** (REST + GraphQL + MCP over live HTTP).
- `tests/git_test.mjs` (kit's own git-client): **36 passed, 0 failed** (advertise
  v2/v0/receive-pack, clone incl. empty + annotated tag, ls-refs prefixes, fetch
  incremental/nothing-new/shallow/deepen, v0 fetch, non-FF refusal, mixed push,
  branch/tag delete, 100-file push, pack integrity, sideband framing, malformed-body
  survival).
- Sealed suite, run directly (`program/env/src/main.ts` + `program/scorer/score.mjs`,
  Docker unavailable so `grade-local --driver process` cannot run a Python bot;
  the suite was driven by a local runner script, same checks/scorer):
  - Run 1 (before fixes): 118/120 checks, **806/1000** (feature 590, agent 200,
    perf 16). Failures were the keep-alive desync (see above); perf was destroyed
    by the 41 ms/req Nagle penalty.
  - Run 2 (after fixes): **120/120 checks, 900/1000** (feature 600, agent 200,
    perf 100; push 31, clone 22, read 23, write 24; perf_p50_us 795,201 — slightly
    better than the house reference median 805,899). No suite-log failures.
- Local micro-benchmarks (this dev VM, 1.5 shared CPUs): 2,000 keep-alive issue
  GETs in 1,595 ms (0.80 ms/req); 200 issue POSTs in 194 ms (0.97 ms/req);
  50 concurrent depth-1 clones of a 200-commit repo in ~380–500 ms/round.
  Production grading (2 CPUs) should measure equal or faster.

## What is still needed to submit

1. **GitHub account + public repo** — the single blocker. GitHub account is
   `genahjasa` (verified, id 210531860, https://github.com/genahjasa). Repo
   `github-core-clone-01` still needs to be created (public, created after the
   competition opened 2026-11-15 18:00 UTC) — needs a GitHub PAT, not yet
   available. Then push this directory.
2. **Git identity**: `genahjasa <210531860+genahjasa@users.noreply.github.com>`,
   set locally in this repo; all commits use it, never an invented author.
   Local commit done 2026-11-17 (no push yet — no GitHub token).
3. **README badges**: rewritten 2026-11-17 from the requirements'
   `how_to_submit.readme_template` with owner `genahjasa`, repo
   `github-core-clone-01`, referral code `maqpwds7` — no `{owner}`/`{repo}`
   placeholder remains.
4. **`.makefun/owner.json`** is already present, naming the agent wallet
   `DbquhvB27T8Qfiubgm9NXTts7nT9sTHsm3yHKut5fWiM` (same as best-game-01).
5. **Bundle + check + submit** with the kit CLI, from the repo directory:
   `node <kit>/cli/makefun.mjs bundle --mcp none --provider meta --effort default
   --harness-version <version>`, then `check --transcripts`, then
   `submit --competition github-core-clone-01 --repo-url <url> --commit <sha>
   --title <name> --bundle <file> --keypair ~/workspace/makefun/makefun-keypair.json`.
   Save the claim code to
   `~/workspace/goals/earn-usdc-from-make-fun-bounties/hidden_files/submissions.md`
   and report (bounty, prize, deadline) to the user.
6. **Never claim any prize without the user's explicit permission.** Never pay any
   fee; never create a new keypair/wallet for this entry.

## Files

- `main.py` — the entry (pure stdlib, 3,218 lines, ~131 KB)
- `LICENSE` — MIT (from the kit starter)
- `README.md` — from the requirements' `how_to_submit.readme_template`
  (owner `genahjasa`, repo `github-core-clone-01`, referral `maqpwds7` filled in)
- `.makefun/owner.json` — agent wallet
- `tests/smoke_test.py`, `tests/git_test.mjs`, `tests/clone_bench.mjs`
- `NOTES.md` — this file
