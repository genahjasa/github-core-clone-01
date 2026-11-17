/* Git conformance smoke test against makefun-forge, using the kit's own client. */
import { spawn } from 'node:child_process';
import {
  GitClient, ZERO_OID, buildCommit, buildTag, buildTree, collectReachable,
  hashObject, parseCommit, parseTag, readPack, verifyPackChecksum,
} from '/home/hatch/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit/program/env/src/git-client.ts';
import { buildScenario } from '/home/hatch/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit/program/env/src/wire.ts';

const BASE = 'http://127.0.0.1:18767';
const TOKEN = 'shpat_testtoken123';
const scenario = buildScenario();
const viewer = scenario.viewer.login;
const passed = [];
const failed = [];
function check(name, cond, detail = '') {
  (cond ? passed : failed).push(name);
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' | ' + String(detail).slice(0, 300)));
}

class LocalRepo {
  constructor() { this.objects = new Map(); this.refs = new Map(); }
  lookup(oid) { return this.objects.get(oid) ?? null; }
  add(type, data) {
    const oid = hashObject(type, data);
    this.objects.set(oid, { type, data });
    return oid;
  }
  commit(files, meta) {
    // files: [{path, data: Buffer}]
    const byDir = new Map();
    const put = (path, data) => this.add('blob', data);
    const treeFor = (prefix) => {
      const entries = [];
      const seen = new Set();
      for (const f of files) {
        if (!f.path.startsWith(prefix)) continue;
        const rest = f.path.slice(prefix.length);
        const slash = rest.indexOf('/');
        if (slash === -1) {
          entries.push({ mode: '100644', name: rest, oid: put(f.path, f.data) });
        } else if (!seen.has(rest.slice(0, slash))) {
          seen.add(rest.slice(0, slash));
          entries.push({ mode: '40000', name: rest.slice(0, slash), oid: treeFor(prefix + rest.slice(0, slash) + '/') });
        }
      }
      return this.add('tree', buildTree(entries));
    };
    const tree = treeFor('');
    const parents = meta.parents ?? [];
    const commit = buildCommit({
      tree, parents,
      author: `${scenario.viewer.name} <${scenario.viewer.email}> ${meta.epoch} +0000`,
      committer: `${scenario.viewer.name} <${scenario.viewer.email}> ${meta.epoch} +0000`,
      message: meta.message,
    });
    return this.add('commit', commit);
  }
  tag(name, target, meta) {
    const tag = buildTag({
      object: target, type: 'commit', tag: name,
      tagger: `${scenario.viewer.name} <${scenario.viewer.email}> ${meta.epoch} +0000`,
      message: meta.message,
    });
    return this.add('tag', tag);
  }
  reachable(tips) { return collectReachable(tips, (oid) => this.lookup(oid)); }
}

async function pushRefs(client, local, owner, repo, commands) {
  const adv = await client.advertisement(owner, repo, 'git-receive-pack');
  const onServer = new Set();
  for (const ref of adv.refs) {
    if (!ref.oid || ref.oid === ZERO_OID) continue;
    try { for (const oid of collectReachable([ref.oid], (o) => local.lookup(o))) onServer.add(oid); } catch { onServer.clear(); break; }
  }
  const newTips = commands.filter((c) => c.newOid !== ZERO_OID).map((c) => c.newOid);
  const send = [];
  for (const oid of collectReachable(newTips, (o) => local.lookup(o))) {
    if (onServer.has(oid)) continue;
    const obj = local.objects.get(oid);
    if (obj) send.push({ oid, type: obj.type, data: obj.data });
  }
  const result = await client.push(owner, repo, commands, { objects: send });
  if (result.report) {
    for (const r of result.report.refs) {
      if (!r.ok) continue;
      const cmd = commands.find((c) => c.ref === r.ref);
      if (!cmd) continue;
      if (cmd.newOid === ZERO_OID) local.refs.delete(r.ref);
      else local.refs.set(r.ref, cmd.newOid);
    }
  }
  return result;
}

const server = spawn('python3', ['main.py'], {
  cwd: '/home/hatch/workspace/makefun/github-core-clone-01',
  env: { ...process.env, MAKEFUN_SHOP_TOKEN: TOKEN, MAKEFUN_ENV_URL: '', MAKEFUN_PORT: '18767' },
  stdio: 'ignore',
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(BASE + '/healthz'); if (r.ok) break; } catch {}
    await sleep(200);
  }
  const client = new GitClient({ baseUrl: BASE, token: TOKEN });
  const v0client = new GitClient({ baseUrl: BASE, token: TOKEN, gitProtocol: 0 });
  const blob0 = scenario.blobs[0];
  const blob1 = scenario.blobs[1];
  const meta = (i) => ({ epoch: scenario.commits[i].epochSec, message: scenario.commits[i].message });

  // --- advertise ---
  await client.createRepo(viewer, 'git-adv', {});
  let adv = await client.advertisement(viewer, 'git-adv', 'git-upload-pack');
  check('adv v2 protocol', adv.protocol === 2, adv.protocol);
  check('adv v2 caps', adv.capabilities.has('fetch') && adv.capabilities.has('ls-refs')
    && adv.capabilities.get('fetch').includes('shallow'), [...adv.capabilities.keys()].join(','));
  const adv0 = await v0client.advertisement(viewer, 'git-adv', 'git-upload-pack');
  check('adv v0 protocol', adv0.protocol === 0, adv0.protocol);
  check('adv v0 empty refs', adv0.refs.length === 0, adv0.refs.length);
  const advR = await client.advertisement(viewer, 'git-adv', 'git-receive-pack');
  check('adv receive v0 + caps', advR.protocol === 0
    && advR.capabilities.has('report-status') && advR.capabilities.has('side-band-64k')
    && advR.capabilities.has('delete-refs'), [...advR.capabilities.keys()].join(','));
  const missing = await client.infoRefs(viewer, 'git-adv-missing', 'git-upload-pack');
  check('unknown repo 404', missing.status === 404, missing.status);

  // --- initial push + clone ---
  const local = new LocalRepo();
  const tip1 = local.commit([{ path: blob0.path, data: Buffer.from(blob0.content) }], { ...meta(0), parents: [] });
  let res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: ZERO_OID, newOid: tip1, ref: 'refs/heads/main' }]);
  check('push initial ok', res.report?.unpackOk && res.report.refs[0]?.ok && res.sideband === true,
    JSON.stringify(res.report));
  const clone = await client.clone(viewer, 'git-adv');
  check('clone protocol v2', clone.protocol === 2, clone.protocol);
  check('clone objects complete', clone.objects.has(tip1)
    && [...local.reachable([tip1])].every((oid) => clone.objects.has(oid)),
    `${clone.objects.size} vs ${local.reachable([tip1]).size}`);
  const clonedCommit = clone.objects.get(tip1);
  check('clone commit bytes', clonedCommit && Buffer.from(clonedCommit.data).equals(local.objects.get(tip1).data));
  check('clone checksum', clone.checksumOk && clone.pack !== null);

  // --- clone empty ---
  await client.createRepo(viewer, 'git-empty', {});
  const emptyClone = await client.clone(viewer, 'git-empty');
  const emptyRealRefs = emptyClone.refs.filter((r) => r.name.startsWith('refs/'));
  check('clone empty', emptyClone.objects.size === 0 && emptyRealRefs.length === 0
    && (emptyClone.pack === null || emptyClone.packBytes === 0),
    `${emptyClone.objects.size} realRefs=${emptyRealRefs.length} packBytes=${emptyClone.packBytes}`);

  // --- ls-refs ---
  const ls = await client.lsRefs(viewer, 'git-adv');
  check('ls-refs main+HEAD', ls.protocol === 2
    && ls.refs.some((r) => r.name === 'refs/heads/main' && r.oid === tip1)
    && ls.headSymref === 'refs/heads/main', JSON.stringify(ls.refs.map((r) => r.name)));
  const lsP = await client.lsRefs(viewer, 'git-adv', { prefixes: ['refs/heads/'] });
  check('ls-refs prefix heads', lsP.rawRefs.every((r) => r.name.startsWith('refs/heads/'))
    && lsP.rawRefs.some((r) => r.name === 'refs/heads/main'), lsP.rawRefs.map((r) => r.name).join(','));

  // --- annotated tag ---
  const tagOid = local.tag('v1', tip1, { ...meta(1), message: 'release v1\n' });
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: ZERO_OID, newOid: tagOid, ref: 'refs/tags/v1' }]);
  check('push tag ok', res.report?.refs[0]?.ok, JSON.stringify(res.report));
  const ls2 = await client.lsRefs(viewer, 'git-adv');
  const tagRef = ls2.refs.find((r) => r.name === 'refs/tags/v1');
  check('ls-refs tag peel', tagRef && tagRef.oid === tagOid && tagRef.peeled === tip1, JSON.stringify(tagRef));
  const clone2 = await client.clone(viewer, 'git-adv');
  const clonedTag = clone2.objects.get(tagOid);
  check('clone tag bytes', clonedTag && Buffer.from(clonedTag.data).equals(local.objects.get(tagOid).data));

  // --- second commit + incremental fetch ---
  const tip2 = local.commit([
    { path: blob0.path, data: Buffer.from(blob0.content) },
    { path: blob1.path, data: Buffer.from(blob1.content) },
  ], { ...meta(2), parents: [tip1] });
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: tip1, newOid: tip2, ref: 'refs/heads/main' }]);
  check('push ff update', res.report?.refs[0]?.ok, JSON.stringify(res.report));
  const inc = await client.fetch(viewer, 'git-adv', { wants: [tip2], haves: [tip1] });
  check('fetch incremental', inc.objects.has(tip2) && !inc.objects.has(tip1)
    && [...local.reachable([tip2])].filter((o) => !local.reachable([tip1]).has(o))
      .every((o) => inc.objects.has(o))
    && ![...local.reachable([tip1])].some((o) => inc.objects.has(o)),
    `got ${inc.objects.size}`);
  const nonew = await client.fetch(viewer, 'git-adv', { wants: [tip2], haves: [tip2] });
  check('fetch nothing new', nonew.objects.size === 0, nonew.objects.size);

  // --- shallow ---
  const shallow1 = await client.fetch(viewer, 'git-adv', { wants: [tip2], depth: 1 });
  check('shallow depth1', shallow1.shallow.includes(tip2) && !shallow1.objects.has(tip1)
    && shallow1.objects.has(tip2), `shallow=${shallow1.shallow} objs=${shallow1.objects.size}`);
  const deep = await client.fetch(viewer, 'git-adv', { wants: [tip2], haves: [tip2], shallow: [tip2], depth: 2 });
  check('deepen 2', (deep.shallow.includes(tip1) || deep.unshallow.includes(tip2))
    && deep.objects.has(tip1) && !deep.objects.has(tip2), `shallow=${deep.shallow} unshallow=${deep.unshallow}`);
  const tip3 = local.commit([
    { path: blob0.path, data: Buffer.from(blob0.content) },
    { path: blob1.path, data: Buffer.from(blob1.content + '\n# rev\n') },
  ], { ...meta(3), parents: [tip2] });
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: tip2, newOid: tip3, ref: 'refs/heads/main' }]);
  check('push tip3', res.report?.refs[0]?.ok);
  const shallowNew = await client.fetch(viewer, 'git-adv', { wants: [tip3], haves: [tip2], shallow: [tip2], depth: 1 });
  check('shallow new tip', shallowNew.shallow.includes(tip3) && shallowNew.objects.has(tip3)
    && !shallowNew.objects.has(tip2), `shallow=${shallowNew.shallow}`);

  // --- v0 fetch path ---
  const v0fetch = await v0client.fetch(viewer, 'git-adv', { wants: [tip3], haves: [tip2] });
  check('v0 fetch incremental', v0fetch.protocol === 0 && v0fetch.objects.has(tip3) && !v0fetch.objects.has(tip2),
    `proto=${v0fetch.protocol} objs=${v0fetch.objects.size}`);

  // --- non-fast-forward + mixed + deletes ---
  const sibling = local.commit([{ path: blob0.path, data: Buffer.from(blob0.content) }],
    { ...meta(4), parents: [tip2] });
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: tip3, newOid: sibling, ref: 'refs/heads/main' }]);
  const ng = res.report?.refs[0];
  check('push non-ff refused', res.report?.unpackOk === true && ng && !ng.ok && ng.reason === 'non-fast-forward',
    JSON.stringify(res.report));
  const lsAfter = await client.lsRefs(viewer, 'git-adv');
  check('non-ff ref unmoved', lsAfter.refs.find((r) => r.name === 'refs/heads/main')?.oid === tip3);
  res = await pushRefs(client, local, viewer, 'git-adv', [
    { oldOid: ZERO_OID, newOid: sibling, ref: 'refs/heads/side' },
    { oldOid: tip3, newOid: sibling, ref: 'refs/heads/main' },
  ]);
  check('push mixed', res.report?.refs.length === 2
    && res.report.refs.find((r) => r.ref === 'refs/heads/side')?.ok === true
    && res.report.refs.find((r) => r.ref === 'refs/heads/main')?.ok === false,
    JSON.stringify(res.report));
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: sibling, newOid: ZERO_OID, ref: 'refs/heads/side' }]);
  check('push delete branch', res.report?.refs[0]?.ok, JSON.stringify(res.report));
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: tagOid, newOid: ZERO_OID, ref: 'refs/tags/v1' }]);
  check('push delete tag', res.report?.refs[0]?.ok, JSON.stringify(res.report));

  // --- push 100 files ---
  const files100 = [];
  for (let i = 0; i < 100; i++) files100.push({ path: `bulk/f${String(i).padStart(3, '0')}.txt`, data: Buffer.from(`file ${i}\n`) });
  const tip100 = local.commit(files100, { ...meta(5), parents: [tip3] });
  res = await pushRefs(client, local, viewer, 'git-adv', [{ oldOid: tip3, newOid: tip100, ref: 'refs/heads/main' }]);
  check('push 100 files', res.report?.refs[0]?.ok, JSON.stringify(res.report));
  const clone100 = await client.clone(viewer, 'git-adv');
  check('clone 100 files intact', [...local.reachable([tip100])].every((o) => clone100.objects.has(o))
    && clone100.objects.get(tip100) !== undefined, `${clone100.objects.size}`);

  // --- pack integrity + sideband ---
  let sawSideband = false;
  const packCheck = await client.fetch(viewer, 'git-adv', {
    wants: [tip100], haves: [],
    observe: (p) => { if (p.kind === 'data' && p.payload[0] === 1) sawSideband = true; },
  });
  check('pack integrity', packCheck.pack !== null && verifyPackChecksum(packCheck.pack).ok
    && readPack(packCheck.pack).objects.size > 0, 'pack null?');
  check('sideband framing', sawSideband, 'no band-1 packet seen');

  // --- malformed body survival ---
  const bad = await fetch(BASE + `/${viewer}/git-adv.git/git-upload-pack`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-git-upload-pack-request', Authorization: `Bearer ${TOKEN}` },
    body: Buffer.from('zzzznot-a-pkt-line'),
  });
  await bad.arrayBuffer();
  const lsAfterMalformed = await client.lsRefs(viewer, 'git-adv');
  check('survives malformed body', lsAfterMalformed.protocol === 2
    && lsAfterMalformed.refs.some((r) => r.name === 'refs/heads/main'), 'adv failed after malformed');
  const cloneAfter = await client.clone(viewer, 'git-adv');
  check('clone intact after malformed', [...local.reachable([tip100])].every((o) => cloneAfter.objects.has(o)));

  console.log(`\n${passed.length} passed, ${failed.length} failed`);
  if (failed.length) console.log('FAILED:', failed);
} catch (err) {
  console.error('TEST THREW:', err);
  process.exitCode = 2;
} finally {
  server.kill();
  process.exit(failed.length ? 1 : 0);
}
