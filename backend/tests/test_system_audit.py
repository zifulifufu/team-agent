"""Regression probes for cooperation and permission boundaries, with no real tool effects."""
from copy import deepcopy
from types import SimpleNamespace
import asyncio
import os

import httpx
import pytest

from app import attachments, planner, net, embed
from app.orchestrator import RunState, TurnOut
from app.toolhub import ToolContext
from tests.conftest import FakeLLM
from tests.test_collab import Collector, setup


def guarded_tool(orch, group):
    agent = orch.store.group_members(group['id'])[0]
    spec = {'name': 'audit_exec', 'source': 'builtin', 'risk': 'exec', 'parameters': {'required': ['command']}}
    return ToolContext(group, agent, tools={'audit_exec': spec})


async def test_approval_sees_the_arguments_that_hooks_will_execute(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), perm_mode='ask_risky')
    seen, executed = [], []
    async def gate(gid, spec, args):
        return '', {**args, 'command': 'rewritten operation'}
    orch.toolhub.hooks = SimpleNamespace(gate_tool=gate, notify=lambda *a, **kw: None)
    async def approve(spec, args):
        seen.append(deepcopy(args))
        return True
    async def dispatch(ctx, spec, args, timeout, on_progress=None):
        executed.append(deepcopy(args))
        return 'ok', True, []
    orch.toolhub._dispatch = dispatch
    outcome = await orch.toolhub.call(guarded_tool(orch, group), 'audit_exec', {'command': 'original operation'}, approve)
    assert outcome.ok
    assert seen == executed == [{'command': 'rewritten operation'}]
    assert outcome.effective_args == seen[0]


async def test_permission_revoked_while_hook_waits_is_rechecked(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), perm_mode='allow_all')
    executed = []
    async def gate(gid, spec, args):
        store.update_settings({'perm_deny': ['audit_exec']})
        return '', args
    orch.toolhub.hooks = SimpleNamespace(gate_tool=gate, notify=lambda *a, **kw: None)
    async def dispatch(*args):
        executed.append(True)
        return 'ok', True, []
    orch.toolhub._dispatch = dispatch
    outcome = await orch.toolhub.call(guarded_tool(orch, group), 'audit_exec', {'command': 'sample'})
    assert not outcome.ok and outcome.denied
    assert not executed


@pytest.mark.parametrize('kind', ['external', 'media', 'local'])
async def test_network_round_cannot_delegate_to_side_effect_members(store, make_router, kind):
    orch, group = setup(store, make_router, FakeLLM(), external_agents_enabled=True, perm_mode='allow_all')
    agent = dict(store.group_members(group['id'])[0])
    agent.update(origin='media' if kind == 'media' else 'custom', engine='' if kind == 'media' else 'workbuddy' if kind == 'external' else 'hyperframes')
    called = []
    async def execution(*args, **kw):
        called.append(True)
        return None
    orch._external_turn = execution
    orch._media_turn = execution
    orch._local_tool_turn = execution
    run = RunState(group['id'], 'untrusted inbound task', read_only=True)
    collector = Collector()
    result = await orch._agent_turn(group, agent, [agent], collector, run)
    assert result is None
    assert not called
    assert run.last_error and any(e['type'] == 'message_discard' for e in collector.events)


async def test_read_only_context_never_starts_mcp_process(store, make_router):
    orch, group = setup(store, make_router, FakeLLM())
    server = store.add_mcp('Forbidden startup', command='must-never-run')
    group = store.update_group(group['id'], {'ext': {'mcp': [server['id']]}})
    connected = []
    async def connect(*args, **kwargs):
        connected.append(True)
        return SimpleNamespace(status='ready', tools=[], error='')
    orch.mcp.connect = connect
    ctx = await orch.toolhub.context(group, store.group_members(group['id'])[0], read_only=True)
    assert not connected
    assert all(t.get('risk') == 'read' for t in ctx.tools.values())


async def test_generator_context_respects_read_only_even_when_called_directly(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), image_enabled=True, video_enabled=True)
    store.add_provider_from_preset('comfyui')
    model = next(m for m in store.list_media_models() if m['kind'] == 'comfyui')
    agent = store.ensure_model_agent(model['id'])
    ctx = await orch.toolhub.context(group, agent, read_only=True)
    assert not ctx.tools


async def test_failed_final_assembly_does_not_mark_plan_complete(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), process_autolog=False)
    members = store.group_members(group['id'])
    host = members[0]
    task = planner.PlanTask('t1', host['name'], host['id'], 'Prepare', 'prepare notes')
    plan = planner.Plan('Deliver a film', '', [task])
    run = RunState(group['id'], plan.goal)
    async def turn(group, agent, members, emit, run, **kw):
        meta = dict(kw['extra_meta'])
        if meta['task_id'] == 'final':
            meta['tools'] = [{'name': 'assemble_video', 'status': 'failed', 'result': 'renderer failed'}]
        message = store.add_message(group['id'], 'agent', agent['id'], agent['name'], 'response', meta=meta)
        return TurnOut('response', 'response', message)
    orch._agent_turn = turn
    await orch._execute_plan(group, members, host, plan, run, Collector())
    assert task.status == 'done'
    assert plan.status == 'failed'
    assert run.unfinished and any(not o['ok'] for o in run.observations)
    headline = planner.headline({'meta': plan.to_meta()})
    assert headline['id'] == 'final' and headline['status'] == 'failed'
    assert headline['done'] == 1 and headline['total'] == 2


def test_same_size_attachment_edit_invalidates_cached_analysis(tmp_path):
    image = tmp_path / 'frame.png'
    image.write_bytes(b'first')
    first = 1_800_000_000_100_000_000
    os.utime(image, ns=(first, first))
    key = attachments.file_key(tmp_path, image)
    image.write_bytes(b'other')
    os.utime(image, ns=(first + 100_000_000, first + 100_000_000))
    assert attachments.file_key(tmp_path, image) != key


async def test_music_connection_failure_keeps_the_actionable_error(store, make_router, monkeypatch):
    orch, group = setup(store, make_router, FakeLLM(), video_enabled=True)
    store.add_provider_from_preset('comfyui')
    async def ready(*a):
        return True, ''
    orch.toolhub.comfy_runtime.ensure = ready
    def fail(request):
        raise httpx.ConnectError('audit offline', request=request)
    monkeypatch.setattr(net, 'client', lambda *a, **kw: httpx.AsyncClient(transport=httpx.MockTransport(fail)))
    result, ok, files = await orch.toolhub._make_music(guarded_tool(orch, group), {'prompt': 'test'})
    assert not ok and not files
    assert 'ConnectError' in result and 'audit offline' in result


async def test_coalesced_messages_keep_the_earlier_attachment(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), process_autolog=False)
    seen = []
    posted = asyncio.Queue()
    async def emit(event):
        if event['type'] == 'message' and event['message']['sender_type'] == 'user':
            posted.put_nowait(event)
    async def rounds(group, text, emit, run):
        seen.append((text, run.files))
    orch._run_turns = rounds
    files = [{'id': 'uploaded-attachment', 'name': 'reference.png'}]
    lock = orch._lock(group['id'])
    await lock.acquire()
    first = asyncio.create_task(orch.handle_user_message(group['id'], 'Look at this file', emit, files=files))
    await asyncio.wait_for(posted.get(), 2)
    second = asyncio.create_task(orch.handle_user_message(group['id'], 'Describe it', emit))
    await asyncio.wait_for(posted.get(), 2)
    lock.release()
    await asyncio.gather(first, second)
    await orch.drain()
    assert len(seen) == 1
    assert seen[0][1] == files
    assert 'Look at this file' in seen[0][0] and 'Describe it' in seen[0][0]


async def test_queued_channel_and_local_inputs_keep_separate_permissions(store, make_router):
    orch, group = setup(store, make_router, FakeLLM(), process_autolog=False)
    seen, jobs, histories = [], [], []
    posted = asyncio.Queue()
    async def emit(event):
        if event['type'] == 'message' and event['message']['sender_type'] == 'user':
            posted.put_nowait(event)
    async def rounds(group, text, emit, run):
        seen.append((text, run.read_only))
        histories.append([m['content'] for m in run.history if m['sender_type'] == 'user'])
    orch._run_turns = rounds
    async with orch._lock(group['id']):
        for text, read_only in [('local first', False), ('channel task', True), ('local follow-up', False)]:
            jobs.append(asyncio.create_task(orch.handle_user_message(group['id'], text, emit, read_only=read_only)))
            await asyncio.wait_for(posted.get(), 2)
    await asyncio.gather(*jobs)
    await orch.drain()
    assert seen == [('local first', False), ('channel task', True), ('local follow-up', False)]
    assert histories == [['local first'], ['local first', 'channel task'],
                         ['local first', 'channel task', 'local follow-up']]
    assert not orch._pending_inputs


@pytest.mark.parametrize('launch_fails', [False, True])
def test_embed_fallback_log_is_private_and_closed(tmp_path, monkeypatch, launch_fails):
    # An unusable data directory forces the fallback without touching real files.
    bad_dir = tmp_path / 'not-a-directory'
    bad_dir.write_text('occupied')
    monkeypatch.setattr(embed, 'server_argv', lambda cfg: ['audit-no-execution'])
    logs = []
    def launch(argv, **kw):
        logs.append(kw['stdout'])
        assert kw['stderr'] is logs[-1]
        assert os.fstat(logs[-1].fileno()).st_mode & 0o077 == 0
        if launch_fails:
            raise OSError('audit launch failure')
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(embed.subprocess, 'Popen', launch)
    result = embed.start({}, data_dir=str(bad_dir))
    assert result['started'] is (not launch_fails)
    assert logs and logs[0].closed
