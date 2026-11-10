from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.h3.duration import H3_MAX_REQUEST_SECONDS


H3_PROMPT_TEMPLATE_VERSION = "h3.prompt.ref2va.v10"
H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION = "h3.prompt.ref2va.loop_anchor.v3"
H3_MANUAL_PROMPT_OVERRIDE_VERSION = "h3.prompt.manual-override.v1"
H3_MAX_PROMPT_CHARS = 7000
_RESERVED_PROMPT_SYNTAX = re.compile(
    r"(?i)(?:subject_definitions|summary|retention_analysis|detailed_description|"
    r"overall_soundscape|non_diegetic_music)\s*:|</?d>|<\s*(?:subject|picture|video|audio)\b"
)


@dataclass(frozen=True)
class H3PromptRequest:
    segment_text: str
    segment_duration_seconds: float
    segment_index: int
    segment_count: int
    identity_image_count: int = 0
    has_continuity_anchor: bool = False
    user_direction: str = ""
    include_dialogue_transcript: bool = True


def _normalized_free_text(value: object, field: str, *, max_length: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if len(text) > max_length:
        raise ValueError(f"{field}不能超过 {max_length} 个字符")
    return text


def normalize_h3_prompt_override(value: object) -> str:
    """Normalize an explicitly selected full Prompt without flattening its layout."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\x00" in text:
        raise ValueError("H3 人工总体提示词不能包含空字符")
    if len(text) > H3_MAX_PROMPT_CHARS:
        raise ValueError(
            f"H3 人工总体提示词不能超过 {H3_MAX_PROMPT_CHARS} 个字符（当前 {len(text)}）"
        )
    return text


def validate_h3_prompt_request(request: H3PromptRequest) -> None:
    """Validate the non-Prompt inputs even when the system compiler is bypassed."""

    _validate_request(request)


def _validate_request(request: H3PromptRequest) -> tuple[str, str]:
    segment_text = str(request.segment_text or "").strip()
    if not segment_text:
        raise ValueError("H3 分段台词不能为空")
    if len(segment_text) > 5000:
        raise ValueError("H3 分段台词不能超过 5000 个字符")
    if _RESERVED_PROMPT_SYNTAX.search(segment_text):
        raise ValueError("H3 分段台词包含保留的 Prompt 标签或段落语法")
    try:
        duration = float(request.segment_duration_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("H3 分段时长不合法") from exc
    if not 0 < duration <= float(H3_MAX_REQUEST_SECONDS):
        raise ValueError("H3 分段音频时长必须大于 0 且不超过 15 秒")
    if request.segment_count < 1 or not 0 <= request.segment_index < request.segment_count:
        raise ValueError("H3 分段序号不合法")
    if not 0 <= request.identity_image_count <= 5:
        raise ValueError("H3 人物参考图数量必须在 0 到 5 之间")
    if request.has_continuity_anchor and request.identity_image_count >= 6:
        raise ValueError("H3 有效参考图数量不能超过 6")
    if type(request.include_dialogue_transcript) is not bool:
        raise ValueError("H3 transcript 实验开关必须是布尔值")
    user_direction = _normalized_free_text(request.user_direction, "H3 用户补充方向", max_length=1000)
    if user_direction and _RESERVED_PROMPT_SYNTAX.search(user_direction):
        raise ValueError("H3 用户补充方向不能覆盖 Prompt 段落或引用标签")
    return segment_text, user_direction


def _spoken_dialogue_description(
    segment_text: str,
    *,
    include_dialogue_transcript: bool,
    is_final_segment: bool,
) -> str:
    endpoint = "" if is_final_segment else " <cutoff>"
    if include_dialogue_transcript:
        return (
            "From the first audible moment, <Subject 1> (S1) physically speaks using <Audio 1> and says exactly, "
            f"<d>[Chinese] {segment_text}</d>{endpoint} The mouth, lips, jaw, and subtle facial muscles follow every audible word, "
            "pause, and rhythm accurately."
        )
    description = (
        "From the first audible moment, <Subject 1> (S1) naturally speaks in precise synchronization with <Audio 1>. "
        "The mouth, lips, jaw, and subtle facial muscles follow the supplied speech timing, pauses, rhythm, pace, "
        "and delivery accurately and naturally."
    )
    return description + endpoint


def _picture_labels(start: int, end: int) -> str:
    labels = [f"<Picture {index}>" for index in range(start, end + 1)]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _picture_guided_sections(
    request: H3PromptRequest,
    segment_text: str,
    user_direction: str,
) -> tuple[list[str], list[str], str, str]:
    supporting_pictures = _picture_labels(2, request.identity_image_count)
    subject_lines = [
        (
            "<Subject 1> is the same person established by <Picture 1>, including facial identity, proportions, "
            "hairstyle, body proportions, and stable personal features."
        ),
        (
            "<Subject 2> is the same wardrobe in <Picture 1>, including layers, sleeves, neckline, closure, panels, "
            "seams, trim, markings, coverage, material, base colors, and overall color appearance."
        ),
        (
            "<Subject 3> is the same accessories in <Picture 1>, including identity, count, shape, material, color, "
            "orientation, attachment, placement, and wearing method."
        ),
        (
            "<Subject 4> is the environment and camera-original rendering in <Picture 1>, including background "
            "geometry, lighting, exposure, white balance, contrast, saturation, skin tones, wardrobe colors, and "
            "scene colors."
        ),
        (
            "<Subject 5> is the reference camera viewpoint and spatial state established by <Picture 1>: position, "
            "height, horizontal and vertical angles, focal-length appearance, perspective, image-plane orientation, "
            "shot size, crop, framing, headroom, upper-body scale, upper-torso depth anchor, subject center, "
            "background margins, and landmarks."
        ),
        (
            "<Subject 6> is the local speaking-performance language in <Video 1>: expression, eye, head, shoulder, "
            "breathing, hand, gesture, and posture rhythms adapted within <Picture 1>'s state."
        ),
        "<Picture 1> is the authoritative persistent visual, rendering, viewpoint, and spatial anchor for [Shot 1].",
        (
            "<Audio 1> is the complete uploaded Mandarin segment audio physically spoken by <Subject 1> (S1) and "
            "reused as the complete final track with its original dialogue, voice, pauses, rhythm, pace, tone, "
            "and delivery."
        ),
    ]
    if supporting_pictures:
        picture_word = "pictures" if request.identity_image_count > 2 else "picture"
        refine_word = "refine" if request.identity_image_count > 2 else "refines"
        subject_lines.insert(
            1,
            (
                f"The supporting {picture_word} {supporting_pictures} {refine_word} facial identity and structure "
                "only; <Picture 1> remains authoritative for hairstyle, body presentation, skin-tone rendering, "
                "wardrobe, accessories, environment, camera-original rendering, camera geometry, framing, and "
                "spatial scale."
            ),
        )
    retention_lines = [
        "<Subject 1> (throughout [Shot 1]): fully_preserved - the same person persists.",
        "<Subject 2> (throughout [Shot 1]): fully_preserved - the same garments and colors persist.",
        "<Subject 3> (throughout [Shot 1]): fully_preserved - the same accessories and attachments persist.",
        "<Subject 4> (throughout [Shot 1]): fully_preserved - the environment and rendering persist.",
        (
            "<Subject 5> (throughout [Shot 1]): fully_preserved - the matched viewpoint, framing, scale, depth anchor, "
            "and landmarks persist."
        ),
        (
            "<Subject 6> (guides [Shot 1]): partially_preserved - local performance is adapted within <Picture 1>'s "
            "state."
        ),
        (
            "<Picture 1> ([Shot 1] persistent anchor): fully_preserved - its authoritative role persists."
        ),
    ]
    endpoint = "" if request.segment_index == request.segment_count - 1 else " <cutoff>"
    if request.include_dialogue_transcript:
        spoken_dialogue = (
            "From the first audible moment, <Subject 1> (S1) physically speaks using <Audio 1> and says exactly, "
            f"<d>[Chinese] {segment_text}</d>{endpoint} The mouth, lips, jaw, cheeks, and facial muscles follow every "
            "word, pause, rhythm, pace, tone, and delivery accurately, coordinated with expression, breathing, head "
            "motion, and posture."
        )
    else:
        spoken_dialogue = (
            "From the first audible moment, <Subject 1> (S1) naturally speaks in precise synchronization with "
            "<Audio 1>. The mouth, lips, jaw, cheeks, and facial muscles follow the supplied speech timing, pauses, "
            "rhythm, pace, tone, and delivery accurately, coordinated with expression, breathing, head motion, and "
            f"posture.{endpoint}"
        )
    opening_description = (
        "The opening frame inherits the preceding pose and motion from <Picture 6> while retaining <Picture 1>'s "
        "reference viewpoint and composition."
        if request.has_continuity_anchor
        else (
            "The opening frame adopts <Picture 1>'s reference viewpoint and composition, matching its camera position, "
            "height, horizontal and vertical angles, focal-length appearance, perspective, image-plane orientation, "
            "shot size, crop, framing, headroom, subject scale, and landmarks."
        )
    )
    detailed = (
        "The target retains <Picture 1>'s camera-original appearance and composition.\n\n"
        f"[Shot 1] {opening_description} This matched viewpoint remains the persistent camera state through the final "
        "frame as the segment develops in one continuous shot. The person begins from a living, natural state close to "
        "<Picture 1>'s body orientation, expression, gaze, hand visibility, and posture.\n\n"
        "<Subject 1>, hairstyle, <Subject 2>, <Subject 3>, and <Subject 4> persist as the same physical entities. Each "
        "frame inherits their identity, construction, attachments, and defining appearance from the preceding frame. "
        "Motion, cloth deformation, occlusion, breathing, and lighting response develop with physical and temporal "
        "continuity.\n\n"
        "The camera continuously retains <Subject 5>'s matched reference viewpoint. Shot size, digital crop, principal "
        "body extent, headroom, upper-body scale, subject center, background margins, and landmarks remain perceptually "
        "constant. The upper-torso center stays within a stable, naturally narrow depth envelope around <Picture 1>'s "
        "anchor. Sentence boundaries, emphasis, emotions, and gestures are expressed through local performance inside "
        "this inherited frame state.\n\n"
        "Expression, gaze, head and shoulder rotation, torso turns, lateral posture adjustment, arm movement, hand "
        "gestures, breathing, and posture variation develop naturally with speech. Local depth changes and "
        "foreshortening develop around the stable upper-torso anchor while overall scale, crop, headroom, framing, and "
        "perspective retain <Picture 1>'s state.\n\n"
        "<Subject 6> contributes local timing for expression, gaze, head, shoulders, breathing, hands, gestures, and "
        "posture. Each action is re-grounded in <Picture 1>'s body position, depth anchor, scale, framing, and "
        "composition. <Picture 1> remains authoritative for identity, wardrobe, accessories, environment, rendering, "
        "camera viewpoint, shot size, crop, scale, and composition.\n\n"
        "<Subject 2> persists as the same garments with established construction, coverage, material, base colors, and "
        "overall color appearance. Movement creates continuous folds, tension, occlusion, highlights, and shadows "
        "across these garments. <Subject 3> retains its identity, count, color, orientation, attachment, placement, and "
        "wearing method while moving with the body. <Subject 4> retains its background geometry, lighting, skin tones, "
        "wardrobe colors, and scene color relationships.\n\n"
        f"{spoken_dialogue}\n\n"
        "All visible content belongs to the clean camera-original physical scene established by <Picture 1>. "
        "Communication is carried by <Audio 1>, synchronized facial articulation, gaze, expression, and natural "
        "gesture.\n\n"
        "The final moments continue from the preceding motion and softly favor a natural pose, gaze, expression, hand "
        "visibility, and posture close to <Picture 1>. Physical continuity, the matched reference viewpoint, persistent "
        "framing, stable upper-torso depth, rendering, and spatial state retain the highest priority through the final "
        "frame."
    )
    summary = (
        "[reference generation + audio reuse] The target is a single continuous speaking shot that adopts <Picture 1>'s "
        f"reference viewpoint as its persistent camera state for segment {request.segment_index + 1} of "
        f"{request.segment_count}. <Picture 1> governs appearance and composition, <Audio 1> governs speech and timing, "
        "and <Subject 6> contributes local performance."
    )
    if user_direction:
        detailed += (
            f"\n\nAdditional user direction: {user_direction}. This direction refines the current performance only and remains "
            "subordinate to the established identity, wardrobe, accessories, environment, camera-original rendering, "
            "persistent spatial composition, upper-torso depth anchor, audio reuse, and physical continuity."
        )
    return subject_lines, retention_lines, summary, detailed


def _video_guided_sections(
    request: H3PromptRequest,
    segment_text: str,
    user_direction: str,
) -> tuple[list[str], list[str], str, str]:
    face_reference = (
        " <Picture 1> supplies additional high-detail evidence of this same person's facial identity and distinctive "
        "smiling appearance only; it does not define wardrobe, environment, framing, pose, or the opening frame."
        if request.identity_image_count == 1
        else ""
    )
    subject_lines = [
        (
            "<Subject 1> is the same single person appearing in <Video 1>, including the person's stable facial "
            "identity, hairstyle, wardrobe, accessories, body proportions, and complete visible styling."
            f"{face_reference}"
        ),
        (
            "<Subject 2> is the continuous environment, lighting, viewpoint, shot size, framing, subject scale, and "
            "composition appearing in <Video 1>."
        ),
        (
            "<Subject 3> is the natural speaking-performance language demonstrated by <Subject 1> in <Video 1>, "
            "including facial-expression cadence, head-and-shoulder movement, gesture rhythm and amplitude, hand-use "
            "habits, and coordinated upper-body movement."
        ),
        (
            "<Audio 1> is the complete uploaded Mandarin segment audio physically spoken by <Subject 1> (S1) and "
            "reused as the complete final audio track with its exact dialogue, voice, pauses, rhythm, pace, tone, "
            "and delivery."
        ),
    ]
    retention_lines = [
        "<Subject 1> (appears throughout [Shot 1]): fully_preserved - retain the complete person and styling from <Video 1>.",
        (
            "<Subject 2> (appears throughout [Shot 1]): fully_preserved - retain the environment, lighting, viewpoint, "
            "shot size, framing, subject scale, and composition from <Video 1>."
        ),
        (
            "<Subject 3> (appears throughout [Shot 1]): partially_preserved - retain the natural expression cadence, "
            "head-and-shoulder movement, gesture habits, and upper-body coordination, while adapting the performance "
            "to <Audio 1>."
        ),
    ]
    spoken_dialogue = _spoken_dialogue_description(
        segment_text,
        include_dialogue_transcript=request.include_dialogue_transcript,
        is_final_segment=request.segment_index == request.segment_count - 1,
    )
    detailed = (
        "Use a realistic, polished natural-talking style in one continuous shot. [Shot 1] <Subject 1> appears inside "
        "<Subject 2>. The camera remains locked throughout the shot. The viewpoint, shot size, framing, subject scale, "
        "and composition remain stable and visually consistent. Keep the character's clothing colors unchanged throughout. "
        "Keep the character's on-screen size unchanged throughout. <Subject 3> guides the person's natural expression "
        "cadence, head-and-shoulder movement, gesture habits, and coordinated upper-body performance. "
        f"{spoken_dialogue} Clear Mandarin articulation uses restrained realistic motion. Maintain breathing, "
        "eye motion, posture adjustment, and continuous natural upper-body micro-movement. Gestures respond naturally "
        "to meaning and rhythm. Movement continues through the final frame while identity, styling, scene, composition, "
        "and quality remain stable. The frame remains clean, natural, and unobstructed."
    )
    summary = (
        "[reference generation + audio reuse] A locked-camera single-shot spoken performance preserving <Subject 1> "
        "and <Subject 2>, while <Subject 3> guides the adapted natural performance. <Audio 1> is the complete final "
        f"track for segment {request.segment_index + 1} of {request.segment_count}."
    )
    if user_direction:
        detailed += (
            f" Additional user direction: {user_direction}. This direction remains subordinate to the established "
            "identity, styling, scene, locked composition, audio reuse, and natural performance."
        )
    return subject_lines, retention_lines, summary, detailed


def compile_ref2va_prompt(request: H3PromptRequest) -> str:
    """Compile the frozen single-speaker Ref2VA profile into six official sections."""

    segment_text, user_direction = _validate_request(request)
    if request.identity_image_count >= 1:
        subject_lines, retention_lines, summary, detailed = _picture_guided_sections(
            request, segment_text, user_direction
        )
    else:
        subject_lines, retention_lines, summary, detailed = _video_guided_sections(
            request, segment_text, user_direction
        )

    if request.has_continuity_anchor:
        subject_lines.insert(
            -1,
            "<Picture 6> is the previous segment's final visible frame and a soft [Shot 1] opening anchor for pose, "
            "expression, framing, and environment.",
        )
        retention_lines.append(
            "<Picture 6> ([Shot 1] soft opening keyframe anchor): partially_preserved - begin close to its pose, "
            "expression, framing, and environment, then move naturally forward."
        )
    retention_lines.append("<Audio 1>: fully_copy - reuse it 1:1 as the complete final audio track.")

    overall_soundscape = (
        "The complete audible soundscape is <Audio 1> with its original spoken content, voice, pauses, rhythm, pace, "
        "tone, delivery, and timing."
        if request.identity_image_count >= 1
        else "<Audio 1> is the complete final audio track."
    )
    prompt = "\n\n".join(
        [
            "subject_definitions:\n" + "\n".join(subject_lines),
            "summary:\n" + summary,
            "retention_analysis:\n" + "\n".join(retention_lines),
            "detailed_description:\n" + detailed,
            "overall_soundscape:\n" + overall_soundscape,
            "non_diegetic_music:\nN/A",
        ]
    )
    if len(prompt) > H3_MAX_PROMPT_CHARS:
        raise ValueError(
            f"H3 编译后 Prompt 不能超过 {H3_MAX_PROMPT_CHARS} 个字符（当前 {len(prompt)}）"
        )
    return prompt


def compile_loop_anchor_ref2va_prompt(request: H3PromptRequest) -> str:
    """Compile loop-anchor requests with the shared Picture 1 C-version profile."""

    _validate_request(request)
    if request.identity_image_count < 1:
        raise ValueError("H3 首尾同图模式至少需要 1 张参考图")
    if request.has_continuity_anchor:
        raise ValueError("H3 首尾同图模式不能同时使用 soft_chain 连续性锚点")
    return compile_ref2va_prompt(request)
