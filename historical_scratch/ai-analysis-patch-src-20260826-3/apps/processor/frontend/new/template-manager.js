(function () {
    'use strict';

    const DRAFT_FILES = new Set([
        'draft_content.json', 'draft_meta_info.json', 'draft_agency_config.json',
        'draft_cover.jpg', 'draft_cover.png'
    ]);
    const RESOURCE_EXTENSIONS = new Set([
        'json', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'ttf', 'otf',
        'woff', 'woff2', 'mp4', 'mov', 'webm', 'bin', 'dat'
    ]);
    const state = { templates: [], selectedId: '', busy: false };
    const context = () => window.JYD_TEMPLATE_CONTEXT;
    const node = (id) => document.getElementById(id);
    const html = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    }[char]));

    function progress(message, warning = false) {
        const output = node('jianying-template-progress');
        output.textContent = message || '';
        output.classList.toggle('text-red-300', warning);
        output.classList.toggle('text-gray-500', !warning);
    }
    function setBusy(value) {
        state.busy = value;
        node('jianying-template-upload').disabled = value;
        node('jianying-template-apply').disabled = value;
    }
    function setOpen(value) {
        const modal = node('jianying-template-modal');
        const panel = modal.querySelector('.glass-panel-heavy');
        modal.classList.toggle('opacity-0', !value);
        modal.classList.toggle('pointer-events-none', !value);
        panel.classList.toggle('scale-95', !value);
        panel.classList.toggle('scale-100', value);
    }
    async function api(path, options) { return context().api(path, options); }

    async function selectedDraftFiles(handle) {
        const files = [];
        for await (const [name, child] of handle.entries()) {
            if (child.kind === 'file' && DRAFT_FILES.has(name.toLowerCase())) {
                files.push({ path: name, file: await child.getFile() });
            }
        }
        if (!files.some((entry) => entry.path.toLowerCase() === 'draft_content.json')) {
            throw new Error('所选目录没有 draft_content.json，请选择具体的剪映草稿文件夹。');
        }
        return files;
    }

    async function resourceFiles(handle, prefix = '', output = []) {
        for await (const [name, child] of handle.entries()) {
            if (output.length >= 500) throw new Error('单个花字资源超过 500 个文件，已停止读取。');
            const path = prefix ? `${prefix}/${name}` : name;
            if (child.kind === 'directory') await resourceFiles(child, path, output);
            else {
                const extension = name.includes('.') ? name.split('.').pop().toLowerCase() : 'dat';
                if (RESOURCE_EXTENSIONS.has(extension)) output.push({ path, file: await child.getFile() });
            }
        }
        return output;
    }

    async function uploadFiles(templateId, endpoint, files, extra = {}) {
        for (let index = 0; index < files.length; index += 1) {
            const entry = files[index];
            progress(`正在上传 ${index + 1} / ${files.length}：${entry.path}`);
            const query = new URLSearchParams({ ...extra, path: entry.path });
            await api(`/api/new/jianying-templates/${templateId}/${endpoint}?${query}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: entry.file
            });
        }
    }

    function status(template) {
        if (template.status === 'READY') return '<span class="text-emerald-300">可使用</span>';
        if (template.status === 'NEEDS_RESOURCES') {
            return `<span class="text-amber-300">缺少 ${template.missing_resources?.length || 0} 个花字资源</span>`;
        }
        return '<span class="text-gray-400">尚未完成</span>';
    }

    function render() {
        const cards = [`<label class="flex items-center gap-3 rounded-xl border ${state.selectedId ? 'border-slate-700/60' : 'border-brand-primary/50 bg-brand-primary/5'} px-4 py-3 cursor-pointer">
            <input type="radio" name="jianying-template-choice" value="" ${state.selectedId ? '' : 'checked'}>
            <span class="flex-1"><strong class="block text-sm text-white">不使用模板</strong><small class="text-[10px] text-gray-500">使用工作台默认后期配方</small></span>
        </label>`];
        for (const template of state.templates) {
            const checked = state.selectedId === template.template_id;
            const disabled = template.status !== 'READY';
            const hint = template.missing_resources?.[0]?.candidate_cache_paths?.[0] || '';
            cards.push(`<div class="rounded-xl border ${checked ? 'border-brand-primary/50 bg-brand-primary/5' : 'border-slate-700/60'} px-4 py-3">
                <div class="flex items-center gap-3">
                    <input type="radio" name="jianying-template-choice" value="${template.template_id}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
                    <label class="min-w-0 flex-1 cursor-pointer"><strong class="block truncate text-sm text-white">${html(template.name)}</strong><small class="text-[10px]">${status(template)}</small></label>
                    ${template.status === 'NEEDS_RESOURCES' ? `<button data-action="repair" data-id="${template.template_id}" class="px-3 py-1.5 rounded-lg bg-amber-500/10 text-amber-200 text-[10px]">修复资源</button>` : ''}
                    <button data-action="rename" data-id="${template.template_id}" class="w-8 h-8 rounded-lg text-gray-400 hover:text-white" title="重命名"><i class="fa-solid fa-pen"></i></button>
                    <button data-action="delete" data-id="${template.template_id}" class="w-8 h-8 rounded-lg text-gray-400 hover:text-red-300" title="删除"><i class="fa-solid fa-trash"></i></button>
                </div>
                ${hint ? `<p class="mt-2 pl-7 text-[10px] text-gray-500">Cache 下需要：<code class="text-amber-200">${html(hint)}</code></p>` : ''}
            </div>`);
        }
        const list = node('jianying-template-list');
        list.innerHTML = cards.join('');
        list.querySelectorAll('input[type="radio"]').forEach((radio) => radio.addEventListener('change', () => {
            state.selectedId = radio.value; render();
        }));
        list.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => {
            void runAction(button.dataset.action, button.dataset.id);
        }));
    }

    async function load() {
        state.templates = (await api('/api/new/jianying-templates')).templates || [];
        state.selectedId = String(context().getProject()?.settings?.jianying_template?.template_id || '');
        render();
    }

    async function open() {
        if (!context().getProject()) {
            context().toast('请先选择项目', '模板需要绑定到一个已经保存的项目。', 'warning'); return;
        }
        setOpen(true); progress('正在读取当前账号的模板…');
        try { await load(); progress(''); } catch (error) { progress(error.message, true); }
    }

    async function upload() {
        if (state.busy) return;
        const name = node('jianying-template-name').value.trim();
        if (!name) { progress('请先填写模板名称。', true); return; }
        if (!window.showDirectoryPicker) {
            progress('请通过 HTTPS 使用最新版 Chrome 或 Edge，以便选择草稿目录。', true); return;
        }
        let created = null;
        try {
            const handle = await window.showDirectoryPicker({ mode: 'read' });
            const files = await selectedDraftFiles(handle);
            setBusy(true);
            created = await api('/api/new/jianying-templates', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name })
            });
            await uploadFiles(created.template_id, 'draft-files', files);
            progress('正在自动识别主视频和语音字幕轨…');
            const analyzed = await api(`/api/new/jianying-templates/${created.template_id}/analyze`, { method: 'POST' });
            node('jianying-template-name').value = '';
            await load();
            if (analyzed.status === 'READY') state.selectedId = analyzed.template_id;
            render();
            progress(analyzed.status === 'READY' ? '模板上传完成，可以直接应用。' : '模板已读取，需要补充本机剪映花字缓存。');
        } catch (error) {
            if (created) try { await api(`/api/new/jianying-templates/${created.template_id}`, { method: 'DELETE' }); } catch (_) {}
            progress(error.message, true);
        } finally { setBusy(false); }
    }

    async function directoryAt(root, relative) {
        let current = root;
        for (const part of relative.replaceAll('\\', '/').split('/').filter(Boolean)) {
            current = await current.getDirectoryHandle(part);
        }
        return current;
    }

    async function repair(templateId) {
        if (!window.showDirectoryPicker) throw new Error('资源修复需要通过 HTTPS 使用最新版 Chrome 或 Edge。');
        const template = state.templates.find((item) => item.template_id === templateId);
        const cacheRoot = await window.showDirectoryPicker({ mode: 'read' });
        setBusy(true);
        try {
            for (const missing of template?.missing_resources || []) {
                let resource = null;
                for (const candidate of missing.candidate_cache_paths || []) {
                    try { resource = await directoryAt(cacheRoot, candidate); break; } catch (_) {}
                }
                if (!resource) throw new Error('在所选目录中没有找到指定资源。请选择剪映 User Data\\Cache 目录，或先在剪映中使用一次该花字。');
                const files = await resourceFiles(resource);
                if (!files.length) throw new Error('指定花字资源目录中没有可上传的素材文件。');
                await uploadFiles(templateId, 'resource-files', files, { resource_key: missing.resource_key });
            }
            const result = await api(`/api/new/jianying-templates/${templateId}/resources/complete`, { method: 'POST' });
            await load();
            progress(result.status === 'READY' ? '花字资源已补齐，模板可以使用。' : '仍有资源未找到，请检查缓存目录。', result.status !== 'READY');
        } finally { setBusy(false); }
    }

    async function runAction(action, templateId) {
        try {
            if (action === 'repair') return await repair(templateId);
            if (action === 'rename') {
                const current = state.templates.find((item) => item.template_id === templateId);
                const name = window.prompt('新的模板名称', current?.name || '');
                if (!name?.trim()) return;
                await api(`/api/new/jianying-templates/${templateId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
            } else if (action === 'delete') {
                if (!window.confirm('确认删除这个账号模板？模板副本和资源副本会一起删除。')) return;
                await api(`/api/new/jianying-templates/${templateId}`, { method: 'DELETE' });
            }
            await load(); progress('操作已保存。');
        } catch (error) { progress(error.message, true); }
    }

    async function apply() {
        const project = context().getProject();
        if (!project || state.busy) return;
        setBusy(true);
        try {
            const updated = await api(`/api/new/projects/${project.project_id}/jianying-template`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: state.selectedId })
            });
            context().syncProject(updated);
            const selected = state.templates.find((item) => item.template_id === state.selectedId);
            context().toast('剪映模板已保存', selected ? `当前项目将使用“${selected.name}”。` : '当前项目已恢复默认后期配方。', 'success');
            setOpen(false);
        } catch (error) { progress(error.message, true); } finally { setBusy(false); }
    }

    function refreshButton() {
        const button = node('btn-jianying-template');
        if (!button) return;
        const binding = context().getProject()?.settings?.jianying_template;
        button.querySelector('span').textContent = binding?.name || '剪映模板';
        button.classList.toggle('border-brand-primary/50', Boolean(binding));
        button.title = binding ? `当前模板：${binding.name}` : '选择账号保存的剪映模板';
    }

    window.JYDTemplates = { open, close: () => setOpen(false), upload, apply, refreshButton };
})();
