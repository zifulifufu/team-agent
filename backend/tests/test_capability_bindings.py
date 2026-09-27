"""Capabilities must be attached to the intended group without activating other resources."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.tools import skills_prompt, write_skill
from tests.conftest import FakeLLM


@pytest.fixture
def catalog(tmp_path):
    data = tmp_path / 'data'
    plugins = data / 'plugins'
    plugins.mkdir(parents=True)
    (plugins / 'qa.py').write_text('PLUGIN={"name":"QA plugin"}\ndef register(r):\n r.register("qa_echo", "Echo", {}, lambda a: "ok")\n')
    app = create_app(data, completion_fn=FakeLLM(), token='', background=False)
    store = app.state.store
    write_skill(data / 'skills', 'QA procedure', 'QA', 'Always include QA-EVIDENCE-42.', 'group')
    server = store.add_mcp('QA disabled', command='must-not-execute')
    store.update_mcp(server['id'], {'enabled': False})
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client, store, server


def test_binding_preserves_other_extensions_and_does_not_activate_server(catalog):
    c, st, server = catalog
    gid = st.list_groups()[0]['id']
    st.update_group(gid, {'ext': {'memory': False, 'plan': 'off', 'library': {'mode': 'off'}}})
    before = st.get_group(gid)['ext']
    url = f'/api/groups/{gid}/capability-binding'
    for kind, ref in [('skills', 'QA procedure'), ('plugins', 'qa'), ('mcp', server['id'])]:
        for _ in range(2):
            r = c.patch(url, json={'kind': kind, 'ref': ref, 'attached': True})
            assert r.status_code == 200, r.text
        assert st.get_group(gid)['ext'][kind].count(ref) == 1
    ext = st.get_group(gid)['ext']
    assert ext['memory'] == before['memory']
    assert ext['library'] == before['library']
    assert ext['plan'] == before['plan']
    assert st.get_mcp(server['id'])['enabled'] is False
    assert 'QA-EVIDENCE-42' in skills_prompt(st.data_dir / 'skills', ext['skills'], group=True)
    cap = c.get(f'/api/groups/{gid}/capabilities').json()
    assert any(t['name'].endswith('qa_echo') for t in cap['tools']), cap['tools']
    assert c.patch(url, json={'kind': 'plugins', 'ref': 'qa', 'attached': False}).status_code == 200
    after = st.get_group(gid)['ext']
    assert after['skills'] == ext['skills'] and after['mcp'] == ext['mcp']
    assert not any(t['name'].endswith('qa_echo') for t in c.get(f'/api/groups/{gid}/capabilities').json()['tools'])


@pytest.mark.parametrize('kind', ['skills', 'plugins', 'mcp'])
def test_missing_resource_rejected_but_orphan_can_be_removed(catalog, kind):
    c, st, _ = catalog
    gid = st.list_groups()[0]['id']
    before = st.get_group(gid)['ext']
    url = f'/api/groups/{gid}/capability-binding'
    assert c.patch(url, json={'kind': kind, 'ref': 'missing', 'attached': True}).status_code == 404
    assert st.get_group(gid)['ext'] == before
    st.update_group(gid, {'ext': {kind: ['missing']}})
    assert c.patch(url, json={'kind': kind, 'ref': 'missing', 'attached': False}).status_code == 200
    assert not st.get_group(gid)['ext'][kind]


def test_localized_skill_can_be_attached_then_removed_in_another_language(catalog):
    c, st, _ = catalog
    gid = st.list_groups()[0]['id']
    en = c.get('/api/skills', headers={'Accept-Language': 'en'}).json()
    zh = c.get('/api/skills', headers={'Accept-Language': 'zh'}).json()
    # Match physical files, not list order (sorting follows the display language).
    english = next(s for s in en if any(z['path'] == s['path'] and z['name'] != s['name'] for z in zh))
    chinese = next(s for s in zh if s['path'] == english['path'])
    url = f'/api/groups/{gid}/capability-binding'
    assert c.patch(url, json={'kind': 'skills', 'ref': chinese['name'], 'attached': True}).status_code == 200
    assert c.patch(url, json={'kind': 'skills', 'ref': english['name'], 'attached': True}).status_code == 200
    assert len([x for x in st.get_group(gid)['ext']['skills'] if x in (english['name'], chinese['name'])]) == 1
    assert c.patch(url, json={'kind': 'skills', 'ref': english['name'], 'attached': False}).status_code == 200
    assert not any(x in st.get_group(gid)['ext']['skills'] for x in (english['name'], chinese['name']))


async def test_busy_group_cannot_change_bindings(tmp_path):
    entered = asyncio.Event()
    async def slow(**kw):
        entered.set()
        await asyncio.Event().wait()
    app = create_app(tmp_path / 'data', completion_fn=slow, token='', background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as c:
            gid = (await c.get('/api/groups')).json()[0]['id']
            url = f'/api/groups/{gid}'
            before = app.state.store.get_group(gid)['ext']
            await c.post(url + '/messages', json={'text': '@Copywriter wait'})
            await asyncio.wait_for(entered.wait(), 3)
            assert (await c.patch(url + '/capability-binding', json={'kind': 'skills', 'ref': 'anything', 'attached': False})).status_code == 409
            assert app.state.store.get_group(gid)['ext'] == before
            await c.post(url + '/stop')


def test_hook_tests_honor_registered_events_and_saved_scope(catalog):
    c, st, _ = catalog
    first = st.list_groups()[0]['id']
    second = st.create_group('Another')['id']
    hook = '/api/hooks/example-round-log'
    assert c.patch(hook, json={'groups': [second]}).status_code == 200
    assert c.post(hook + '/test', json={'event': 'before_send', 'group_id': second}).status_code == 422
    assert c.post(hook + '/test', json={'event': 'round.end', 'group_id': first}).status_code == 422
    assert c.post(hook + '/test', json={'event': 'round.end', 'group_id': 'missing'}).status_code == 404
    result = c.post(hook + '/test', json={'event': 'round.end'})
    assert result.status_code == 200 and result.json()['ok'], result.text
    log = c.get('/api/hooks/log').json()['entries']
    assert log[0]['group_id'] == second
