(function () {
    'use strict';

    const imageCache = new Map();
    let active = null;
    let animationFrame = null;

    function context() {
        return window.JYD_TEMPLATE_PREVIEW_CONTEXT;
    }

    function node(id) {
        return document.getElementById(id);
    }

    function number(value, fallback = 0) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function range(segment) {
        const value = segment?.target_timerange || {};
        return {
            start: Math.max(0, number(value.start)),
            duration: Math.max(0, number(value.duration))
        };
    }

    function materialIndex(manifest) {
        const result = new Map();
        Object.entries(manifest?.materials || {}).forEach(([group, values]) => {
            (values || []).forEach((value) => {
                if (value?.id) result.set(String(value.id), { ...value, __group: group });
            });
        });
        return result;
    }

    function captionSource(manifest, materials) {
        const track = (manifest?.tracks || []).find((item) =>
            String(item?.id || '') === String(manifest?.caption_track_id || '')
        );
        const segment = (track?.segments || [])[0];
        if (!segment) return null;
        let material = materials.get(String(segment.material_id || ''));
        if (!material?.content?.styles?.length) {
            const nestedId = (material?.text_info_resources || [])
                .map((item) => String(item?.text_material_id || ''))
                .find(Boolean);
            if (nestedId) material = materials.get(nestedId);
        }
        const style = (material?.content?.styles || [])[0];
        if (!material || !style) return null;
        return { track, segment, material, style };
    }

    function captionTrackIds(manifest) {
        const values = Array.isArray(manifest?.caption_track_ids)
            ? manifest.caption_track_ids
            : [manifest?.caption_track_id];
        return new Set(values.map((value) => String(value || '')).filter(Boolean));
    }

    function cssColor(value, fallback = '') {
        if (!Array.isArray(value) || value.length < 3) return fallback;
        const channels = value.slice(0, 3).map((entry) =>
            Math.round(Math.max(0, Math.min(1, number(entry))) * 255)
        );
        const alpha = value.length > 3 ? Math.max(0, Math.min(1, number(value[3], 1))) : 1;
        return alpha < 1
            ? `rgba(${channels.join(',')},${alpha})`
            : `rgb(${channels.join(',')})`;
    }

    function styleColor(value, fallback = '') {
        return cssColor(
            value?.content?.solid?.color
            || value?.solid?.color
            || value?.color,
            fallback
        );
    }

    function captionShadowCSS(source, displayedWidth) {
        const material = source?.material || {};
        const style = source?.style || {};
        if (material.has_shadow === false) return 'none';
        const styleShadow = (Array.isArray(style.shadows) ? style.shadows[0] : null)
            || style.shadow
            || {};
        const alphaFallback = number(styleShadow.alpha, Object.keys(styleShadow).length ? 0.85 : 0);
        const alpha = Math.max(0, Math.min(1, number(material.shadow_alpha, alphaFallback)));
        const distance = Math.max(0, number(material.shadow_distance, number(styleShadow.distance)));
        if (alpha <= 0 || distance <= 0) return 'none';
        const point = material.shadow_point || {};
        const pointX = number(point.x);
        const pointY = number(point.y);
        const pointLength = Math.hypot(pointX, pointY);
        const angle = number(material.shadow_angle, number(styleShadow.angle, -45)) * Math.PI / 180;
        const directionX = pointLength > 0 ? pointX / pointLength : Math.cos(angle);
        const directionY = pointLength > 0 ? -pointY / pointLength : -Math.sin(angle);
        // Jianying stores distance in 1080-wide canvas pixels. shadow_point is
        // only a direction vector; treating it as an em offset creates a full
        // duplicate glyph several pixels away from the real caption.
        const distancePx = Math.max(0.25, displayedWidth * distance / 1080);
        const smoothing = Math.max(0, number(material.shadow_smoothing, number(styleShadow.diffuse)));
        const blurPx = Math.max(0.35, distancePx * smoothing * 2);
        return `${directionX * distancePx}px ${directionY * distancePx}px ${blurPx}px rgba(0, 0, 0, ${alpha})`;
    }

    async function loadCaptionFont(source, templateId) {
        const url = String(source?.material?.browser_font_url || '');
        if (!url || !window.FontFace) return '';
        const family = `jyd-template-caption-${templateId}-${active?.manifest?.content_hash || 'current'}`;
        try {
            const face = await new FontFace(family, `url("${url}")`).load();
            document.fonts.add(face);
            return family;
        } catch (_) {
            return '';
        }
    }

    function applyCaptionStyle(layout = {}) {
        const source = active?.caption;
        const caption = layout.caption || node('video-preview-caption');
        if (!source || !caption || !layout.displayedWidth || !layout.displayedHeight) return false;
        const style = source.style || {};
        const transform = source.segment?.clip?.transform || {};
        const clipScale = transform.scale || {};
        const x = number(transform.x);
        const y = number(transform.y, -0.6);
        const scaleX = Math.max(0.1, number(clipScale.x, 1));
        const scaleY = Math.max(0.1, number(clipScale.y, 1));
        const rotation = number(transform.rotation ?? source.segment?.clip?.rotation);
        const fill = styleColor(style.fill, '#FFFFFF');
        const stroke = style.stroke || style.border || {};
        const strokeColor = styleColor(stroke, 'transparent');
        const strokeWidth = Math.max(0, number(stroke.width ?? stroke.size));
        const fontSize = Math.max(9, layout.displayedWidth * Math.max(1, number(style.size, 12)) / 220);
        const left = layout.horizontalMargin + layout.displayedWidth * (0.5 + x / 2);
        const top = layout.verticalMargin + layout.displayedHeight * (0.5 - y / 2);
        caption.style.left = `${left}px`;
        caption.style.top = `${top}px`;
        caption.style.width = `${layout.displayedWidth * 0.92}px`;
        caption.style.maxWidth = `${layout.displayedWidth * 0.92}px`;
        caption.style.fontSize = `${fontSize}px`;
        caption.style.fontFamily = active.fontFamily ? `"${active.fontFamily}", sans-serif` : 'sans-serif';
        caption.style.fontWeight = style.bold ? '700' : '400';
        caption.style.fontStyle = style.italic ? 'italic' : 'normal';
        caption.style.textDecoration = style.underline ? 'underline' : 'none';
        caption.style.color = fill;
        caption.style.lineHeight = String(Math.max(1, 1 + number(source.material?.line_spacing)));
        caption.style.webkitTextStroke = strokeWidth > 0
            ? `${Math.max(1, layout.displayedWidth * strokeWidth / 220)}px ${strokeColor}`
            : '0px transparent';
        caption.style.textShadow = captionShadowCSS(source, layout.displayedWidth);
        caption.style.backgroundColor = styleColor(style.background || style.background_color, 'transparent');
        caption.style.transform = `translate(-50%, -50%) rotate(${-rotation}deg) scale(${scaleX}, ${scaleY})`;
        return true;
    }

    function effectKind(material) {
        const value = `${material?.name || ''} ${material?.effect_id || ''} ${material?.resource_id || ''}`.toLowerCase();
        if (value.includes('萤火') || value.includes('firefl') || value.includes('7399493359015890228')) return 'fireflies';
        if (value.includes('雪') || value.includes('snow')) return 'snow';
        if (value.includes('星') || value.includes('闪耀') || value.includes('spark')) return 'sparkles';
        if (value.includes('暗角') || value.includes('vignette')) return 'vignette';
        if (value.includes('颗粒') || value.includes('噪点') || value.includes('grain')) return 'grain';
        if (value.includes('闪白') || value.includes('闪光') || value.includes('flash')) return 'flash';
        return '';
    }

    function classify(manifest, materials, captionTracks) {
        let supported = 0;
        const unsupported = new Set();
        (manifest?.tracks || []).forEach((track) => {
            if (captionTracks.has(String(track.id || ''))) return;
            (track.segments || []).forEach((segment) => {
                const material = materials.get(String(segment.material_id || ''));
                if (!material) return;
                if (track.type === 'text' && material.content?.text) supported += 1;
                else if (track.type === 'effect') {
                    if (effectKind(material)) supported += 1;
                    else unsupported.add(material.name || material.effect_id || '未知特效');
                } else if (material.browser_asset_url) supported += 1;
                else if (!['video', 'audio'].includes(track.type)) unsupported.add(material.name || track.name || track.type || '未知素材');
            });
        });
        return { supported, unsupported: Array.from(unsupported) };
    }

    function setStatus(message, warning = false) {
        const status = node('video-preview-template-status');
        if (!status) return;
        status.textContent = message;
        status.classList.toggle('hidden', !message);
        status.classList.toggle('text-amber-200', warning);
        status.classList.toggle('text-cyan-100', !warning);
        status.classList.toggle('border-amber-400/30', warning);
        status.classList.toggle('border-cyan-400/30', !warning);
    }

    function reset() {
        if (animationFrame !== null) cancelAnimationFrame(animationFrame);
        animationFrame = null;
        active = null;
        const caption = node('video-preview-caption');
        if (caption) {
            caption.style.fontStyle = 'normal';
            caption.style.textDecoration = 'none';
            caption.style.backgroundColor = 'transparent';
            caption.style.textShadow = 'none';
        }
        const canvas = node('video-preview-template-canvas');
        if (canvas) {
            const drawing = canvas.getContext('2d');
            drawing?.clearRect(0, 0, canvas.width, canvas.height);
            canvas.classList.add('hidden');
        }
        setStatus('');
    }

    async function prepare() {
        const api = context();
        const binding = api?.getProject()?.settings?.jianying_template;
        if (!api?.isBrowserPreview?.() || !binding?.template_id) {
            reset();
            return;
        }
        const templateId = String(binding.template_id);
        setStatus('正在解析剪映模板 JSON…');
        try {
            const manifest = await api.api(
                `/api/new/jianying-templates/${encodeURIComponent(templateId)}/browser-preview`
            );
            if (String(api.getProject()?.settings?.jianying_template?.template_id || '') !== templateId) return;
            const materials = materialIndex(manifest);
            const captionTracks = captionTrackIds(manifest);
            const summary = classify(manifest, materials, captionTracks);
            const caption = captionSource(manifest, materials);
            if (!caption) throw new Error('模板中没有可解析的字幕样式');
            active = {
                templateId,
                manifest,
                materials,
                summary,
                caption,
                captionTracks,
                fontFamily: ''
            };
            active.fontFamily = await loadCaptionFont(caption, templateId);
            node('video-preview-template-canvas')?.classList.remove('hidden');
            setStatus('');
            api?.refreshCaptionLayout?.();
            render();
        } catch (error) {
            active = null;
            node('video-preview-template-canvas')?.classList.add('hidden');
            setStatus(`模板 JSON 预览失败：${error.message}`, true);
        }
    }

    function seed(text) {
        let value = 2166136261;
        for (const character of String(text || '')) {
            value ^= character.charCodeAt(0);
            value = Math.imul(value, 16777619);
        }
        return value >>> 0;
    }

    function random(value) {
        const x = Math.sin(value * 12.9898 + 78.233) * 43758.5453;
        return x - Math.floor(x);
    }

    function segmentAlpha(segment, templateTimeUs) {
        const value = range(segment);
        const local = templateTimeUs - value.start;
        const edge = Math.min(250000, value.duration / 4);
        if (edge <= 0) return 1;
        return Math.max(0, Math.min(1, local / edge, (value.duration - local) / edge));
    }

    function drawParticles(ctx, rect, segment, material, time, kind) {
        const base = seed(`${segment.id}:${material.id}`);
        const count = kind === 'snow' ? 54 : 38;
        const speed = Math.max(0.2, number((material.adjust_params || []).find((item) => item.name === 'effects_adjust_speed')?.value, 0.45));
        ctx.save();
        ctx.globalCompositeOperation = kind === 'snow' ? 'source-over' : 'screen';
        for (let index = 0; index < count; index += 1) {
            const a = random(base + index * 17);
            const b = random(base + index * 31 + 5);
            const c = random(base + index * 47 + 11);
            const drift = Math.sin(time * (0.35 + c) * speed + index) * 0.04;
            const progress = (b + time * speed * (kind === 'snow' ? 0.035 : -0.012) * (0.5 + c)) % 1;
            const normalizedY = progress < 0 ? progress + 1 : progress;
            const x = rect.x + rect.width * Math.max(0, Math.min(1, a + drift));
            const y = rect.y + rect.height * normalizedY;
            const radius = Math.max(1, rect.width * (kind === 'snow' ? 0.004 : (0.003 + c * 0.007)));
            const pulse = 0.3 + 0.7 * Math.abs(Math.sin(time * (1.2 + c * 2) + index));
            if (kind === 'snow') {
                ctx.fillStyle = `rgba(255,255,255,${0.25 + pulse * 0.55})`;
            } else {
                const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius * 3);
                gradient.addColorStop(0, `rgba(255,255,185,${pulse})`);
                gradient.addColorStop(0.3, `rgba(216,255,92,${pulse * 0.75})`);
                gradient.addColorStop(1, 'rgba(160,220,40,0)');
                ctx.fillStyle = gradient;
            }
            ctx.beginPath();
            ctx.arc(x, y, kind === 'snow' ? radius : radius * 3, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.restore();
    }

    function drawSparkles(ctx, rect, segment, material, time) {
        const base = seed(`${segment.id}:${material.id}`);
        ctx.save();
        ctx.globalCompositeOperation = 'screen';
        for (let index = 0; index < 24; index += 1) {
            const x = rect.x + rect.width * random(base + index * 19);
            const y = rect.y + rect.height * random(base + index * 37);
            const pulse = Math.max(0, Math.sin(time * (1.5 + random(base + index)) + index * 1.7));
            const radius = rect.width * (0.002 + random(base + index * 71) * 0.008) * pulse;
            ctx.strokeStyle = `rgba(255,245,195,${pulse * 0.9})`;
            ctx.lineWidth = Math.max(1, radius * 0.25);
            ctx.beginPath();
            ctx.moveTo(x - radius * 2, y); ctx.lineTo(x + radius * 2, y);
            ctx.moveTo(x, y - radius * 2); ctx.lineTo(x, y + radius * 2);
            ctx.stroke();
        }
        ctx.restore();
    }

    function drawEffect(ctx, rect, segment, material, time) {
        const kind = effectKind(material);
        if (kind === 'fireflies' || kind === 'snow') return drawParticles(ctx, rect, segment, material, time, kind);
        if (kind === 'sparkles') return drawSparkles(ctx, rect, segment, material, time);
        if (kind === 'vignette') {
            const gradient = ctx.createRadialGradient(rect.x + rect.width / 2, rect.y + rect.height / 2, rect.width * 0.15, rect.x + rect.width / 2, rect.y + rect.height / 2, rect.width * 0.72);
            gradient.addColorStop(0, 'rgba(0,0,0,0)');
            gradient.addColorStop(1, 'rgba(0,0,0,0.65)');
            ctx.fillStyle = gradient; ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
        } else if (kind === 'flash') {
            ctx.fillStyle = `rgba(255,255,255,${Math.max(0, Math.sin(time * 5)) * 0.2})`;
            ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
        } else if (kind === 'grain') {
            const base = Math.floor(time * 12) * 97;
            ctx.fillStyle = 'rgba(255,255,255,0.1)';
            for (let index = 0; index < 100; index += 1) {
                ctx.fillRect(rect.x + rect.width * random(base + index), rect.y + rect.height * random(base + index * 3), 1, 1);
            }
        }
    }

    function color(value, fallback = '#ffffff') {
        if (!Array.isArray(value) || value.length < 3) return fallback;
        const channels = value.slice(0, 3).map((entry) => Math.round(Math.max(0, Math.min(1, number(entry))) * 255));
        return `rgb(${channels.join(',')})`;
    }

    function drawText(ctx, rect, segment, material, alpha) {
        const content = material.content || {};
        const text = String(content.text || '').trim();
        if (!text) return;
        const style = (content.styles || [])[0] || {};
        const transform = segment.clip?.transform || {};
        const scale = transform.scale || {};
        const x = rect.x + rect.width * (0.5 + number(transform.x) / 2);
        const y = rect.y + rect.height * (0.5 - number(transform.y) / 2);
        const size = Math.max(9, rect.width * number(style.size, 12) / 220);
        const scaleX = Math.max(0.1, number(scale.x, 1));
        const scaleY = Math.max(0.1, number(scale.y, 1));
        const radians = number(transform.rotation ?? segment.clip?.rotation) * Math.PI / 180;
        const fill = color(style.fill?.content?.solid?.color, '#ffffff');
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(-radians);
        ctx.scale(scaleX, scaleY);
        ctx.globalAlpha = alpha;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.font = `${style.italic ? 'italic ' : ''}${style.bold === false ? 500 : 700} ${size}px sans-serif`;
        ctx.lineWidth = Math.max(1, size * 0.08);
        ctx.strokeStyle = 'rgba(0,0,0,0.75)';
        ctx.fillStyle = fill;
        text.split(/\r?\n/).forEach((line, index, lines) => {
            const offset = (index - (lines.length - 1) / 2) * size * 1.18;
            ctx.strokeText(line, 0, offset);
            ctx.fillText(line, 0, offset);
        });
        ctx.restore();
    }

    function imageFor(url) {
        if (!url) return null;
        if (imageCache.has(url)) return imageCache.get(url);
        const image = new Image();
        image.crossOrigin = 'same-origin';
        image.src = url;
        image.addEventListener('load', render, { once: true });
        imageCache.set(url, image);
        return image;
    }

    function drawAsset(ctx, rect, segment, material, alpha) {
        const image = imageFor(material.browser_asset_url);
        if (!image?.complete || !image.naturalWidth) return;
        const transform = segment.clip?.transform || {};
        const scale = transform.scale || {};
        const width = rect.width * Math.max(0.05, number(scale.x, 0.32));
        const height = width * image.naturalHeight / image.naturalWidth * Math.max(0.05, number(scale.y, 1));
        const x = rect.x + rect.width * (0.5 + number(transform.x) / 2) - width / 2;
        const y = rect.y + rect.height * (0.5 - number(transform.y) / 2) - height / 2;
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.drawImage(image, x, y, width, height);
        ctx.restore();
    }

    function render() {
        const api = context();
        const canvas = node('video-preview-template-canvas');
        const frame = node('video-preview-frame');
        const video = node('video-preview-local');
        if (!active || !canvas || !frame || !video || !api?.isBrowserPreview?.() || !video.videoWidth || !video.videoHeight) return;
        const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
        const width = Math.max(1, frame.clientWidth);
        const height = Math.max(1, frame.clientHeight);
        if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
            canvas.width = Math.round(width * ratio);
            canvas.height = Math.round(height * ratio);
        }
        const ctx = canvas.getContext('2d');
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        const fit = Math.min(width / video.videoWidth, height / video.videoHeight);
        const rect = {
            width: video.videoWidth * fit,
            height: video.videoHeight * fit,
            x: (width - video.videoWidth * fit) / 2,
            y: (height - video.videoHeight * fit) / 2
        };
        const videoDurationUs = Math.max(1, number(video.duration, 1) * 1_000_000);
        const templateDurationUs = Math.max(1, number(active.manifest.duration_us, videoDurationUs));
        const templateTimeUs = number(video.currentTime) * 1_000_000 * templateDurationUs / videoDurationUs;
        const timeline = [];
        (active.manifest.tracks || []).forEach((track, trackIndex) => {
            if (active.captionTracks.has(String(track.id || '')) || track.type === 'audio') return;
            (track.segments || []).forEach((segment) => {
                const value = range(segment);
                if (templateTimeUs < value.start || templateTimeUs >= value.start + value.duration) return;
                timeline.push({ track, trackIndex, segment, material: active.materials.get(String(segment.material_id || '')) });
            });
        });
        timeline.sort((a, b) => number(a.segment.track_render_index ?? a.segment.render_index, a.trackIndex) - number(b.segment.track_render_index ?? b.segment.render_index, b.trackIndex));
        ctx.save();
        ctx.beginPath(); ctx.rect(rect.x, rect.y, rect.width, rect.height); ctx.clip();
        timeline.forEach(({ track, segment, material }) => {
            if (!material) return;
            const alpha = segmentAlpha(segment, templateTimeUs);
            if (track.type === 'effect') drawEffect(ctx, rect, segment, material, number(video.currentTime));
            else if (track.type === 'text') drawText(ctx, rect, segment, material, alpha);
            else if (material.browser_asset_url && track.type !== 'video') drawAsset(ctx, rect, segment, material, alpha);
        });
        ctx.restore();
    }

    function animate() {
        animationFrame = null;
        render();
        const video = node('video-preview-local');
        if (active && video && !video.paused && !video.ended) animationFrame = requestAnimationFrame(animate);
    }

    function play() {
        if (!active || animationFrame !== null) return;
        animationFrame = requestAnimationFrame(animate);
    }

    function stop() {
        if (animationFrame !== null) cancelAnimationFrame(animationFrame);
        animationFrame = null;
        render();
    }

    window.JYDTemplateBrowserPreview = { applyCaptionStyle, prepare, render, play, stop, reset };
})();
