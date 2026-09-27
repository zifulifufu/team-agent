"""Reference bytes reach the edit endpoint; invalid inputs never turn into paid text-only jobs."""
import base64
import hashlib
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from PIL import Image

from app import imagegen
from app.imagegen import generate as generate_image
from tests.test_imagegen import env as env, fake as fake, draw


def source(root, name="reference.png"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    Image.new("RGB", (12, 10), "green").save(path)
    return path


@pytest.mark.parametrize("count", [1, 2])
async def test_edit_uploads_actual_workspace_bytes_and_records_provenance(env, monkeypatch, count):
    orch, store, group, _, _ = env
    work = store.workspace_dir(group['id'])
    paths = [source(work, f"ref-{n}.png") for n in range(count)]
    calls = []
    def reply(request):
        calls.append(request)
        assert request.url.path.endswith('/images/edits')
        document = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + request.headers['content-type'].encode() + b"\r\n\r\n" + request.content)
        parts = list(document.iter_parts())
        images = [p for p in parts if p.get_filename()]
        assert [p.get_payload(decode=True) for p in images] == [p.read_bytes() for p in paths]
        assert all(p.get_param('name', header='content-disposition') == ('image' if count == 1 else 'image[]') for p in images)
        assert any(p.get_payload(decode=True) == b'Synthetic edit' for p in parts)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(paths[0].read_bytes()).decode()}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        async def run(provider, payload, **kwargs):
            return await generate_image(provider, payload, client=client, **kwargs)
        monkeypatch.setattr(imagegen, 'generate', run)
        out = await draw(orch, store, group, {
            'prompt': 'Synthetic edit', 'reference_images': [p.name for p in paths]})
    assert out.ok and len(calls) == 1, out.text
    assert len(out.files[0]['reference_inputs']) == count
    assert out.files[0]['reference_inputs'][0]['sha256'] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    assert paths[0].name in out.text
    assert 'data' not in out.files[0]['reference_inputs'][0]


@pytest.mark.parametrize('bad', ['../outside.png', 'missing.png', 'escape.png', 'not-image.txt'])
async def test_invalid_reference_makes_no_generation_request(env, tmp_path, bad):
    orch, store, group, _, server = env
    work = store.workspace_dir(group['id'])
    work.mkdir(parents=True, exist_ok=True)
    outside = source(tmp_path, 'outside.png')
    (work / 'escape.png').symlink_to(outside)
    (work / 'not-image.txt').write_text('Synthetic non-image')
    out = await draw(orch, store, group, {'prompt': 'Synthetic edit', 'reference_images': [bad]})
    assert not out.ok and not out.files and not server.paths


async def test_unsupported_reference_field_is_not_silently_dropped(env):
    orch, store, group, _, server = env
    out = await draw(orch, store, group, {'prompt': 'Synthetic edit', 'reference_files': ['ref.png']})
    assert not out.ok and not server.paths


async def test_chat_member_can_use_recruited_image_model_without_global_switch(env):
    orch, store, group, provider, server = env
    selected = store.add_model(provider['id'], 'gpt-image-1.5')
    agent = store.ensure_model_agent(selected['id'])
    original = store.get_settings()['image_model']
    out = await draw(orch, store, group, {'model_id': selected['id']})
    assert not out.ok and not server.paths  # registered is not the same as in this group
    store.add_member(group['id'], agent['id'])
    out = await draw(orch, store, group, {'model_id': selected['id']})
    assert out.ok and server.payloads[-1]['model'] == 'gpt-image-1.5'
    assert store.get_settings()['image_model'] == original
    calls = len(server.paths)
    store.update_model(selected['id'], {'enabled': False})
    out = await draw(orch, store, group, {'model_id': selected['id']})
    assert not out.ok and len(server.paths) == calls


async def test_joined_image_model_remains_callable_when_global_default_is_unavailable(env):
    orch, store, group, provider, _ = env
    selected = store.add_model(provider['id'], 'gpt-image-1.5')
    store.add_member(group['id'], store.ensure_model_agent(selected['id'])['id'])
    store.update_settings({'image_provider_id': 'no-longer-configured'})
    out = await draw(orch, store, group, {'model_id': selected['id']})
    assert out.ok


async def test_generating_member_cannot_redirect_itself_to_another_model(env):
    orch, store, group, provider, server = env
    members = []
    for name in ['gpt-image-1', 'gpt-image-1.5']:
        row = store.add_model(provider['id'], name)
        agent = store.ensure_model_agent(row['id'])
        store.add_member(group['id'], agent['id'])
        members.append(agent)
    ctx = await orch.toolhub.context(store.get_group(group['id']), members[0])
    out = await orch.toolhub.call(ctx, 'generate_image', {'prompt': 'Synthetic', 'model_id': members[1]['model_id']})
    assert not out.ok and not server.paths


def test_generator_member_preserves_selected_references_without_inheriting_unrelated_plan_images(env, monkeypatch):
    from app.orchestrator import RunState
    orch, _, group, provider, _ = env
    monkeypatch.setattr(orch, '_media_refs', lambda *args: {'reference_images': ['portrait.png']})
    target = {'use': 'image', 'provider': provider}
    run = RunState(group['id'], 'Synthetic image')
    args = orch._media_arguments(group, run, target, 'Synthetic image', {
        'plan_id': 'synthetic', 'arguments': {'prompt': 'Synthetic', 'reference_images': ['diagram.png']}})
    assert args['reference_images'] == ['diagram.png']
    args = orch._media_arguments(group, run, target, 'Synthetic image', {'plan_id': 'synthetic'})
    assert 'reference_images' not in args
    args = orch._media_arguments(group, run, target, 'Synthetic image', {})
    assert args['reference_images'] == ['portrait.png']


async def test_media_adapter_without_upload_refuses_before_submit(tmp_path):
    refs = imagegen.read_references(tmp_path, [source(tmp_path).name])
    with pytest.raises(imagegen.ImageError, match='upload|上传'):
        await imagegen.generate({'kind': 'metachat_media'}, {}, max_bytes=1000, deadline_s=2,
                                reference_images=refs)


def test_reference_size_and_count_are_bounded(tmp_path, monkeypatch):
    path = source(tmp_path)
    with pytest.raises(imagegen.ImageError):
        imagegen.read_references(tmp_path, [path.name] * 5)
    monkeypatch.setattr(imagegen, 'REFERENCE_BYTES', 5)
    with pytest.raises(imagegen.ImageError, match='20 MB'):
        imagegen.read_references(tmp_path, [path.name])


async def test_edit_failure_never_retries_as_text_only(tmp_path):
    refs = imagegen.read_references(tmp_path, [source(tmp_path).name])
    paths = []
    def fail(request):
        paths.append(request.url.path)
        return httpx.Response(400, json={'error': {'message': 'model does not support editing'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(imagegen.ImageError):
            await imagegen.generate({'base_url': 'https://example.invalid/v1'}, {'model': 'synthetic'},
                                    max_bytes=1000, deadline_s=2, reference_images=refs, client=client)
    assert paths == ['/v1/images/edits']
