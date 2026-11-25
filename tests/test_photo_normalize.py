import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.photo_media import telegram_photo
from app.video_compress import FFMPEG
from tests.test_outbox import fixture,event

@pytest.fixture
def webp():
    return (Path(__file__).parent/'fixtures/synthetic-max-album.webp').read_bytes()

@pytest.mark.asyncio
async def test_webp_normalizes_to_jpeg(webp):
    data=await telegram_photo(webp)
    assert data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9')
    assert await telegram_photo(data)==data
    assert await telegram_photo(b'RIFF\x00\x00\x00\x00WEBPbroken') is None

@pytest.mark.asyncio
async def test_album_persists_normalized_images_before_api(tmp_path,webp):
    c,s,state,db=fixture(tmp_path);m=event('webp-album');m.attaches=[{'_type':'PHOTO','url':f'https://i.oneme.ru/{i}'} for i in range(3)]
    c.download_file=AsyncMock(return_value=webp)
    async def send(**kw):
        key=db.job_key(42,'webp-album');row=db._part(key,0)
        assert row['state']=='SENDING'
        assert all(x.startswith(b'\xff\xd8') for x in s._unpack_album(row['media']))
        assert all(x.media.input_file_content.startswith(b'\xff\xd8') for x in kw['media'])
        return [SimpleNamespace(message_id=i) for i in (101,102,103)]
    s._bot.send_media_group=AsyncMock(side_effect=send)
    await c._on_message_cb(m)
    assert db.counts()=={'SENT':1}
    await c._on_message_cb(m);s._bot.send_media_group.assert_awaited_once();db.close()

@pytest.mark.asyncio
async def test_invalid_webp_fails_without_dispatch(tmp_path):
    c,s,state,db=fixture(tmp_path);m=event('broken-webp');m.attaches=[{'_type':'PHOTO','url':'https://i.oneme.ru/1'}]
    c.download_file=AsyncMock(return_value=b'RIFF\x00\x00\x00\x00WEBPbroken');s._bot.send_photo=AsyncMock()
    await c._on_message_cb(m);assert db.counts()=={'FAILED':1};s._bot.send_photo.assert_not_awaited();db.close()


@pytest.mark.asyncio
async def test_album_real_ptb_multipart_has_one_matching_file_per_item(tmp_path,webp):
    import json
    from telegram.request._requestparameter import RequestParameter
    from telegram.request._requestdata import RequestData
    c,s,state,db=fixture(tmp_path);m=event('album-wire');m.attaches=[{'_type':'PHOTO','url':f'https://i.oneme.ru/{i}'} for i in range(3)]
    c.download_file=AsyncMock(return_value=webp)
    async def send(**kw):
        rd=RequestData([RequestParameter.from_input('media',kw['media'])])
        entries=json.loads(rd.json_parameters['media']);assert len(entries)==3
        assert all(e['type']=='photo' and e['media'].startswith('attach://') for e in entries)
        names=[e['media'].removeprefix('attach://') for e in entries]
        assert len(set(names))==3 and set(names)==set(rd.multipart_data)
        for i,name in enumerate(names):
            filename,content,mimetype=rd.multipart_data[name]
            assert filename==f'photo-{i+1}.jpg' and content.startswith(b'\xff\xd8') and mimetype=='image/jpeg'
        return [SimpleNamespace(message_id=i) for i in (101,102,103)]
    s._bot.send_media_group=AsyncMock(side_effect=send)
    await c._on_message_cb(m);assert db.counts()=={'SENT':1}
    assert state.tg_for_max_message(42,'album-wire')==101
    db.close()
