"""Smoke test for makefun-forge: drives the live server over REST/GraphQL/MCP."""
import base64
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error

PORT = 18765
TOKEN = 'shpat_testtoken123'
BASE = 'http://127.0.0.1:%d' % PORT

proc = subprocess.Popen(
    [sys.executable, 'main.py'],
    cwd='/home/hatch/workspace/makefun/github-core-clone-01',
    env={'PATH': '/usr/bin:/bin', 'MAKEFUN_SHOP_TOKEN': TOKEN,
         'MAKEFUN_ENV_URL': '', 'MAKEFUN_PORT': str(PORT), 'HOME': '/home/hatch'},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + '/healthz', timeout=1) as r:
                assert r.status == 200 and json.loads(r.read()) == {'ok': True}
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise SystemExit('server did not start')

    passed = []
    failed = []

    def check(name, cond, detail=''):
        (passed if cond else failed).append(name)
        print(('PASS ' if cond else 'FAIL ') + name + (' | ' + str(detail)[:200] if detail and not cond else ''))

    def req(method, path, body=None, token=TOKEN, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            r.add_header('Content-Type', 'application/json')
        if token:
            r.add_header('Authorization', 'Bearer ' + token)
        r.add_header('Accept', 'application/vnd.github+json')
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, json.loads(resp.read() or b'null')
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, e.headers, json.loads(raw or b'null')
            except ValueError:
                return e.code, e.headers, raw

    # ---- REST ----
    s, h, b = req('GET', '/api/v3/')
    check('rest root', s == 200 and h.get('X-OAuth-Scopes') == 'repo, read:org', (s, h.get('X-OAuth-Scopes')))
    s, h, b = req('GET', '/api/v3/meta')
    check('rest meta', s == 200 and b.get('installed_version'), (s, b))
    s, h, b = req('GET', '/api/v3/user')
    check('rest user', s == 200 and b.get('login') == 'adaiversen', (s, b))
    s, h, b = req('GET', '/api/v3/user/repos', token=None)
    check('rest 401 anon', s == 401, s)
    s, h, b = req('POST', '/api/v3/user/repos', {'name': 'smoke-repo', 'private': True, 'description': 'd'})
    check('rest create repo', s == 201 and b['name'] == 'smoke-repo', (s, b))
    s, h, b = req('POST', '/api/v3/user/repos', {'name': 'smoke-repo', 'private': True})
    check('rest create dup 422', s == 422, s)
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo')
    check('rest get repo', s == 200 and b['default_branch'] == 'main', (s, b))
    s, h, b = req('GET', '/api/v3/repos/olgabauer/smoke-repo')
    check('rest foreign 404', s == 404, s)
    s, h, b = req('GET', '/api/v3/repos/adaiversen/nope')
    check('rest missing 404', s == 404 and isinstance(b.get('message'), str), (s, b))
    content = base64.b64encode(b'hello world\n').decode()
    s, h, b = req('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md',
                  {'message': 'add readme', 'content': content})
    check('rest put contents 201', s == 201 and b['content']['encoding'] == 'base64', (s, b))
    commit_sha = b['commit']['sha']
    blob_sha = b['content']['sha']
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md')
    check('rest get contents', s == 200 and base64.b64decode(b['content']).decode() == 'hello world\n'
          and b['sha'] == blob_sha and b['size'] == 12, (s, b))
    s, h, b = req('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md',
                  {'message': 'upd', 'content': base64.b64encode(b'hello2\n').decode(), 'sha': '0' * 40})
    check('rest stale sha 409', s == 409, s)
    s, h, b = req('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md',
                  {'message': 'upd', 'content': base64.b64encode(b'hello2\n').decode(), 'sha': blob_sha})
    check('rest update contents 200', s in (200, 201), (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/docs')
    check('rest dir listing', s == 200 and isinstance(b, list) and any(e['name'] == 'readme.md' for e in b), (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/branches')
    check('rest branches', s == 200 and any(x['name'] == 'main' for x in b), (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/commits')
    check('rest commits', s == 200 and len(b) == 2 and b[0]['commit']['message'] == 'upd', (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/git/refs',
                  {'ref': 'refs/heads/feature', 'sha': commit_sha})
    check('rest create branch', s == 201 and b['ref'] == 'refs/heads/feature', (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/git/refs/heads/feature')
    check('rest get ref', s == 200 and b['object']['sha'] == commit_sha, (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/labels', {'name': 'bug', 'color': 'ededed'})
    check('rest create label', s == 201 and b['name'] == 'bug', (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                  {'title': 'Broken clone', 'body': 'body text', 'labels': ['bug']})
    check('rest create issue', s == 201 and b['number'] == 1 and b['state'] == 'open', (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                  {'title': 'Bad', 'body': 'x', 'labels': ['nope']})
    check('rest bad label 422', s == 422, s)
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                  {'title': 'Second', 'body': 'b2'})
    check('rest issue numbers increment', s == 201 and b['number'] == 2, (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/issues?state=open&sort=created&direction=desc&per_page=100')
    check('rest list issues desc', s == 200 and [i['number'] for i in b] == [2, 1], (s, b))
    s, h, b = req('PATCH', '/api/v3/repos/adaiversen/smoke-repo/issues/1', {'state': 'closed'})
    check('rest close issue', s == 200 and b['state'] == 'closed', (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/issues/2/comments', {'body': 'a comment'})
    check('rest comment create', s == 201 and b['body'] == 'a comment', (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/issues/2/comments')
    check('rest comment list', s == 200 and len(b) == 1, (s, b))
    # pulls
    s, h, b = req('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/other.txt',
                  {'message': 'on feature', 'content': base64.b64encode(b'feat\n').decode(), 'branch': 'feature'})
    check('rest put on branch', s == 201, (s, b))
    s, h, b = req('POST', '/api/v3/repos/adaiversen/smoke-repo/pulls',
                  {'title': 'Add other', 'body': 'pb', 'head': 'feature', 'base': 'main'})
    check('rest create pull', s == 201 and b['number'] == 3 and b['state'] == 'open', (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls/3/files')
    _other = [f for f in b if f['filename'] == 'docs/other.txt']
    check('rest pull files', s == 200 and len(_other) == 1
          and _other[0]['status'] == 'added' and _other[0]['additions'] >= 1, (s, b))
    s, h, b = req('PUT', '/api/v3/repos/adaiversen/smoke-repo/pulls/3/merge', {'merge_method': 'squash'})
    check('rest merge', s == 200 and b['merged'] is True, (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls/3')
    check('rest merged pr closed', s == 200 and b['state'] == 'closed' and b['merged'] is True
          and b['merge_commit_sha'] is not None, (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/docs/other.txt?ref=main')
    check('rest merged content on main', s == 200 and base64.b64decode(b['content']) == b'feat\n', (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls?state=all&per_page=100')
    check('rest pulls list all', s == 200 and len(b) == 1, (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls?per_page=100')
    check('rest pulls default open', s == 200 and len(b) == 0, (s, b))
    # pagination: 30 repos
    for i in range(30):
        req('POST', '/api/v3/user/repos', {'name': 'page-%02d' % i, 'private': True})
    seen = []
    url = '/api/v3/user/repos?per_page=10'
    for _ in range(10):
        s, h, b = req('GET', url)
        assert s == 200, (s, b)
        seen += [x['name'] for x in b]
        link = h.get('Link')
        if not link:
            break
        nxt = link.split(';')[0].strip('<>')
        assert nxt.startswith(BASE), nxt
        url = nxt[len(BASE):]
    check('rest pagination 30+', len(seen) >= 30 and len(seen) == len(set(seen)), len(seen))
    # malformed body + unknown route + request id
    r = urllib.request.Request(BASE + '/api/v3/user/repos', data=b'{"name":', method='POST')
    r.add_header('Authorization', 'Bearer ' + TOKEN)
    r.add_header('Content-Type', 'application/json')
    try:
        urllib.request.urlopen(r, timeout=5)
        s = 200
    except urllib.error.HTTPError as e:
        s = e.code
    check('rest malformed 400', s == 400, s)
    s, h, b = req('GET', '/api/v3/no-such-route')
    check('rest unknown 404 json', s == 404 and isinstance(b.get('message'), str)
          and h.get('X-GitHub-Request-Id'), (s, b))

    # ---- GraphQL ----
    def gql(query, variables=None, token=TOKEN):
        return req('POST', '/api/graphql', {'query': query, 'variables': variables or {}}, token=token)

    s, h, b = gql('query ViewerLogin { viewer { login } }')
    check('gql viewer', s == 200 and b['data']['viewer']['login'] == 'adaiversen', (s, b))
    s, h, b = gql('query RepositoryFields($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id databaseId name owner { login } defaultBranchRef { name } viewerPermission hasIssuesEnabled } }',
                  {'owner': 'adaiversen', 'name': 'smoke-repo'})
    check('gql repo fields', s == 200 and b['data']['repository']['name'] == 'smoke-repo'
          and b['data']['repository']['defaultBranchRef']['name'] == 'main'
          and b['data']['repository']['viewerPermission'] == 'ADMIN'
          and b['data']['repository']['hasIssuesEnabled'] is True, (s, b))
    repo_id = b['data']['repository']['id']
    check('gql repo id opaque', repo_id != str(b['data']['repository']['databaseId']), repo_id)
    s, h, b = gql('query Issue_fields { Issue: __type(name: "Issue") { fields(includeDeprecated: true) { name } } }')
    names = [f['name'] for f in b['data']['Issue']['fields']]
    check('gql introspect Issue', s == 200 and all(x in names for x in ['id', 'number', 'title', 'state', 'body']), names)
    s, h, b = gql('query PullRequestAndRepositoryFields { PullRequest: __type(name: "PullRequest") { fields(includeDeprecated: true) { name } } Repository: __type(name: "Repository") { fields(includeDeprecated: true) { name } } }')
    pr_names = [f['name'] for f in b['data']['PullRequest']['fields']]
    repo_names = [f['name'] for f in b['data']['Repository']['fields']]
    check('gql introspect PR+Repo', all(x in pr_names for x in ['id', 'number', 'headRefName', 'baseRefName', 'mergeable', 'state', 'merged', 'files'])
          and all(x in repo_names for x in ['id', 'databaseId', 'name', 'nameWithOwner', 'owner', 'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled', 'issue', 'issues', 'pullRequests', 'pullRequest']), (pr_names, repo_names))
    s, h, b = gql('query SearchType_enumValues { SearchType: __type(name: "SearchType") { enumValues(includeDeprecated: true) { name } } }')
    check('gql introspect SearchType', 'ISSUE' in [e['name'] for e in b['data']['SearchType']['enumValues']], b)
    s, h, b = gql('query OpenIssues($owner: String!, $name: String!, $first: Int!) { repository(owner: $owner, name: $name) { issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) { nodes { number title state id } } } }',
                  {'owner': 'adaiversen', 'name': 'smoke-repo', 'first': 10})
    nodes = b['data']['repository']['issues']['nodes']
    check('gql open issues desc', s == 200 and [n['number'] for n in nodes] == [2]
          and nodes[0]['state'] == 'OPEN', (s, b))
    s, h, b = gql('query IssueByNumber($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { issue(number: $number) { number title state id body } } }',
                  {'owner': 'adaiversen', 'name': 'smoke-repo', 'number': 2})
    check('gql issue by number', s == 200 and b['data']['repository']['issue']['number'] == 2
          and b['data']['repository']['issue']['title'] == 'Second', (s, b))
    issue2_id = b['data']['repository']['issue']['id']
    s, h, b = gql('mutation IssueCreate($input: CreateIssueInput!) { createIssue(input: $input) { issue { number id } } }',
                  {'input': {'repositoryId': repo_id, 'title': 'GQL issue', 'body': 'gb'}})
    check('gql create issue', s == 200 and b['data']['createIssue']['issue']['number'] == 4, (s, b))
    gql_issue_num = b['data']['createIssue']['issue']['number']
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/issues/%d' % gql_issue_num)
    check('gql create issue rest view', s == 200 and b['title'] == 'GQL issue', (s, b))
    s, h, b = gql('query IssueByNumber($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { issue(number: $number) { id } } }',
                  {'owner': 'adaiversen', 'name': 'smoke-repo', 'number': gql_issue_num})
    gql_issue_id = b['data']['repository']['issue']['id']
    s, h, b = gql('mutation IssueClose($input: CloseIssueInput!) { closeIssue(input: $input) { issue { number id state } } }',
                  {'input': {'issueId': gql_issue_id}})
    check('gql close issue', s == 200 and b['data']['closeIssue']['issue']['state'] == 'CLOSED', (s, b))
    s, h, b = gql('mutation AddComment($input: AddCommentInput!) { addComment(input: $input) { commentEdge { node { id body } } } }',
                  {'input': {'subjectId': issue2_id, 'body': 'gql comment'}})
    check('gql add comment', s == 200 and b['data']['addComment']['commentEdge']['node']['body'] == 'gql comment', (s, b))
    s, h, b = req('GET', '/api/v3/repos/adaiversen/smoke-repo/issues/2/comments')
    check('gql comment rest view', s == 200 and any(c['body'] == 'gql comment' for c in b), (s, b))
    s, h, b = gql('query ViewerInvalidField { viewer { login noSuchFieldOnViewer } }')
    check('gql invalid field 200+errors', s == 200 and b.get('errors')
          and any('noSuchFieldOnViewer' in e['message'] for e in b['errors']), (s, b))
    s, h, b = gql('query ViewerLogin { viewer { login } }', token=None)
    check('gql unauth 401', s == 401, s)
    s, h, b = gql('query NodeRepository($id: ID!) { node(id: $id) { id ... on Repository { name nameWithOwner } } }',
                  {'id': repo_id})
    check('gql node repo', s == 200 and b['data']['node']['name'] == 'smoke-repo'
          and b['data']['node']['nameWithOwner'] == 'adaiversen/smoke-repo', (s, b))
    s, h, b = gql('query RepositoryFields($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }',
                  {'owner': 'adaiversen', 'name': 'missing-repo'})
    check('gql unknown repo null', s == 200 and b['data']['repository'] is None and not b.get('errors'), (s, b))
    # batch
    s, h, b = req('POST', '/api/graphql',
                  [{'query': 'query ViewerLogin { viewer { login } }'},
                   {'query': 'query Issue_fields { Issue: __type(name: "Issue") { fields(includeDeprecated: true) { name } } }'}])
    check('gql batch', s == 200 and isinstance(b, list) and len(b) == 2
          and b[0]['data']['viewer']['login'] == 'adaiversen' and b[1]['data']['Issue']['fields'], (s, b))
    # pagination: create 7 issues in fresh repo
    req('POST', '/api/v3/user/repos', {'name': 'pages-repo', 'private': False})
    for i in range(7):
        req('POST', '/api/v3/repos/adaiversen/pages-repo/issues', {'title': 'page %d' % (i + 1), 'body': 'b'})
    s, h, b = gql('query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) { repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }',
                  {'owner': 'adaiversen', 'name': 'pages-repo', 'first': 5, 'after': None})
    p1 = b['data']['repository']['issues']
    check('gql page 1', s == 200 and len(p1['nodes']) == 5 and p1['pageInfo']['hasNextPage'] is True
          and p1['pageInfo']['endCursor'], (s, b))
    s, h, b = gql('query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) { repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }',
                  {'owner': 'adaiversen', 'name': 'pages-repo', 'first': 5, 'after': p1['pageInfo']['endCursor']})
    p2 = b['data']['repository']['issues']
    check('gql page 2', s == 200 and len(p2['nodes']) == 2 and p2['pageInfo']['hasNextPage'] is False, (s, b))

    # ---- MCP ----
    def mcp(body, token=TOKEN, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        r = urllib.request.Request(BASE + '/mcp', data=data, method='POST')
        r.add_header('Content-Type', 'application/json')
        r.add_header('Accept', 'application/json, text/event-stream')
        r.add_header('MCP-Protocol-Version', '2026-07-28')
        r.add_header('Mcp-Method', 'tools/list')
        if token:
            r.add_header('Authorization', 'Bearer ' + token)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')

    s, b = mcp({'jsonrpc': '2.0', 'id': 'discover-1', 'method': 'server/discover', 'params': {}})
    check('mcp discover', s == 200 and b['result']['serverInfo']['name']
          and '2026-07-28' in b['result']['supportedVersions']
          and b['result']['capabilities'].get('tools') == {}, (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 'init-1', 'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}})
    check('mcp initialize legacy', s == 200 and b['result']['protocolVersion'] == '2025-11-25'
          and b['result']['serverInfo']['name'], (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    tools = b['result']['tools']
    names = [t['name'] for t in tools]
    check('mcp tools list', s == 200 and len(tools) >= 6
          and all(x in names for x in ['create_repo', 'get_repo', 'create_issue', 'list_issues', 'create_pull_request', 'merge_pull_request'])
          and all(t.get('description') and t['inputSchema']['type'] == 'object' for t in tools), (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'create_repo', 'arguments': {'name': 'mcp-repo', 'private': True}}})
    check('mcp create_repo', s == 200 and b['result']['structuredContent']['full_name'] == 'adaiversen/mcp-repo', (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'get_repo', 'arguments': {'owner': 'adaiversen', 'repo': 'mcp-repo'}}})
    check('mcp get_repo', s == 200 and 'mcp-repo' in b['result']['content'][0]['text'], (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                'params': {'name': 'create_issue', 'arguments': {'owner': 'adaiversen', 'repo': 'mcp-repo', 'title': 'MCP issue', 'body': 'mb'}}})
    check('mcp create_issue', s == 200 and b['result']['structuredContent']['number'] == 1, (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                'params': {'name': 'list_issues', 'arguments': {'owner': 'adaiversen', 'repo': 'mcp-repo', 'state': 'open'}}})
    check('mcp list_issues', s == 200 and 'MCP issue' in b['result']['content'][0]['text'], (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
                'params': {'name': 'nope_tool', 'arguments': {}}})
    check('mcp unknown tool', s == 200 and b.get('error', {}).get('code') in (-32601, -32602), (s, b))
    s, b = mcp(None, raw=b'{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"')
    check('mcp parse error', s == 400 and b.get('error', {}).get('code') == -32700, (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                'params': {'name': 'create_issue', 'arguments': {}}})
    check('mcp missing args', s == 200 and (b.get('error', {}).get('code') == -32602 or b['result'].get('isError')), (s, b))
    s, b = mcp({'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
                'params': {'name': 'create_issue', 'arguments': {'owner': 'x', 'repo': 'y', 'title': 't'}}}, token=None)
    check('mcp auth 401', s == 401, s)

    print('\n%d passed, %d failed' % (len(passed), len(failed)))
    if failed:
        print('FAILED:', failed)
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
        if failed:
            print('--- server log ---')
            print(out[-3000:])
    except Exception:
        proc.kill()
sys.exit(1 if failed else 0)
