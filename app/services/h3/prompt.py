from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.h3.duration import H3_MAX_REQUEST_SECONDS


H3_PROMPT_TEMPLATE_VERSION = "h3.prompt.ref2va.v8"
H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION = "h3.prompt.ref2va.loop_anchor.v1"
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
    all_pictures = _picture_labels(1, request.identity_image_count)
    facial_pictures = _picture_labels(2, request.identity_image_count)
    facial_definition = (
        f" {facial_pictures} provide additional high-detail evidence of the same person's facial identity and "
        "distinctive smiling appearance, including eye shape while smiling, cheek movement, mouth-corner shape, "
        "teeth appearance, and natural smile asymmetry."
        if facial_pictures
        else ""
    )
    picture_one_role = (
        "<Picture 1> is the primary visual anchor for the complete target video. <Picture 6> provides the opening frame "
        "of [Shot 1] for this continuous segment."
        if request.has_continuity_anchor
        else "<Picture 1> is the primary visual anchor for the complete target video and the first frame of [Shot 1]."
    )
    subject_lines = [
        (
            f"<Subject 1> is the same single person defined jointly by {all_pictures}. <Picture 1> establishes the "
            "complete on-screen identity, facial appearance, hairstyle, wardrobe, accessories, body appearance, and "
            f"current styling.{facial_definition}"
        ),
        (
            "<Subject 2> is the environment, lighting, viewpoint, shot size, framing, subject scale, and composition "
            "established by <Picture 1>."
        ),
        (
            "<Subject 3> is the natural speaking-performance language demonstrated in <Video 1>, including facial-"
            "expression cadence, natural eye and eyebrow changes, head-and-shoulder movement, gesture rhythm and "
            "amplitude, hand-use habits, and coordinated upper-body movement. Exact expressions and gestures adapt "
            "naturally to the new dialogue."
        ),
        picture_one_role,
        (
            "<Audio 1> is the complete uploaded Mandarin segment audio physically spoken by <Subject 1> (S1) and "
            "reused as the complete final audio track with its exact dialogue, voice, pauses, rhythm, pace, tone, "
            "and delivery."
        ),
    ]
    retention_lines = [
        (
            "<Subject 1> (appears throughout [Shot 1]): fully_preserved - retain the complete appearance and current "
            "styling established by <Picture 1>, with the same person's facial identity and distinctive smiling "
            "characteristics informed by all supplied pictures."
        ),
        (
            "<Subject 2> (appears throughout [Shot 1]): fully_preserved - retain the environment, lighting, viewpoint, "
            "shot size, framing, subject scale, and composition established by <Picture 1>."
        ),
        (
            "<Subject 3> (appears throughout [Shot 1]): partially_preserved - retain the natural expression cadence, "
            "head-and-shoulder movement, gesture habits, and upper-body coordination demonstrated in <Video 1>, while "
            "adapting the performance to <Audio 1>."
        ),
        (
            "<Picture 1> (primary visual anchor): fully_preserved - retain its complete person, styling, environment, "
            "lighting, viewpoint, framing, subject scale, and composition."
        ),
    ]
    opening_anchor = "<Picture 6>" if request.has_continuity_anchor else "<Picture 1>"
    spoken_dialogue = _spoken_dialogue_description(
        segment_text,
        include_dialogue_transcript=request.include_dialogue_transcript,
        is_final_segment=request.segment_index == request.segment_count - 1,
    )
    detailed = (
        "Use a realistic, polished natural-talking style in one continuous shot. "
        f"[Shot 1] The shot begins from {opening_anchor}. <Subject 1> appears with the complete identity, hairstyle, "
        "wardrobe, accessories, body appearance, and current styling established by <Picture 1>, inside <Subject 2>. "
        "The camera remains locked throughout the shot. The viewpoint, shot size, framing, subject scale, and composition "
        "remain stable and visually consistent. <Subject 3> guides the person's natural expression cadence, head-and-"
        "shoulder movement, gesture habits, and coordinated upper-body performance. "
        f"{spoken_dialogue} Preserve the naturally balanced mouth proportions, facial identity, and "
        "distinctive smile characteristics. Clear Mandarin articulation uses restrained realistic motion. Maintain "
        "breathing, eye motion, posture adjustment, and continuous natural upper-body micro-movement. The performance "
        "is mature, confident, composed, positive, energetic, and naturally authoritative, with steady camera-facing "
        "gaze. Gestures respond naturally to meaning and rhythm. Movement continues through the final frame while "
        "identity, styling, scene, composition, and quality remain stable. The frame remains clean, natural, and "
        "unobstructed."
    )
    summary = (
        "[keyframe completion + reference generation + audio reuse] A locked-camera single-shot spoken performance "
        "using <Picture 1> as the primary visual anchor. <Subject 1> and <Subject 2> define the stable visual result, "
        "while <Subject 3> guides the adapted natural performance. <Audio 1> is the complete final track for segment "
        f"{request.segment_index + 1} of {request.segment_count}."
    )
    if user_direction:
        detailed += (
            f" Additional user direction: {user_direction}. This direction remains subordinate to the established "
            "identity, styling, scene, locked composition, audio reuse, and natural performance."
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
        "and composition remain stable and visually consistent. <Subject 3> guides the person's natural expression "
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
    if request.identity_image_count >= 2:
        subject_lines, retention_lines, summary, detailed = _picture_guided_sections(
            request, segment_text, user_direction
        )
    else:
        subject_lines, retention_lines, summary, detailed = _video_guided_sections(
            request, segment_text, user_direction
        )

    if request.has_continuity_anchor:
        subject_lines.append(
            "<Picture 6> is the previous segment's final visible frame and a soft [Shot 1] opening anchor for pose, "
            "expression, framing, and environment."
        )
        retention_lines.append(
            "<Picture 6> ([Shot 1] soft opening keyframe anchor): partially_preserved - begin close to its pose, "
            "expression, framing, and environment, then move naturally forward."
        )
    retention_lines.append("<Audio 1>: fully_copy - reuse it 1:1 as the complete final audio track.")

    prompt = "\n\n".join(
        [
            "subject_definitions:\n" + "\n".join(subject_lines),
            "summary:\n" + summary,
            "retention_analysis:\n" + "\n".join(retention_lines),
            "detailed_description:\n" + detailed,
            "overall_soundscape:\n<Audio 1> is the complete final audio track.",
            "non_diegetic_music:\nN/A",
        ]
    )
    if len(prompt) > H3_MAX_PROMPT_CHARS:
        raise ValueError(
            f"H3 编译后 Prompt 不能超过 {H3_MAX_PROMPT_CHARS} 个字符（当前 {len(prompt)}）"
        )
    return prompt


def compile_loop_anchor_ref2va_prompt(request: H3PromptRequest) -> str:
    """Compile the Ref2VA profile that uses Picture 1 at both boundaries."""

    segment_text, user_direction = _validate_request(request)
    if request.identity_image_count < 1:
        raise ValueError("H3 首尾同图模式至少需要 1 张参考图")
    if request.has_continuity_anchor:
        raise ValueError("H3 首尾同图模式不能同时使用 soft_chain 连续性锚点")

    all_pictures = _picture_labels(1, request.identity_image_count)
    supporting_pictures = _picture_labels(2, request.identity_image_count)
    supporting_description = (
        f" {supporting_pictures} provide additional high-detail evidence of the same person's facial identity only; "
        "they must not override the wardrobe, accessories, environment, lighting, viewpoint, framing, pose, or "
        "composition established by <Picture 1>."
        if supporting_pictures
        else ""
    )
    subject_lines = [
        (
            f"<Subject 1> is the same single person defined jointly by {all_pictures}. <Picture 1> establishes the "
            "complete target appearance, including facial identity, hairstyle, wardrobe, accessories, body "
            f"appearance, and current styling.{supporting_description}"
        ),
        (
            "<Subject 2> is the environment, lighting, viewpoint, shot size, framing, subject scale, and composition "
            "established by <Picture 1>."
        ),
        (
            "<Subject 3> is the natural speaking-performance language demonstrated in <Video 1>, including facial-"
            "expression cadence, natural eye and eyebrow changes, head-and-shoulder movement, gesture rhythm and "
            "amplitude, hand-use habits, and coordinated upper-body movement. Exact expressions, poses, and gestures "
            "are not required to match the reference video and may adapt naturally to the new dialogue."
        ),
        (
            "<Picture 1> is the primary visual anchor for the complete target video and serves as both the first frame "
            "and the final frame of [Shot 1]. It establishes the shared visual boundary for every generated segment, "
            "including the same person, facial appearance, hairstyle, wardrobe, accessories, environment, lighting, "
            "viewpoint, framing, subject scale, pose, expression, gaze direction, hand visibility, posture, and overall "
            "composition."
        ),
        (
            "<Audio 1> is the complete uploaded Mandarin segment audio physically spoken by <Subject 1> (S1) and "
            "reused as the complete final audio track with its exact dialogue, voice, pauses, rhythm, pace, tone, "
            "and delivery."
        ),
    ]
    summary = (
        "[keyframe completion + reference generation + audio reuse] A locked-camera single-shot spoken performance "
        "that begins from <Picture 1>, permits a free and natural speaking performance in the middle, and returns "
        "smoothly to <Picture 1> at the final frame. <Subject 1> and <Subject 2> define the stable visual identity, "
        "wardrobe, environment, and composition, while <Subject 3> guides the adapted natural performance. <Audio 1> "
        f"is reused as the complete final audio track for segment {request.segment_index + 1} of {request.segment_count}."
    )
    retention_lines = [
        (
            "<Subject 1> (appears throughout [Shot 1]): fully_preserved - retain the same person's facial identity, "
            "hairstyle, wardrobe, accessories, body appearance, and current styling established by <Picture 1>. "
            "Additional supplied pictures provide supporting facial-identity evidence only and must not override the "
            "wardrobe, accessories, environment, lighting, framing, pose, or composition established by <Picture 1>."
        ),
        (
            "<Subject 2> (appears throughout [Shot 1]): fully_preserved - retain the environment, lighting, viewpoint, "
            "shot size, framing, subject scale, and composition established by <Picture 1>."
        ),
        (
            "<Subject 3> (appears throughout [Shot 1]): partially_preserved - retain the natural expression cadence, "
            "head-and-shoulder movement, gesture habits, hand-use habits, and upper-body coordination demonstrated in "
            "<Video 1>, while freely adapting the exact actions and performance to <Audio 1>."
        ),
        (
            "<Picture 1> ([Shot 1] first-frame and final-frame anchor): fully_preserved - the target video must begin "
            "from and end on the same person, facial appearance, hairstyle, wardrobe, accessories, environment, "
            "lighting, viewpoint, framing, subject scale, pose, expression, gaze direction, hand visibility, posture, "
            "and overall composition established by <Picture 1>."
        ),
        (
            "<Audio 1>: fully_copy - reuse it 1:1 as the complete final audio track without truncation, retiming, "
            "replacement, added dialogue, or added music."
        ),
    ]
    detailed = (
        "Use a realistic, polished natural-talking style in one continuous shot. "
        "[Shot 1] The shot begins directly from <Picture 1>. The first visible frame corresponds to <Picture 1>. "
        "<Subject 1> appears with the complete facial identity, hairstyle, wardrobe, accessories, body appearance, "
        "pose, expression, gaze direction, hand visibility, posture, and current styling established by <Picture 1>, "
        "inside <Subject 2>. The camera remains locked throughout the entire shot. The viewpoint, shot size, framing, "
        "subject scale, environment, lighting, and composition remain stable and visually consistent. "
        "From the first audible moment, <Subject 1> (S1) physically speaks using <Audio 1> and says exactly, "
        f"<d>[Chinese] {segment_text}</d> The mouth, lips, jaw, cheeks, and subtle facial muscles follow every audible "
        "word, pause, rhythm, pace, tone, and delivery in <Audio 1> accurately and naturally. The reused audio must "
        "remain complete and unchanged. During the spoken portion, <Subject 3> guides the person's natural expression "
        "cadence, eye movement, eyebrow movement, head-and-shoulder movement, gesture habits, hand-use habits, posture "
        "adjustment, breathing, and coordinated upper-body performance. The exact expressions, poses, head movements, "
        "hand movements, and gestures in the middle of the shot are not required to match <Picture 1> or <Video 1>. "
        "They may develop freely and naturally according to the meaning and rhythm of <Audio 1>, provided that the "
        "same person's identity, hairstyle, wardrobe, accessories, environment, lighting, camera viewpoint, framing, "
        "subject scale, and visual quality remain stable. Clear Mandarin articulation uses restrained and realistic "
        "facial motion. The performance is mature, confident, composed, positive, energetic, and naturally "
        "authoritative, with a generally camera-facing gaze. Gestures respond naturally to the meaning and rhythm of "
        "the speech. Avoid exaggerated head turns, extreme body movement, large changes in subject position, leaving "
        "the frame, camera movement, scene changes, wardrobe changes, accessory changes, background changes, lighting "
        "changes, or changes in shot scale. After the final audible word of <Audio 1>, during the remaining visual tail, "
        "<Subject 1> smoothly reduces the amplitude of all facial, head, hand, shoulder, and upper-body movement. The "
        "person naturally settles back toward the pose, expression, gaze direction, mouth state, hand visibility, "
        "posture, subject position, and subject scale established by <Picture 1>. The environment, lighting, viewpoint, "
        "framing, and composition remain unchanged. The final visible frame corresponds to <Picture 1> and forms the "
        "same stable visual boundary as the opening frame. The return must be gradual, physically plausible, and "
        "visually continuous. It must not truncate, retime, replace, or interfere with <Audio 1>. Do not use a cut, "
        "dissolve, transition, inserted still frame, freeze-frame effect, teleportation, abrupt pose change, sudden "
        "facial change, sudden hand movement, rapid recentering, or visible snap to reach the ending frame. The frame "
        "remains clean, natural, stable, and unobstructed through the end."
    )
    if user_direction:
        detailed += (
            f" Additional user direction: {user_direction}. This direction remains subordinate to the established "
            "identity, styling, scene, locked composition, complete audio reuse, and first-and-final-frame anchor."
        )
    prompt = "\n\n".join(
        [
            "subject_definitions:\n" + "\n".join(subject_lines),
            "summary:\n" + summary,
            "retention_analysis:\n" + "\n".join(retention_lines),
            "detailed_description:\n" + detailed,
            (
                "overall_soundscape:\n<Audio 1> is the complete final audio track. Do not add, remove, replace, extend, "
                "shorten, or retime any dialogue, ambience, sound effect, or other audible element."
            ),
            "non_diegetic_music:\nN/A",
        ]
    )
    if len(prompt) > H3_MAX_PROMPT_CHARS:
        raise ValueError(
            f"H3 编译后 Prompt 不能超过 {H3_MAX_PROMPT_CHARS} 个字符（当前 {len(prompt)}）"
        )
    return prompt
