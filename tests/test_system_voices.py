from __future__ import annotations

from app.database import SessionLocal
from app.models import MiniMaxConfig, MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.speech.minimax import MiniMaxClient
from app.services.speech.system_voices import sync_system_voices
from tests.conftest import create_user


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
