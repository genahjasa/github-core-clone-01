/* Local clone-workload bench: 200-commit repo, 50 concurrent depth-1 clones, 3 rounds. */
import { spawn } from 'node:child_process';
import {
  GitClient, ZERO_OID, buildCommit, buildTree, collectReachable, hashObject,
} from '/home/hatch/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit/program/env/src/git-client.ts';
import { buildScenario } from '/home/hatch/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit/program/env/src/wire.ts';

const BASE = 'http://127.0.0.1:18772';
const TOKEN = 'shpat_testtoken123';
const scenario = buildScenario();
const viewer = scenario.viewer.login;
const server = spawn('python3', ['main.py'], {
  cwd: '/home/hatch/workspace/makefun/github-core-clone-01',
  env: { ...process.env, MAKEFUN_SHOP_TOKEN: TOKEN, MAKEFUN_ENV_URL: '', MAKEFUN_PORT: '18772' },
  stdio: 'ignore',
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(BASE + '/healthz'); if (r.ok) break; } catch {}
    await sleep(200);
  }
  const client = new GitClient({ baseUrl: BASE, token: TOKEN });
  await client.createRepo(viewer, 'bench-clone', {});
  // Build a 200-commit history in one push (one file changed per commit).
  const local = new Map();
  const add = (t, d) => { const o = hashObject(t, d); local.set(o, { type: t, data: d }); return o; };
  let parent = null;
  let tip = null;
  for (let i = 0; i < 200; i++) {
    const blob = add('blob', Buffer.from(`line ${i}\n`.repeat(20)));
    const tree = add('tree', buildTree([{ mode: '100644', name: 'history.txt', oid: blob }]));
    const c = scenario.commits[i % scenario.commits.length];
    tip = add('commit', buildCommit({
      tree, parents: parent ? [parent] : [],
      author: `${scenario.viewer.name} <${scenario.viewer.email}> ${c.epochSec + i} +0000`,
      committer: `${scenario.viewer.name} <${scenario.viewer.email}> ${c.epochSec + i} +0000`,
      message: `commit ${i}\n`,
    }));
    parent = tip;
  }
  const objs = [...collectReachable([tip], (o) => local.get(o) ?? null)]
    .map((o) => ({ oid: o, ...local.get(o) }));
  console.log('pushing', objs.length, 'objects...');
  const tPush0 = performance.now();
  const res = await client.push(viewer, 'bench-clone',
    [{ oldOid: ZERO_OID, newOid: tip, ref: 'refs/heads/main' }], { objects: objs });
  console.log('setup push:', res.report?.refs[0]?.ok, ((performance.now() - tPush0) / 1000).toFixed(2), 's');
  // Warmup: 5 clones.
  for (let i = 0; i < 5; i++) await client.clone(viewer, 'bench-clone', { depth: 1, parse: false });
  for (let round = 0; round < 3; round++) {
    const t0 = performance.now();
    const results = await Promise.all(
      Array.from({ length: 50 }, () => client.clone(viewer, 'bench-clone', { depth: 1, parse: false })));
    const dt = performance.now() - t0;
    const ok = results.filter((r) => r.checksumOk).length;
    console.log(`round ${round + 1}: ${dt.toFixed(1)} ms, ${ok}/50 ok, packBytes~${results[0]?.packBytes}`);
  }
} catch (err) {
  console.error('THREW', err);
} finally {
  server.kill();
  process.exit(0);
}
