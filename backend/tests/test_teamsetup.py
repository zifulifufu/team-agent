"""Reviewing a team is read-only; confirmation validates the whole draft before creating it."""
import pytest
from fastapi.testclient import TestClient

from app import media, teamrec
from app.main import create_app
from tests.conftest import FakeLLM


@pytest.fixture
def review_app(tmp_path):
    fake = FakeLLM()
    app = create_app(tmp_path / 'review', completion_fn=fake, token='')
    st = app.state.store
    st.update_settings({'image_enabled': True, 'video_enabled': True, 'process_review': False,
                        'memory_auto_extract': False, 'scoring_enabled': False})
    st.update_provider('deepseek', {'api_key': 'isolated-review-test'})
    st.add_provider_from_preset('comfyui')
    st.add_model('deepseek', 'gpt-image-1', 'Test image')
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client, st, fake


def counts(st):
    return len(st.list_groups()), len(st.list_agents())


def draft(st):
    host = st.list_agents()[0]['id']
    video = next(m for m in st.list_media_models() if m['kind'] == 'comfyui'
                 and m['model_name'] == 'wan2.2-ti2v-5b')
    return {'task': '制作视频', 'host_ref': 'agent:' + host, 'lineup': [
        {'kind': 'agent', 'id': host}, {'kind': 'model', 'id': video['id']},
        {'kind': 'preset', 'id': 'creative'}]}


def test_suggest_proposes_unmaterialized_tools_without_mutation(review_app):
    client, st, fake = review_app
    before = counts(st)
    for _ in range(2):
        result = client.post('/api/team/suggest', json={'text': '制作视频并生成一张配图'}).json()
        assert {m['use'] for m in result['models']} == {'image', 'video'}
        assert any(m['may_host'] for m in result['members'])
    assert counts(st) == before
    assert not fake.calls


def test_confirmation_creates_exact_lineup_and_hidden_engineer_without_dispatch(review_app):
    client, st, fake = review_app
    request = draft(st)
    request['lineup'].append(request['lineup'][1])  # duplicate clicks do not duplicate members
    before = counts(st)
    reply = client.post('/api/groups', json=request)
    assert reply.status_code == 200, reply.text
    g = st.get_group(reply.json()['id'], include_hidden=True)
    visible = [st.get_agent(i) for i in g['member_ids'] if not st.get_agent(i).get('hidden')]
    assert len(visible) == 3
    assert any(a['origin'] == 'media' for a in visible)
    assert g['host_agent_id'] == request['lineup'][0]['id']
    assert len([st.get_agent(i) for i in g['member_ids'] if st.get_agent(i).get('hidden')]) == 1
    assert counts(st)[0] == before[0] + 1
    assert not any(m['sender_type'] in ('user', 'agent', 'plan') for m in st.list_messages(g['id']))
    assert not fake.calls


@pytest.mark.parametrize('problem', ['bad_host', 'missing_member', 'disabled_model', 'invalid_preset', 'media_host', 'bad_workspace'])
def test_invalid_confirmation_does_not_leave_partial_members(review_app, problem):
    client, st, fake = review_app
    request = draft(st)
    # New model/preset precedes the invalid entry to catch eager materialization.
    if problem == 'bad_host':
        request['host_ref'] = 'agent:missing'
    elif problem == 'missing_member':
        request['lineup'].append({'kind': 'agent', 'id': 'missing'})
    elif problem == 'disabled_model':
        st.update_model(request['lineup'][1]['id'], {'enabled': False})
    elif problem == 'invalid_preset':
        request['lineup'].append({'kind': 'preset', 'id': 'not-a-role'})
    elif problem == 'media_host':
        request['host_ref'] = 'model:' + request['lineup'][1]['id']
    elif problem == 'bad_workspace':
        request['workspace'] = '/does/not/exist/team-review'
    before = counts(st)
    reply = client.post('/api/groups', json=request)
    assert reply.status_code == 400, reply.text
    assert counts(st) == before
    assert not fake.calls


def test_recommendation_limits_duplicate_generators_and_excludes_unconfigured(review_app):
    _, st, _ = review_app
    video = [m for m in st.list_media_models() if m['kind'] == 'comfyui' and m['use'] == 'video']
    for m in video:
        st.ensure_model_agent(m['id'])
    provider = st.add_provider_from_preset('metachat')  # no key: must not be auto-selected
    unavailable = st.add_model(provider['id'], 'gpt-image-1', 'Unconfigured image')
    broken = st.ensure_model_agent(unavailable['id'])
    advice = teamrec.suggest(st, '制作视频')
    members = [st.get_agent(a['id']) for a in advice['members']]
    assert broken['id'] not in [a['id'] for a in members]
    generators = [a for a in members if a['origin'] == media.MEDIA_ORIGIN]
    assert len(generators) == 1
    assert not advice['models']


def test_short_task_still_gets_a_host_and_missing_generation_is_explained(review_app):
    _, st, _ = review_app
    assert any(m['may_host'] for m in teamrec.suggest(st, 'hello')['members'])
    st.update_settings({'video_enabled': False})
    advice = teamrec.suggest(st, '制作视频')
    assert not advice['models']
    assert advice['warnings']


def test_existing_expert_id_is_exposed_and_reused_when_joining_another_group(review_app):
    client, st, _ = review_app
    expert = client.post('/api/agents/from-preset', json={'key': 'creative'}).json()
    rows = client.get('/api/agent-presets', headers={'Accept-Language': 'zh'}).json()
    preset = next(p for p in rows if p['key'] == 'creative')
    assert preset['exists'] and preset['agent_id'] == expert['id']
    before = len(st.list_agents())
    gid = st.list_groups()[0]['id']
    for _ in range(2):
        assert client.post(f'/api/groups/{gid}/members/from-preset', json={'key': 'creative'}).status_code == 200
    assert len(st.list_agents()) == before
    assert st.get_group(gid)['member_ids'].count(expert['id']) == 1


def test_generic_video_does_not_pull_renderers_and_narration_uses_one_voice(review_app):
    _, st, _ = review_app
    from app import localcmd
    st.update_settings({'external_agents_enabled': True})
    for key in ['remotion', 'hyperframes', 'shotcraft', 'qwen3tts', 'voicestudio']:
        row = localcmd.row(key)
        st.create_agent(row['name'], role=row['role'], engine=key)
    video = teamrec.suggest(st, '制作一个视频')
    assert not any(st.get_agent(a['id']).get('engine') for a in video['members'])
    voiced = teamrec.suggest(st, '制作一个视频并配上旁白')
    engines = [st.get_agent(a['id']).get('engine') for a in voiced['members']]
    assert [e for e in engines if e] == ['qwen3tts']
    named = teamrec.suggest(st, '用 Remotion 制作视频')
    assert any(st.get_agent(a['id']).get('engine') == 'remotion' for a in named['members'])


def test_report_does_not_recruit_video_role_or_unrelated_existing_experts(review_app):
    client, st, _ = review_app
    client.post('/api/agents/from-preset', json={'key': 'creative'})
    advice = teamrec.suggest(st, '请写一份项目周报，列出进度、问题、下周计划')
    names = [st.get_agent(a['id'])['name'] for a in advice['members']]
    assert 'Storyboard' not in names
    from app.presets import AGENT_PRESET_BY_KEY
    assert AGENT_PRESET_BY_KEY['creative']['name'] not in names
    assert advice['members'] and any(m['may_host'] for m in advice['members'])
