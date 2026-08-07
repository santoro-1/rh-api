from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.speech.accounts import (
    replicate_shared_custom_voice,
    save_minimax_config,
    synchronize_shared_custom_voices,
)
from app.services.speech.workbench_voices import delete_workbench_voice
from tests.conftest import create_user, login


_OFFICIAL_ITEMS = [
    {
        "voice_id": "Chinese (Mandarin)_Reliable_Executive",
        "voice_name": "Reliable Executive",
        "description": ["Chinese Mandarin"],
    },
    {
        "voice_id": "Chinese (Mandarin)_Warm_Girl",
        "voice_name": "Warm Girl",
        "description": ["Chinese Mandarin"],
    },
    {
        "voice_id": "Chinese (Mandarin)_Unrestrained_Young_Man",
        "voice_name": "Unrestrained Young Man",
        "description": ["Chinese Mandarin"],
    },
]


def _configure(username: str, api_key: str):
    user = create_user(username)
    with SessionLocal() as db:
        attached = db.query(User).filter_by(username=username).one()
        config = save_minimax_config(
            db,
            attached,
            api_key=api_key,
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.commit()
        return config.id, config.account_binding_id


def _custom_voice(user: User, provider_voice_id: str) -> MiniMaxVoiceAsset:
    config = user.minimax_config
    return MiniMaxVoiceAsset(
        id=str(uuid.uuid4()),
        user_id=user.id,
        config_id=config.id,
        name="共享克隆音色",
        voice_id=provider_voice_id,
        account_binding_id=config.account_binding_id,
        credential_fingerprint=config.credential_fingerprint,
        status=VoiceAssetStatus.READY.value,
        method="clone",
        is_saved=True,
    )


def test_same_api_key_materializes_existing_custom_voice_with_local_binding():
    first_config_id, first_binding = _configure("shared-minimax-a", "same-key")
    with SessionLocal() as db:
        first = db.query(User).filter_by(username="shared-minimax-a").one()
        db.add(_custom_voice(first, "SharedProviderVoice01"))
        db.commit()

    second_config_id, second_binding = _configure("shared-minimax-b", "same-key")
    _, isolated_binding = _configure("shared-minimax-c", "different-key")

    assert first_binding != second_binding
    assert isolated_binding != first_binding
    with SessionLocal() as db:
        shared = db.query(MiniMaxVoiceAsset).filter_by(
            config_id=second_config_id,
            voice_id="SharedProviderVoice01",
        ).one()
        assert shared.user.username == "shared-minimax-b"
        assert shared.account_binding_id == second_binding
        assert shared.id != db.query(MiniMaxVoiceAsset).filter_by(
            config_id=first_config_id,
            voice_id="SharedProviderVoice01",
        ).one().id
        assert db.query(MiniMaxVoiceAsset).filter_by(
            account_binding_id=isolated_binding,
            voice_id="SharedProviderVoice01",
        ).count() == 0


def test_new_custom_voice_and_activation_propagate_but_deletion_stays_local():
    first_config_id, binding = _configure("shared-later-a", "later-key")
    second_config_id, second_binding = _configure("shared-later-b", "later-key")
    assert binding != second_binding

    with SessionLocal() as db:
        first = db.query(User).filter_by(username="shared-later-a").one()
        source = _custom_voice(first, "SharedProviderVoice02")
        db.add(source)
        db.flush()
        assert replicate_shared_custom_voice(db, source) == 1
        db.commit()

    with SessionLocal() as db:
        first_copy = db.query(MiniMaxVoiceAsset).filter_by(
            config_id=first_config_id,
            voice_id="SharedProviderVoice02",
        ).one()
        second_user = db.query(User).filter_by(username="shared-later-b").one()
        second_copy = db.query(MiniMaxVoiceAsset).filter_by(
            config_id=second_config_id,
            voice_id="SharedProviderVoice02",
        ).one()
        delete_workbench_voice(db, second_user, second_copy)
        assert first_copy.is_saved is True

    with SessionLocal() as db:
        source = db.query(MiniMaxVoiceAsset).filter_by(
            config_id=first_config_id,
            voice_id="SharedProviderVoice02",
        ).one()
        source.status = VoiceAssetStatus.ACTIVE.value
        assert replicate_shared_custom_voice(db, source) == 0
        db.commit()
        hidden_peer = db.query(MiniMaxVoiceAsset).filter_by(
            config_id=second_config_id,
            voice_id="SharedProviderVoice02",
        ).one()
        assert hidden_peer.is_saved is False
        assert hidden_peer.status == VoiceAssetStatus.READY.value


def test_shared_sync_never_copies_official_voices():
    first_config_id, binding = _configure("shared-system-a", "system-key")
    second_config_id, _ = _configure("shared-system-b", "system-key")
    with SessionLocal() as db:
        first = db.query(User).filter_by(username="shared-system-a").one()
        config = first.minimax_config
        db.add(
            MiniMaxVoiceAsset(
                id=str(uuid.uuid4()),
                user_id=first.id,
                config_id=config.id,
                name="官方音色",
                voice_id="OfficialVoice01",
                account_binding_id=binding,
                credential_fingerprint=config.credential_fingerprint,
                status=VoiceAssetStatus.ACTIVE.value,
                method="system",
                is_saved=True,
            )
        )
        db.commit()
        second = db.query(User).filter_by(username="shared-system-b").one()
        assert synchronize_shared_custom_voices(db, second) == 0
        assert db.query(MiniMaxVoiceAsset).filter_by(
            config_id=second_config_id,
            voice_id="OfficialVoice01",
        ).count() == 0
        assert db.query(MiniMaxVoiceAsset).filter_by(
            config_id=first_config_id,
            voice_id="OfficialVoice01",
        ).count() == 1


def test_shared_custom_voice_is_visible_in_workbench_and_legacy_page(
    client, monkeypatch
):
    _configure("shared-route-a", "route-key")
    _configure("shared-route-b", "route-key")
    with SessionLocal() as db:
        first = db.query(User).filter_by(username="shared-route-a").one()
        db.add(_custom_voice(first, "SharedRouteProviderVoice"))
        db.commit()

    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": _OFFICIAL_ITEMS,
    )
    token_response = client.post(
        "/api/auth/center/login",
        json={"username": "shared-route-b", "password": "password123"},
    )
    assert token_response.status_code == 200
    token = token_response.json()["access_token"]
    workbench = client.post(
        "/api/workbench/voices", json={"access_token": token}
    )
    assert workbench.status_code == 200, workbench.text
    custom = [
        voice for voice in workbench.json()["voices"] if voice["source"] == "custom"
    ]
    assert [(voice["provider_voice_id"], voice["name"]) for voice in custom] == [
        ("SharedRouteProviderVoice", "共享克隆音色")
    ]

    login(client, "shared-route-b")
    legacy_page = client.get("/generate/batch")
    assert legacy_page.status_code == 200
    assert "共享克隆音色" in legacy_page.text


def test_key_rotation_keeps_voice_shareable_but_explicit_account_switch_does_not():
    first_config_id, original_binding = _configure("shared-rotation-a", "old-key")
    with SessionLocal() as db:
        first = db.query(User).filter_by(username="shared-rotation-a").one()
        db.add(_custom_voice(first, "RotatedProviderVoice"))
        db.commit()

        config = save_minimax_config(
            db,
            first,
            api_key="rotated-key",
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.commit()
        assert config.id == first_config_id
        assert config.account_binding_id == original_binding

    second_config_id, _ = _configure("shared-rotation-b", "rotated-key")
    with SessionLocal() as db:
        assert db.query(MiniMaxVoiceAsset).filter_by(
            config_id=second_config_id,
            voice_id="RotatedProviderVoice",
        ).count() == 1

        first = db.query(User).filter_by(username="shared-rotation-a").one()
        switched = save_minimax_config(
            db,
            first,
            api_key="brand-new-account-key",
            base_url="https://api.minimax.io",
            requests_per_minute=20,
            start_new_account_binding=True,
        )
        db.commit()
        assert switched.account_binding_id != original_binding
        assert synchronize_shared_custom_voices(db, first) == 0
        assert db.query(MiniMaxVoiceAsset).filter_by(
            config_id=first_config_id,
            account_binding_id=switched.account_binding_id,
            voice_id="RotatedProviderVoice",
        ).count() == 0
