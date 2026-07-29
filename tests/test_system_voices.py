from __future__ import annotations

from app.database import SessionLocal
from app.models import MiniMaxConfig, MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.speech.minimax import MiniMaxClient
from app.services.speech.system_voices import (
    group_available_voice_assets,
    sync_system_voices,
    system_voice_category,
)
from tests.conftest import create_user, login


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(
            {
                "system_voice": [
                    {
                        "voice_id": "male-qn-jingying",
                        "voice_name": "精英青年音色",
                    }
                ],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )


def test_minimax_client_lists_provider_system_voices():
    session = _Session()
    client = MiniMaxClient(
        "test-key",
        base_url="https://api.minimaxi.com",
        session=session,
    )

    voices = client.list_voices("system")

    assert voices[0]["voice_id"] == "male-qn-jingying"
    assert session.calls[0][0] == "https://api.minimaxi.com/v1/get_voice"
    assert session.calls[0][1]["json"] == {"voice_type": "system"}


def test_system_voice_categories_use_provider_metadata():
    assert (
        system_voice_category(
            {
                "voice_id": "Chinese (Mandarin)_Reliable_Executive",
                "description": ["A standard Mandarin executive voice."],
            }
        )
        == "中文普通话"
    )
    assert (
        system_voice_category(
            {
                "voice_id": "Chinese (Cantonese)_Warm_Friend",
                "description": ["A Cantonese voice."],
            }
        )
        == "中文方言"
    )
    assert (
        system_voice_category({"voice_id": "English_Trustworthy_Man"})
        == "英语"
    )
    assert system_voice_category({"voice_id": "Japanese_Gentle"}) == "日语"
    assert system_voice_category({"voice_id": "Korean_Calm"}) == "韩语"


def test_sync_system_voice_is_saved_active_and_idempotent():
    create_user("system-voice-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="system-voice-user").one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("system-voice-key"),
            credential_fingerprint=credential_fingerprint("system-voice-key"),
            base_url="https://api.minimaxi.com",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()

        class _Client:
            @staticmethod
            def list_voices(voice_type):
                assert voice_type == "system"
                return [
                    {"voice_id": "male-qn-jingying", "voice_name": "精英青年音色"}
                ]

        first = sync_system_voices(
            db,
            user,
            _Client(),
            {"male-qn-jingying": "官方测试男声"},
        )[0]
        db.commit()
        original_id = first.id

        second = sync_system_voices(
            db,
            user,
            _Client(),
            {"male-qn-jingying": "官方测试男声（更新）"},
        )[0]
        db.commit()

        voices = db.query(MiniMaxVoiceAsset).all()
        assert len(voices) == 1
        assert second.id == original_id
        assert second.name == "官方测试男声（更新）"
        assert second.method == "system"
        assert second.status == VoiceAssetStatus.ACTIVE.value
        assert second.is_saved is True
        assert second.expires_at is None


def test_admin_can_sync_provider_system_voices_for_one_user(
    client,
    monkeypatch,
):
    create_user("voice-admin", is_admin=True)
    target = create_user("voice-target")
    other = create_user("voice-other")
    with SessionLocal() as db:
        user = db.get(User, target.id)
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("target-system-voice-key"),
            credential_fingerprint=credential_fingerprint(
                "target-system-voice-key"
            ),
            base_url="https://api.minimaxi.com",
            requests_per_minute=20,
        )
        db.add(config)
        db.commit()

    class _RouteClient:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def list_voices(voice_type):
            assert voice_type == "system"
            return [
                {
                    "voice_id": "male-qn-jingying",
                    "voice_name": "官方默认男声",
                },
                {
                    "voice_id": "female-chengshu",
                    "voice_name": "官方默认女声",
                },
                {
                    "voice_id": " Santa_Claus ",
                    "voice_name": "圣诞老人",
                },
            ]

    monkeypatch.setattr("app.routes.admin.MiniMaxClient", _RouteClient)
    login(client, "voice-admin")
    response = client.post(
        f"/admin/users/{target.id}/system-voices/sync",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "system_voice_count=3" in response.headers["location"]
    with SessionLocal() as db:
        target_voices = db.query(MiniMaxVoiceAsset).filter_by(
            user_id=target.id
        ).all()
        other_voices = db.query(MiniMaxVoiceAsset).filter_by(
            user_id=other.id
        ).all()
        assert {voice.voice_id for voice in target_voices} == {
            "male-qn-jingying",
            "female-chengshu",
            "Santa_Claus",
        }
        assert {
            voice.voice_id: voice.category for voice in target_voices
        } == {
            "male-qn-jingying": "中文普通话",
            "female-chengshu": "中文普通话",
            "Santa_Claus": "英语",
        }
        assert other_voices == []

        groups = group_available_voice_assets(target_voices)
        assert [group["label"] for group in groups] == ["中文普通话", "英语"]

        santa = next(
            voice for voice in target_voices if voice.voice_id == "Santa_Claus"
        )
        santa_id = santa.id

    delete_response = client.post(
        f"/admin/users/{target.id}/system-voices/{santa_id}/delete",
        follow_redirects=False,
    )
    assert delete_response.status_code == 303
    assert "system_voice_deleted=1" in delete_response.headers["location"]
    with SessionLocal() as db:
        santa = db.get(MiniMaxVoiceAsset, santa_id)
        assert santa.status == VoiceAssetStatus.HIDDEN.value
        assert santa.is_saved is False

    resync_response = client.post(
        f"/admin/users/{target.id}/system-voices/sync",
        follow_redirects=False,
    )
    assert resync_response.status_code == 303
    assert "system_voice_count=2" in resync_response.headers["location"]
    with SessionLocal() as db:
        santa = db.get(MiniMaxVoiceAsset, santa_id)
        assert santa.status == VoiceAssetStatus.HIDDEN.value
        assert santa.is_saved is False

    restore_response = client.post(
        f"/admin/users/{target.id}/system-voices/{santa_id}/restore",
        follow_redirects=False,
    )
    assert restore_response.status_code == 303
    assert "system_voice_restored=1" in restore_response.headers["location"]
    with SessionLocal() as db:
        santa = db.get(MiniMaxVoiceAsset, santa_id)
        assert santa.status == VoiceAssetStatus.ACTIVE.value
        assert santa.is_saved is True
        config = db.query(MiniMaxConfig).filter_by(user_id=target.id).one()
        db.add(
            MiniMaxVoiceAsset(
                id="target-custom-clone",
                user_id=target.id,
                config_id=config.id,
                name="我的克隆声音",
                voice_id="custom-clone-voice",
                account_binding_id=config.account_binding_id,
                credential_fingerprint=config.credential_fingerprint,
                status=VoiceAssetStatus.ACTIVE.value,
                method="clone",
                is_saved=True,
            )
        )
        db.commit()

    admin_page = client.get(f"/admin/users/{target.id}")
    assert admin_page.status_code == 200
    assert admin_page.text.index("<h3>我的自定义音色") < admin_page.text.index(
        '<details class="voice-category-group system-voice-library">'
    )
    assert "target-custom-clone" not in admin_page.text

    login(client, "voice-target")
    voices_page = client.get("/voices")
    assert voices_page.status_code == 200
    assert voices_page.text.index("我的自定义音色") < voices_page.text.index(
        "官方系统音色"
    )
    assert "中文普通话" in voices_page.text
    assert "英语" in voices_page.text
    batch_page = client.get("/generate/batch")
    assert batch_page.status_code == 200
    assert batch_page.text.index('optgroup label="我的自定义音色"') < (
        batch_page.text.index('optgroup label="中文普通话"')
    )
    assert '<optgroup label="中文普通话">' in batch_page.text
    assert '<optgroup label="英语">' in batch_page.text
    forbidden = client.post(
        f"/admin/users/{target.id}/system-voices/{santa_id}/delete",
        follow_redirects=False,
    )
    assert forbidden.status_code == 403
