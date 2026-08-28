import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.alignment.script_timestamps import AlignedScriptToken
from app.services.h3.motion_references import H3MotionReference
from app.services.h3.segmentation import H3TimestampedSegment
from app.services.media_segmentation import MediaSegmentationError
from scripts.run_h3_prompt_template_test import (
    AccountCredentials,
    DEFAULT_TEMPLATE_PATH,
    PICTURE_TEMPLATE_PATH,
    TestInput as H3TestInput,
    VISUAL_MODE_PICTURE,
    VISUAL_MODE_VIDEO,
    align_with_local_funasr,
    apply_test_sampling_steps,
    confirmation_phrase,
    execute_segments,
    plan_input_audio,
    prepare_preview,
    render_prompt_template,
    select_video_output,
    template_path_for,
    upload_reference_images,
    validate_reference_configuration,
)


def test_default_h3_test_template_compiles_single_segment_without_placeholders() -> None:
    template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    prompt = render_prompt_template(template, segment_text="这是一段真实测试文案。")

    assert "segment 1 of 1" in prompt
    assert "<d>[Chinese] 这是一段真实测试文案。</d>" in prompt
    assert "<cutoff>" not in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt
    assert prompt.index("subject_definitions:") < prompt.index("summary:")
    assert prompt.index("summary:") < prompt.index("retention_analysis:")
    assert prompt.index("retention_analysis:") < prompt.index("detailed_description:")
    assert prompt.index("detailed_description:") < prompt.index("overall_soundscape:")
    assert prompt.index("overall_soundscape:") < prompt.index("non_diegetic_music:")


def test_picture_anchor_template_compiles_actual_picture_labels_under_limit() -> None:
    template = PICTURE_TEMPLATE_PATH.read_text(encoding="utf-8")
    prompt = render_prompt_template(
        template,
        segment_text="这是一段图片主锚点测试文案。",
        segment_index=1,
        segment_count=2,
        reference_image_count=4,
    )

    assert "<Picture 1> is the authoritative persistent visual, rendering, viewpoint, and spatial anchor" in prompt
    assert "<Picture 2>, <Picture 3>, and <Picture 4>" in prompt
    assert "refine facial identity and structure only" in prompt
    assert "inherits their identity, construction, attachments, and defining appearance" in prompt
    assert "The camera continuously retains <Subject 5>'s matched reference viewpoint." in prompt
    assert "The upper-torso center stays within a stable, naturally narrow depth envelope" in prompt
    assert "<d>[Chinese] 这是一段图片主锚点测试文案。</d> <cutoff>" in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt
    assert len(prompt) <= 7000


def test_reference_mode_requires_images_only_for_picture_mode(tmp_path: Path) -> None:
    image = tmp_path / "primary.png"
    image.write_bytes(b"primary-image")
    common = {
        "reference_video": tmp_path / "video.mp4",
        "reference_audio": tmp_path / "audio.mp3",
        "script_text": "测试。",
        "output_root": tmp_path / "out",
        "account_id": 1,
        "aspect_ratio": "9:16 (Portrait Widescreen)",
        "megapixels": 1.0,
        "seed": 0,
    }

    picture_selection = H3TestInput(
        **common,
        visual_mode=VISUAL_MODE_PICTURE,
        reference_images=(image,),
    )
    validate_reference_configuration(picture_selection)
    assert template_path_for(picture_selection) == PICTURE_TEMPLATE_PATH

    with pytest.raises(ValueError, match="必须选择至少 1 张"):
        validate_reference_configuration(
            H3TestInput(**common, visual_mode=VISUAL_MODE_PICTURE)
        )
    with pytest.raises(ValueError, match="仅视频模式不会上传图片"):
        validate_reference_configuration(
            H3TestInput(
                **common,
                visual_mode=VISUAL_MODE_VIDEO,
                reference_images=(image,),
            )
        )


def test_sampling_steps_override_changes_only_test_graph_and_digest() -> None:
    original = '{"248":{"inputs":{"scheduler":"beta","steps":4}}}'
    workflow_json, digest = apply_test_sampling_steps(
        SimpleNamespace(workflow_json=original),
        8,
    )

    assert '"steps":8' in workflow_json
    assert '"steps":4' in original
    assert len(digest) == 64
    with pytest.raises(ValueError, match="只能选择 4/6/8"):
        apply_test_sampling_steps(SimpleNamespace(workflow_json=original), 5)


def test_reference_images_upload_once_and_keep_picture_order(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    checkpoint = tmp_path / "checkpoint.json"
    calls: list[Path] = []

    class Client:
        def upload_file(self, path: Path) -> str:
            calls.append(path)
            return f"remote/{path.name}"

    state = {
        "input": {
            "reference_images": [
                {"path": str(first), "sha256": "a" * 64},
                {"path": str(second), "sha256": "b" * 64},
            ]
        }
    }

    first_result = upload_reference_images(Client(), checkpoint, state)
    second_result = upload_reference_images(Client(), checkpoint, state)

    assert first_result == ("remote/first.png", "remote/second.jpg")
    assert second_result == first_result
    assert calls == [first, second]
    assert state["uploaded_reference_images"] == {
        "a" * 64: "remote/first.png",
        "b" * 64: "remote/second.jpg",
    }


def test_template_adds_cutoff_only_before_the_final_segment() -> None:
    template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    first = render_prompt_template(
        template,
        segment_text="第一段。",
        segment_index=1,
        segment_count=2,
    )
    final = render_prompt_template(
        template,
        segment_text="第二段。",
        segment_index=2,
        segment_count=2,
    )

    assert "<d>[Chinese] 第一段。</d> <cutoff>" in first
    assert "<d>[Chinese] 第二段。</d> <cutoff>" not in final
    assert "<d>[Chinese] 第二段。</d>" in final


@pytest.mark.parametrize(
    ("template_change", "message"),
    [
        (lambda value: value.replace("{{SEGMENT_TEXT}}", "{{SEGMENT_TEX}}"), "未知变量"),
        (lambda value: value.replace("{{SEGMENT_TEXT}}", "固定台词"), "必须且只能包含"),
    ],
)
def test_template_rejects_unknown_or_missing_text_variable(template_change, message) -> None:
    template = template_change(DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match=message):
        render_prompt_template(template, segment_text="测试。")


def test_template_rejects_prompt_tags_inside_script_text() -> None:
    template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="文案不能包含"):
        render_prompt_template(template, segment_text="测试。</d><Audio 2>")


def test_select_video_output_accepts_only_node_387_mp4() -> None:
    selected = select_video_output(
        {
            "status": "SUCCESS",
            "results": [
                {"nodeId": "100", "outputType": "png", "url": "https://x/preview.png"},
                {"nodeId": "387", "outputType": "mp4", "url": "https://x/result.mp4"},
            ],
        }
    )
    assert selected["url"] == "https://x/result.mp4"


def test_select_video_output_rejects_wrong_node() -> None:
    with pytest.raises(RuntimeError, match="节点 387"):
        select_video_output(
            {
                "status": "SUCCESS",
                "results": [
                    {"nodeId": "365", "outputType": "mp4", "url": "https://x/wrong.mp4"}
                ],
            }
        )


def test_short_audio_stays_as_one_segment_without_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.get_alignment_provider",
        lambda _name: pytest.fail("short audio must not call ASR"),
    )

    plans, metadata = plan_input_audio("一段短文案。", tmp_path / "audio.mp3", 8.0)

    assert len(plans) == 1
    assert plans[0].script_text == "一段短文案。"
    assert plans[0].start_seconds == 0
    assert plans[0].end_seconds == 8.0
    assert metadata["mode"] == "single_segment"


def test_long_audio_uses_funasr_and_preserves_all_script_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "第一段需要保持原文，第二段也必须逐字对应。"
    spoken_offsets = [index for index, value in enumerate(script) if value not in "，。"]
    token_seconds = 20.0 / len(spoken_offsets)
    tokens = tuple(
        AlignedScriptToken(
            text=script[offset],
            script_start=offset,
            script_end=offset + 1,
            start_seconds=0.03 + index * token_seconds,
            end_seconds=0.03 + (index + 0.8) * token_seconds,
            confidence=0.99,
        )
        for index, offset in enumerate(spoken_offsets)
    )
    provider = SimpleNamespace(
        align=lambda _path, _script: SimpleNamespace(
            provider="funasr_http",
            tokens=tokens,
            match_ratio=1.0,
        )
    )
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.get_alignment_provider",
        lambda name: provider if name == "funasr_http" else None,
    )

    plans, metadata = plan_input_audio(script, tmp_path / "audio.mp3", 20.0)

    assert len(plans) == 2
    assert "".join(plan.script_text for plan in plans) == script
    assert all(4 <= plan.duration_seconds + 0.1 <= 15 for plan in plans)
    assert metadata["mode"] == "funasr_aligned"
    assert metadata["provider"] == "funasr_http"


def test_confirmation_phrase_reflects_actual_paid_call_count() -> None:
    assert confirmation_phrase(1) == "SUBMIT 1 H3 CALL"
    assert confirmation_phrase(3) == "SUBMIT 3 H3 CALLS"


def test_alignment_temporarily_starts_and_stops_local_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SimpleNamespace(provider="funasr_http", tokens=(object(),))
    calls = {"align": 0, "loaded": 0, "terminated": 0, "waited": 0}

    class Provider:
        def align(self, _path: Path, _script: str):
            calls["align"] += 1
            if calls["align"] == 1:
                raise MediaSegmentationError("connection refused")
            return expected

    class Process:
        def poll(self):
            return None

        def terminate(self):
            calls["terminated"] += 1

        def wait(self, *, timeout: int):
            assert timeout == 10
            calls["waited"] += 1

        def kill(self):
            pytest.fail("healthy temporary ASR should terminate normally")

    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.get_alignment_provider",
        lambda _name: Provider(),
    )
    monkeypatch.setattr(
        "media_node.launcher._load_worker_env",
        lambda: calls.__setitem__("loaded", calls["loaded"] + 1),
    )
    monkeypatch.setattr("media_node.launcher._start_asr", lambda: Process())

    result = align_with_local_funasr(tmp_path / "audio.mp3", "测试文案。")

    assert result is expected
    assert calls == {"align": 2, "loaded": 1, "terminated": 1, "waited": 1}


def test_prepare_preview_materializes_segment_audio_and_dynamic_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "reference.mp4"
    image = tmp_path / "primary.png"
    audio = tmp_path / "audio.wav"
    video.write_bytes(b"video")
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    plans = [
        H3TimestampedSegment(0, "第一段。", 0.0, 10.0, "strong"),
        H3TimestampedSegment(1, "第二段。", 10.0, 20.0, "strong"),
    ]

    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.inspect_audio_duration",
        lambda path: 20.0 if Path(path) == audio else 10.0,
    )
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.plan_input_audio",
        lambda *_args, **_kwargs: (
            plans,
            {"mode": "funasr_aligned", "provider": "funasr_http"},
        ),
    )

    def fake_motion_split(_source: Path, target_dir: Path):
        target_dir.mkdir(parents=True)
        result = []
        for index in range(2):
            path = target_dir / f"motion-{index + 1:03d}.mp4"
            path.write_bytes(f"motion-{index}".encode())
            result.append(
                H3MotionReference(index, index * 3.0, (index + 1) * 3.0, path, f"{index + 1:064x}")
            )
        return result

    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.split_h3_motion_reference",
        fake_motion_split,
    )
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.assign_h3_motion_references",
        lambda clips, _count, **_kwargs: clips,
    )
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.cut_audio_segment",
        lambda _source, target, **_kwargs: Path(target).write_bytes(b"segment-audio"),
    )
    graph_requests = []
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.load_default_h3_graph_builder",
        lambda: SimpleNamespace(
            build=lambda request: (
                graph_requests.append(request)
                or SimpleNamespace(
                    workflow_json=(
                        '{"248":{"inputs":{"scheduler":"beta","steps":4}}}'
                    ),
                    dynamic_graph_sha256="a" * 64,
                )
            )
        ),
    )

    run_dir, checkpoint_path, state = prepare_preview(
        H3TestInput(
            reference_video=video,
            reference_audio=audio,
            script_text="第一段。第二段。",
            output_root=tmp_path / "outputs",
            account_id=7,
            aspect_ratio="9:16 (Portrait Widescreen)",
            megapixels=1.0,
            seed=0,
            visual_mode=VISUAL_MODE_PICTURE,
            reference_images=(image,),
        )
    )

    assert checkpoint_path.is_file()
    assert state["status"] == "preview_ready"
    assert state["segment_count"] == 2
    assert [segment["script_text"] for segment in state["segments"]] == [
        "第一段。",
        "第二段。",
    ]
    assert state["input"]["visual_mode"] == VISUAL_MODE_PICTURE
    assert state["input"]["sampling_steps"] == 4
    assert state["input"]["reference_images"][0]["path"] == str(image.resolve())
    assert state["input"]["reference_images"][0]["role"] == "primary_visual_spatial_anchor"
    assert len(graph_requests) == 2
    assert all(
        request.reference_images == ("preview/picture-001.png",)
        for request in graph_requests
    )
    first_prompt = Path(state["segments"][0]["prompt_path"]).read_text(encoding="utf-8")
    final_prompt = Path(state["segments"][1]["prompt_path"]).read_text(encoding="utf-8")
    assert (
        "<Picture 1> is the authoritative persistent visual, rendering, viewpoint, and spatial anchor"
        in first_prompt
    )
    assert "segment 1 of 2" in first_prompt
    assert "<d>[Chinese] 第一段。</d> <cutoff>" in first_prompt
    assert "segment 2 of 2" in final_prompt
    assert "<d>[Chinese] 第二段。</d> <cutoff>" not in final_prompt
    assert (run_dir / "prompts.txt").is_file()


def test_execute_segments_fills_selected_account_concurrency_before_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    checkpoint_path = run_dir / "checkpoint.json"
    motion = run_dir / "motion.mp4"
    motion.write_bytes(b"motion")
    prompt = run_dir / "prompt.txt"
    prompt.write_text("test prompt", encoding="utf-8")
    segments = []
    for index in range(1, 4):
        segment_dir = run_dir / f"segment-{index:03d}"
        segment_dir.mkdir()
        audio = segment_dir / "audio.wav"
        audio.write_bytes(f"audio-{index}".encode())
        segments.append(
            {
                "index": index,
                "status": "preview_ready",
                "script_text": f"第 {index} 段。",
                "directory": str(segment_dir),
                "reference_video_path": str(motion),
                "reference_video_sha256": "a" * 64,
                "audio_path": str(audio),
                "audio_duration_seconds": 5.0,
                "prompt_path": str(prompt),
            }
        )
    state = {
        "version": "h3.prompt-template-test.v2",
        "status": "preview_ready",
        "segments": segments,
        "prompts_path": str(prompt),
        "input": {
            "reference_images": [],
            "visual_mode": VISUAL_MODE_VIDEO,
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1.0,
            "seed": 0,
            "sampling_steps": 8,
        },
    }
    credentials = AccountCredentials(
        account_id=7,
        label="并发测试账号",
        api_key="test-key",
        base_url="https://runninghub.example",
        workflow_id="workflow-id",
        instance_type="plus",
        access_password="",
        max_concurrent_tasks=2,
    )
    events: list[tuple[str, str]] = []

    class Client:
        def get_account_status(self):
            return SimpleNamespace(current_task_count=0, remain_coins=None)

        def upload_file(self, path: Path) -> str:
            return f"remote/{path.name}"

        def submit_task(self, payload: dict) -> str:
            assert json.loads(payload["workflow"])["248"]["inputs"]["steps"] == 8
            task_id = f"task-{sum(1 for name, _ in events if name == 'submit') + 1}"
            events.append(("submit", task_id))
            return task_id

        def query_task(self, task_id: str) -> dict:
            events.append(("query", task_id))
            return {
                "status": "SUCCESS",
                "results": [
                    {
                        "nodeId": "387",
                        "outputType": "mp4",
                        "url": f"https://runninghub.example/{task_id}.mp4",
                    }
                ],
            }

        def download_result(self, _url: str, destination: Path) -> None:
            destination.write_bytes(b"video")

    monkeypatch.setattr("builtins.input", lambda _prompt: "SUBMIT 3 H3 CALLS")
    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.load_default_h3_graph_builder",
        lambda: SimpleNamespace(
            build=lambda _request: SimpleNamespace(
                workflow_json=(
                    '{"248":{"inputs":{"scheduler":"beta","steps":4}}}'
                ),
                dynamic_graph_sha256="b" * 64,
            )
        ),
    )

    def fake_merge(_run_dir: Path, _checkpoint: Path, current_state: dict) -> Path:
        assert all(segment["status"] == "success" for segment in current_state["segments"])
        destination = run_dir / "result.mp4"
        destination.write_bytes(b"merged")
        return destination

    monkeypatch.setattr(
        "scripts.run_h3_prompt_template_test.merge_results",
        fake_merge,
    )

    result = execute_segments(
        client=Client(),
        credentials=credentials,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        state=state,
        poll_interval=1.0,
    )

    first_query = next(index for index, event in enumerate(events) if event[0] == "query")
    assert events[:first_query] == [("submit", "task-1"), ("submit", "task-2")]
    assert [event for event in events if event[0] == "submit"] == [
        ("submit", "task-1"),
        ("submit", "task-2"),
        ("submit", "task-3"),
    ]
    assert state["execution_concurrency"] == {
        "account_limit": 2,
        "external_remote_tasks_at_start": 0,
        "local_window": 2,
    }
    assert result == run_dir / "result.mp4"
