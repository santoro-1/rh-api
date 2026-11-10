from __future__ import annotations

from pathlib import Path
import os
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "apps" / "processor" / "frontend" / "new"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class NewFrontendTest(unittest.TestCase):
    def test_h3_row_override_markup_uses_existing_escape_helper(self) -> None:
        page = (PROJECT_ROOT / "apps" / "processor" / "frontend" / "new" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("escapeAttr(", page)
        self.assertIn("escapeHtml(values.megapixels ?? '')", page)

    def test_h3_reference_video_upload_keeps_unicode_filename_out_of_headers(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("filename=${encodeURIComponent(file.name)}", page)
        self.assertNotIn("'X-Filename': file.name", page)

    def test_h3_generation_confirmation_is_minimal(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("title: '是否确认生成？'", page)
        self.assertIn("confirmText: '是'", page)
        self.assertIn("cancelText: '否'", page)
        self.assertNotIn("系统将自动切为", page)
        self.assertNotIn("仅扩大生成窗口", page)

    def test_h3_reuses_row_audio_preview_without_an_extra_review_action(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertNotIn('id="btn-review-h3-audio"', page)
        self.assertNotIn("function reviewH3TargetAudio", page)
        self.assertIn("await startGlobalH3Generation([script])", page)
        self.assertIn(
            "h3EligibleTargets.every(item => latestMinimaxAudio(item) && item.image && item.h3ReferenceVideo)",
            page,
        )
        self.assertIn("点击“生成视频”后会自动锁定当前声音", page)

    def test_h3_automatically_uses_each_rows_mapped_image(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("人物图自动跟随图片分配", page)
        self.assertIn("无需二次勾选", page)
        self.assertIn("missingImage.length", page)
        self.assertNotIn("设为 H3 人物图", page)
        self.assertNotIn("toggleH3IdentityImage", page)

    def test_workspace_keeps_audio_upload_and_generation_on_one_page(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('href="/app/new" data-workspace-nav="workspace"', page)
        self.assertNotIn('data-workspace-nav="audio"', page)
        self.assertNotIn('data-workspace-nav="generate"', page)
        self.assertIn("dataset.workspacePage = 'workspace'", page)
        self.assertIn('audio-page-only', page)
        self.assertIn('generation-page-only', page)
        self.assertNotIn('html[data-workspace-page="audio"] .generation-page-only', page)
        self.assertNotIn('html[data-workspace-page="generate"] .audio-page-only', page)
        self.assertNotIn("window.location.assign(`/app/new/generate", page)
        self.assertIn('声音、人物素材和模板可以并行准备', page)

    def test_main_workspace_exposes_only_the_multi_reference_entry(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('aria-label="当前生成方式"', page)
        self.assertIn('<span class="text-[11px] font-bold text-white">多参考</span>', page)
        self.assertIn(
            '<input id="generation-mode" type="hidden" value="minimax_h3_ref2va">',
            page,
        )
        self.assertIn("generation_mode: 'minimax_h3_ref2va'", page)
        self.assertIn("function isH3GenerationMode() {\n            return true;", page)
        self.assertIn("function isLtxGenerationMode() {\n            return false;", page)
        self.assertNotIn('<option value="runninghub_digital_human">', page)
        self.assertNotIn('<option value="ltx_lip_sync">', page)
        self.assertNotIn('id="engine-route-digital"', page)
        self.assertNotIn('id="engine-route-h3"', page)
        self.assertNotIn('id="ltx-workbench-link"', page)
        self.assertNotIn("activateStandardWorkbenchMode", page)
        self.assertNotIn("activateLtxWorkbenchMode", page)
        self.assertNotIn("activateH3WorkbenchMode", page)
        for user_facing_label in (
            "H3 多参考",
            "H3 成片",
            "H3 费用",
            "选择 H3",
            "H3 参数",
        ):
            self.assertNotIn(user_facing_label, page)

        # Archived projects can still be read, while legacy mutation routes are
        # retained only to return an explicit H3-only conflict to old clients.
        paths = {route.path for route in create_app(self.settings).routes}
        self.assertIn("/api/new/projects/{project_id}/ltx/state", paths)
        self.assertIn(
            "/api/new/projects/{project_id}/items/{item_id}/ltx/source-video",
            paths,
        )
        self.assertIn("/api/new/projects/{project_id}/ltx/generate", paths)
        self.assertIn("/api/new/projects/{project_id}/ltx/refresh", paths)

    def test_all_visible_video_actions_start_multi_reference_only(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        single_start = page.index("async function runSingleVideo")
        single_end = page.index("async function backfillRowSeedvr2", single_start)
        single = page[single_start:single_end]
        self.assertIn("await startGlobalH3Generation([script])", single)
        self.assertNotIn("startGlobalLtxGeneration", single)
        self.assertNotIn("/composition/generate", single)

        selected_start = page.index("async function generateSelectedVideos")
        selected_end = page.index("async function", selected_start + 1)
        selected = page[selected_start:selected_end]
        self.assertIn("await startGlobalH3Generation(missingBase)", selected)
        self.assertNotIn("startGlobalLtxGeneration", selected)
        self.assertNotIn("/composition/generate", selected)

        global_start = page.index("async function startGlobalFinalVideoGeneration")
        global_end = page.index("async function retryFailedCompositionItems", global_start)
        global_generation = page[global_start:global_end]
        self.assertIn("await startGlobalH3Generation()", global_generation)
        self.assertNotIn("startGlobalLtxGeneration", global_generation)
        self.assertNotIn("startGlobalComposition", global_generation)
        self.assertNotIn("retryFailedCompositionItems", global_generation)
        self.assertIn("const canBackfillSeedvr2 = false", page)

    def test_header_has_no_three_route_switcher(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('aria-label="当前生成方式"', page)
        self.assertNotIn('aria-label="工作台切换"', page)
        self.assertNotIn('>生成方式</span>', page)
        self.assertNotIn('普通数字人</button>', page)
        self.assertNotIn('视频对口型</button>', page)
        self.assertNotIn('id="workbench-environment"', page)
        self.assertNotIn("session.workbench_environment_label", page)

    def test_h3_generation_resumes_matching_quote_without_blocking_other_rows(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("async function resumeExistingH3Batch(targets)", page)
        self.assertIn("费用预览已保留", page)
        self.assertNotIn("正在恢复上一次已冻结的费用预览", page)
        self.assertIn("if (await resumeExistingH3Batch(targets)) return;", page)
        self.assertIn("const recoveredBatch = (recovered.h3_batches || []).find", page)
        self.assertIn("body: JSON.stringify({ cost_confirmed: true, batch_id: batchId })", page)
        self.assertIn("const targets = requestedTargets.filter((item) => !item.baseVideo && !h3ItemIsActive(item));", page)
        self.assertNotIn("if (await resumeExistingH3Batch()) return;", page)
        self.assertIn("const recordedMinimaxAudio = item.outputs?.minimax_audio", page)
        self.assertIn("recordedMinimaxAudio?.file_exists !== false", page)
        self.assertIn("audio: sharedMinimaxAudio", page)
        self.assertIn(
            "authoritativeAudio: h3OutputFileAvailable(item.outputs?.audio) ? item.outputs.audio : null",
            page,
        )
        self.assertIn("scheduleH3StatusPoll();", page)
        self.assertLess(
            page.index("if (await resumeExistingH3Batch(targets)) return;"),
            page.index("syncProjectInputs(await saveH3Settings(false));"),
        )

    def test_audio_regeneration_keeps_polling_instead_of_reusing_history(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn(
            "const audioGenerationActive = ['AUDIO_QUEUED', 'AUDIO_RUNNING'].includes(item.status);",
            page,
        )
        self.assertIn("|| (!audioGenerationActive", page)
        self.assertIn(
            "voiceStatus: audioGenerationActive ? item.status : (sharedMinimaxAudio ? 'AUDIO_READY' : item.status)",
            page,
        )
        self.assertIn(
            "if (['AUDIO_QUEUED', 'AUDIO_RUNNING'].includes(item?.voiceStatus)) return null;",
            page,
        )
        self.assertIn(
            "uploadedScripts.some((item) => ['AUDIO_QUEUED', 'AUDIO_RUNNING'].includes(item.voiceStatus))",
            page,
        )

    def test_partial_video_failures_keep_successful_rows_and_auto_preview(self) -> None:
        page = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("function h3StateIsActive(project = activeProject)", page)
        self.assertIn("H3_ACTIVE_ITEM_STATUSES", page)
        self.assertIn("H3_TERMINAL_REMOTE_STATUSES", page)
        self.assertIn("if (h3StateIsActive(result.project))", page)
        self.assertIn("function h3FailedSegments(project = activeProject)", page)
        self.assertIn("function h3HasPendingPostprocess(project = activeProject)", page)
        self.assertIn("function h3NeedsLocalMaterialization(project = activeProject)", page)
        self.assertIn("const hasUncachedSuccessfulSegment", page)
        self.assertIn("segment?.local_preview_ready !== true", page)
        self.assertIn("hasUncachedSuccessfulSegment", page)
        self.assertIn("remoteStatus === 'SUCCESS'", page)
        self.assertIn("function h3OutputFileAvailable(asset)", page)
        self.assertIn("asset.file_exists !== false", page)
        self.assertIn("!h3OutputFileAvailable(item?.outputs?.audio)", page)
        self.assertIn("!h3OutputFileAvailable(item?.outputs?.base_video)", page)
        self.assertIn(
            "!h3StateIsActive() && !h3HasPendingPostprocess() && !h3NeedsLocalMaterialization()",
            page,
        )
        self.assertIn("h3NeedsLocalMaterialization(existing.project)", page)
        self.assertIn("云端 H3 已完成，正在重新下载并合并已有结果，不会重复提交或付费", page)
        self.assertIn("成功行已保留并继续生成预览", page)
        self.assertIn("const h3FailureToastSignatures = new Map()", page)
        self.assertIn("function h3FailureToastSignature(failedSegments)", page)
        self.assertIn("h3FailureToastSignatures.get(projectId) !== signature", page)
        self.assertIn("h3FailureToastSignatures.delete(projectId)", page)
        self.assertIn("segment.error_message || segment.error_code", page)
        self.assertIn("{ allowRetry: false }", page)
        self.assertIn(
            "baseVideo: h3OutputFileAvailable(item.outputs?.base_video) ? item.outputs.base_video : null",
            page,
        )
        refresh = page[
            page.index("async function refreshH3Status()") :
            page.index("async function h3RetrySegment")
        ]
        self.assertLess(
            refresh.index("{ allowRetry: false }"),
            refresh.index("if (h3StateIsActive(result.project))"),
        )
        self.assertNotIn("allowedActions?.retry_postprocess", refresh)
        pending_postprocess = page[
            page.index("function h3HasPendingPostprocess") :
            page.index("function h3OutputFileAvailable")
        ]
        self.assertNotIn("retry_postprocess", pending_postprocess)
        self.assertIn("function h3RetryableSegmentsForScript(script)", page)
        retry = page[
            page.index("async function h3RetrySegment") :
            page.index("function h3RetryableSegmentsForScript")
        ]
        self.assertLess(retry.index("/h3/status"), retry.index("/retry/prepare"))
        self.assertIn("H3 批次已更新", retry)
        self.assertIn("currentSegment?.can_retry", retry)
        self.assertIn("retryH3FailedSegmentsForRow(script)", page)
        self.assertIn(
            "if (isH3GenerationMode()) {\n                void retryH3FailedSegmentsForRow(script);",
            page,
        )
        postprocess_start = page[
            page.index("async function startGlobalPostprocess") :
            page.index("function scheduleVariantStatusPoll")
        ]
        self.assertNotIn("activeProject?.allowed_actions?.start_postprocess", postprocess_start)
        composition_continuation = page[
            page.index("async function continueFinalGenerationAfterComposition") :
            page.index("// Toggle User profile dropdown")
        ]
        self.assertIn("const completedTargets = uploadedScripts.filter", composition_continuation)
        self.assertIn(
            "{ allowRetry: false }",
            composition_continuation,
        )
        self.assertNotIn("allowedActions?.retry_postprocess", composition_continuation)
        self.assertNotIn("if (failed)", composition_continuation)

    def test_workbench_pages_report_runtime_leases(self) -> None:
        runtime_script = (
            PROJECT_ROOT / "apps" / "processor" / "frontend" / "workbench-runtime.js"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/runtime/pages", runtime_script)
        self.assertIn('window.addEventListener("pagehide"', runtime_script)
        for name in ("index.html", "login.html", "gallery.html", "voice-library.html", "templates.html"):
            page = (FRONTEND_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("/app-static/workbench-runtime.js", page, name)

    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT / "runtime" / "test_tmp" / f"new_frontend_{uuid.uuid4().hex}"
        )
        self.settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
            admin_password="admin-pass",
            admin_session_secret="admin-secret",
            auth_authority=False,
            auth_server_url="http://127.0.0.1:8000",
            ltx_workbench_url="http://127.0.0.1:8792",
            execution_mode="agent",
        )
        for directory in (
            self.settings.storage_root,
            self.settings.template_library_root,
            self.settings.default_draft_root,
            self.settings.audio_library_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def test_runtime_page_lease_and_manager_shutdown_require_token(self) -> None:
        with patch.dict(
            os.environ,
            {"PUBLIC_WORKBENCH_MANAGER_TOKEN": "manager-secret"},
        ):
            with TestClient(create_app(self.settings)) as client:
                self.assertEqual(
                    client.get("/api/runtime/status").json()["active_pages"],
                    0,
                )
                opened = client.post("/api/runtime/pages")
                self.assertEqual(opened.status_code, 200)
                lease_id = opened.json()["lease_id"]
                status = client.get("/api/runtime/status").json()
                self.assertEqual(status["active_pages"], 1)
                self.assertTrue(status["seen_page"])
                self.assertEqual(
                    client.post(f"/api/runtime/pages/{lease_id}").status_code,
                    200,
                )
                self.assertEqual(
                    client.post(f"/api/runtime/pages/{lease_id}/close").status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/api/runtime/status").json()["active_pages"],
                    0,
                )
                self.assertEqual(client.post("/api/runtime/shutdown").status_code, 403)
                accepted = client.post(
                    "/api/runtime/shutdown",
                    headers={"X-Workbench-Manager-Token": "manager-secret"},
                )
                self.assertEqual(accepted.status_code, 200)
                self.assertTrue(
                    client.get("/api/runtime/status").json()["shutdown_requested"]
                )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_new_workspace_uses_real_script_and_image_input_apis(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/api/new/script-template"', html)
        self.assertIn("/api/new/script-imports/preview", html)
        self.assertIn("/metadata-import", html)
        self.assertIn("给当前批次回填文章类型和分配账号", html)
        self.assertIn("source_metadata", html)
        self.assertIn("/content-analysis", html)
        self.assertIn("retryRowContentAnalysis", html)
        self.assertIn("contentAnalysisTargetsForRows", html)
        self.assertIn(
            "const titleStatus = script.contentAnalysis?.title_analysis_status || 'NOT_REQUESTED';",
            html,
        )
        self.assertIn("titleStatus === 'NOT_REQUESTED'", html)
        self.assertIn("startContentAnalysisForChangedScripts", html)
        self.assertIn("markContentAnalysisPending", html)
        self.assertIn("scheduleContentAnalysisStatusPoll", html)
        self.assertIn("CONTENT_ANALYSIS_STATUS_POLL_INTERVAL_MS = 5000", html)
        self.assertIn("projectHasPendingContentAnalysis", html)
        self.assertIn("reconcileContentAnalysisStatusPoll(project);", html)
        self.assertIn("item?.visual_analysis?.analysis_status === 'PENDING'", html)
        self.assertIn("scheduleContentAnalysisStatusPoll(projectId, [rowId]);", html)
        self.assertIn("scheduleContentAnalysisStatusPoll(projectId, refreshItemIds);", html)
        self.assertIn("analysisStatus === 'PENDING' ? ''", html)
        self.assertIn("AI 分析中 · 等待网站返回", html)
        self.assertIn("详细阶段记录在 data/logs/workbench.log", html)
        self.assertIn("overall_status: 'PENDING',\n                    errors: {}", html)
        self.assertIn("生成声音预览和脚本分析", html)
        self.assertIn("一键下载视频", html)
        self.assertIn("一键下载声音", html)
        self.assertIn("/audios/download", html)
        self.assertIn("/videos/download", html)
        self.assertIn('id="btn-download-selected-videos"', html)
        self.assertIn("function downloadSelectedProjectVideos()", html)
        self.assertIn("const requestedIds = Array.isArray(itemIds) ? new Set(itemIds) : null", html)
        self.assertIn("/videos/download?item_ids=${itemIdsQuery}", html)
        self.assertIn("ensureProjectItemExport", html)
        self.assertIn("const candidateItems = uploadedScripts.filter", html)
        self.assertIn("const existing = candidateItems.filter", html)
        self.assertIn("正在导出并打包", html)
        self.assertIn("const readyAudioCount = rows.filter", html)
        self.assertIn("const exportableVideoCount = uploadedScripts.filter", html)
        self.assertNotIn("const downloadUrl = finalVideoUrl || localVideoUrl", html)
        self.assertIn("if (finalVideoUrl)", html)
        self.assertIn("deleteProjectItem", html)
        self.assertIn("row-delete-item", html)
        self.assertIn("{ method: 'DELETE' }", html)
        self.assertIn("已经产生的第三方费用无法撤销", html)
        self.assertNotIn("AI 智能匹配", html)
        self.assertIn("{ id: 'none', name: '无音乐', previewUrl: '' }", html)
        self.assertIn("bgm_selection_mode", html)
        self.assertIn("resolvedBgmIdentity", html)
        self.assertIn(
            "=== 'NOT_REQUESTED'",
            html,
        )
        self.assertIn(
            "startContentAnalysisForChangedScripts(projectId, rows)",
            html,
        )
        self.assertIn("displayIndex: itemIndex + 1", html)
        self.assertIn("补齐脚本分析", html)
        self.assertIn("重新映射", html)
        self.assertIn("锁定选中行为换图范围", html)
        self.assertIn("/image-mapping-scope", html)
        self.assertIn("image_mapping_target", html)
        self.assertIn("mapping.image_ids", html)
        self.assertIn("本次映射", html)
        self.assertIn("canReuseSavedResult", html)
        self.assertIn("describeAudioPlaybackError", html)
        self.assertIn("服务器文件可以读取", html)
        self.assertNotIn("正在逐行分析音乐意图和字幕语义", html)
        self.assertNotIn(
            "void analyzeProjectContent(project.project_id).then(syncProjectInputs)",
            html,
        )
        self.assertIn("/image-mapping", html)
        self.assertIn("uploadProjectImage", html)
        self.assertIn("initializeProjectInputs", html)
        self.assertIn('id="project-selector"', html)
        self.assertIn('id="project-selector-toolbar"', html)
        self.assertIn("beginNewProjectBatch", html)
        self.assertIn("switchWorkspaceProject", html)
        self.assertIn("reconcileInactiveCompositionProjects", html)
        self.assertIn("INACTIVE_COMPOSITION_SYNC_INTERVAL_MS = 60000", html)
        self.assertIn("projectHasActiveComposition(project)", html)
        self.assertIn("projectNeedsAudioSync(project)", html)
        self.assertIn("projectNeedsH3Sync(project)", html)
        self.assertIn("projectNeedsPostprocessSync(project)", html)
        self.assertIn("projectNeedsVariantSync(project)", html)
        self.assertIn("project.project_id !== activeProject?.project_id", html)
        self.assertIn(
            "workspaceApi(`/api/new/projects/${projectId}/${stage.path}`)",
            html,
        )
        self.assertIn("void reconcileInactiveCompositionProjects(false)", html)
        self.assertIn('id="delete-current-project-toolbar"', html)
        self.assertIn("deleteCurrentProjectBatch", html)
        self.assertIn('id="article-type-filter"', html)
        self.assertIn(".article-type-select option", html)
        self.assertIn("color-scheme: dark", html)
        self.assertIn("applyArticleTypeFilter", html)
        self.assertIn("filteredProjectItems", html)
        self.assertIn("const selectedItems = selectedProjectItems()", html)
        self.assertIn("item_ids: targetItems.map((item) => item.id)", html)
        self.assertIn("范围外已有声音保持不变", html)
        self.assertIn("可直接全选后设置语速或生成声音", html)
        self.assertIn("/items/batch", html)
        self.assertIn("原测试内容保持不变", html)
        self.assertIn("字幕断句已回退", html)
        self.assertIn("不调用大模型", html)
        self.assertIn("remapTargets", html)
        self.assertIn('id="select-all-project-items"', html)
        self.assertIn('class="row-select-checkbox', html)
        self.assertIn("applyQuickTaskSelection", html)
        self.assertIn("3,5,8-12", html)
        self.assertIn("analyzeSelectedRows", html)
        self.assertIn("generateSelectedAudio", html)
        self.assertIn("generateSelectedVideos", html)
        self.assertIn("waitForSelectedScriptSaves", html)
        self.assertIn("Promise.allSettled(saves)", html)
        self.assertIn("item_ids: itemIds", html)
        self.assertIn("item_ids: targets.map(item => item.id)", html)
        self.assertNotIn("simulateExcelParsing", html)
        self.assertNotIn("loadSampleData", html)
        self.assertNotIn("const sampleImages", html)
        self.assertTrue((FRONTEND_ROOT / "project-script-template.xlsx").is_file())
        self.assertIn("activeLayoutProfileConfig()?.cover", html)
        self.assertIn("coverStyle.overlay_y_ratio ?? 0.615", html)
        self.assertIn("coverStyle.overlay_height_ratio ?? 0.28", html)
        self.assertNotIn("displayedHeight * (0.609375 - 0.36 / 2)", html)

    def test_workspace_sidebar_collapses_and_table_headers_distinguish_io(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="workspace-grid"', html)
        self.assertIn('id="input-sidebar"', html)
        self.assertIn('id="processing-area"', html)
        self.assertIn('id="input-sidebar-toggle"', html)
        self.assertIn("function toggleInputSidebar()", html)
        self.assertIn("脚本总字数 0", html)
        self.assertIn("const totalScriptCharacters = uploadedScripts.reduce(", html)
        self.assertIn("Array.from(String(script.text || '')).length", html)
        self.assertIn("new-workbench-input-sidebar-collapsed", html)
        self.assertIn("input-sidebar-collapsed #processing-area", html)
        self.assertIn('class="table-flow-legend"', html)
        self.assertIn("输入 / 操作", html)
        self.assertIn("生成结果", html)
        self.assertIn("--table-header-accent: #818cf8", html)
        self.assertIn("--table-header-accent: #2dd4bf", html)
        self.assertEqual(html.count('scope="col" class="table-header-input'), 11)
        self.assertEqual(html.count('scope="col" class="table-header-output'), 3)
        self.assertIn('colspan="14"', html)
        self.assertLess(html.index(">画面</th>"), html.index(">姿态</th>"))
        self.assertLess(html.index(">姿态</th>"), html.index(">背景音乐</th>"))
        self.assertIn("handleRowLayoutProfileChange", html)
        self.assertIn("preserve_auto_bgm: config.bgmIdentity === 'auto'", html)
        self.assertIn("applyLayoutProfileToSelected('standing')", html)
        self.assertIn("applyLayoutProfileToSelected('seated')", html)
        self.assertLess(html.index(">背景音乐</th>"), html.index(">字幕样式</th>"))
        self.assertLess(html.index(">字幕样式</th>"), html.index(">语义视觉</th>"))
        self.assertLess(html.index(">语义视觉</th>"), html.index(">视频预览</th>"))
        self.assertLess(html.index(">视频预览</th>"), html.index(">片段检查</th>"))
        self.assertLess(html.index(">片段检查</th>"), html.index(">单条生成</th>"))
        self.assertIn("table-actions-column", html)
        self.assertIn("row-semantic-visual-cell", html)
        self.assertIn(
            'class="table-header-output px-4 py-3.5 w-36">声音预览</th>',
            html,
        )
        self.assertIn(
            'class="table-header-output px-4 py-3.5 w-32 text-center whitespace-nowrap">视频预览</th>',
            html,
        )
        self.assertIn(
            'class="table-header-output px-4 py-3.5 w-36 text-center whitespace-nowrap">片段检查</th>',
            html,
        )

    def test_selected_videos_use_one_multi_reference_start(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        start = html.index("async function generateSelectedVideos")
        end = html.index("async function generateAllVoicePreviews", start)
        selected = html[start:end]
        self.assertEqual(selected.count("startGlobalH3Generation(missingBase)"), 1)
        self.assertNotIn("retry_composition", selected)
        self.assertNotIn("/composition/generate", selected)

    def test_new_frontend_styles_are_bundled_locally(self) -> None:
        pages = ("index.html", "login.html", "voice-library.html", "gallery.html", "templates.html")
        for page in pages:
            html = (FRONTEND_ROOT / page).read_text(encoding="utf-8")
            self.assertIn('/app-static/new/tailwind.generated.css', html, page)
            self.assertIn(
                '/app-static/new/vendor/fontawesome/css/all.min.css',
                html,
                page,
            )
            self.assertNotIn("cdn.tailwindcss.com", html, page)
            self.assertNotIn("cdn.staticfile.net/ajax/libs/font-awesome", html, page)
            self.assertNotIn("tailwind.config", html, page)

        tailwind_css = (FRONTEND_ROOT / "tailwind.generated.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".hidden{display:none}", tailwind_css)
        self.assertIn(".fixed{position:fixed}", tailwind_css)
        self.assertIn(".grid{display:grid}", tailwind_css)
        self.assertIn(".w-44", tailwind_css)
        self.assertIn(".min-h-\\[58px\\]", tailwind_css)
        tailwind_config = (FRONTEND_ROOT / "tailwind.config.cjs").read_text(
            encoding="utf-8"
        )
        self.assertIn('path.join(__dirname, "*.html")', tailwind_config)
        self.assertTrue(
            (FRONTEND_ROOT / "vendor/fontawesome/css/all.min.css").is_file()
        )
        self.assertTrue(
            (FRONTEND_ROOT / "vendor/fontawesome/webfonts/fa-solid-900.woff2").is_file()
        )

    def test_status_polling_preserves_other_row_controls_and_image_target(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("function ltxStateNeedsRefresh()", html)
        self.assertIn("function ltxStateIsActive()", html)
        self.assertIn("const shouldRefresh = ltxStateIsActive();", html)
        self.assertIn("item?.remote_batch_id", html)
        self.assertIn("function projectHasActiveItems(project)", html)
        self.assertIn(
            "function syncProjectInputs(project, { renderTable = true, allowProjectSwitch = false } = {})",
            html,
        )
        self.assertIn(
            "syncProjectInputs(project, { renderTable: !projectHasActiveItems(project) });",
            html,
        )
        self.assertIn("let activeImageItemId = null;", html)
        self.assertIn("const itemId = activeImageItemId;", html)
        self.assertNotIn("let activeImageRow = null;", html)
        self.assertNotIn("const row = activeImageRow;", html)

    def test_semantic_visual_review_and_dynamic_preview_share_recipe(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="semantic-visual-modal"', html)
        self.assertIn('id="video-preview-semantic-overlay"', html)
        self.assertIn('id="video-preview-fixed-nameplate"', html)
        self.assertIn("function updatePreviewFixedNameplate()", html)
        self.assertIn("/api/new/fixed-visuals/nameplate/preview", html)
        self.assertIn("const nameplateScale = Number(nameplate.scale || 0.45)", html)
        self.assertIn("const transformX = Number(nameplate.transform_x || 0)", html)
        self.assertIn("const transformY = Number(nameplate.transform_y || 0)", html)
        self.assertIn("video-preview-nameplate-text-1", html)
        self.assertIn("activeLayoutProfileConfig", html)
        self.assertIn("function retryRowVisualAnalysis(button)", html)
        self.assertNotIn("/items/${rowId}/visual-analysis/retry", html)
        self.assertIn("/items/${rowId}/content-analysis/retry", html)
        self.assertIn("item.contentAnalysis?.overall_status === 'SUCCESS'", html)
        self.assertIn("一次处理音乐意图、字幕语义和视觉计划", html)
        self.assertIn("function saveSemanticVisualRecipe()", html)
        self.assertIn("function normalizeAutomaticSemanticOverlay(overlay)", html)
        self.assertIn("normalized.loop_to_target = false", html)
        self.assertIn("semanticVisualDraft[index].loop_to_target = false", html)
        self.assertIn("const configuredDurationUs = Number(effective.source_duration_us", html)
        self.assertIn(".map(normalizeAutomaticSemanticOverlay)", html)
        self.assertIn("decision.priority === 2", html)
        self.assertIn("关键画面", html)
        self.assertIn("仅供审核", html)
        self.assertNotIn("置信度 ${Math.round(Number(decision.confidence", html)
        self.assertIn("function removeSemanticVisualDraft(index)", html)
        self.assertIn("移除本行", html)
        self.assertIn("semanticVisualDraft[index].selection_mode = 'manual'", html)
        self.assertIn("if (field !== 'locked') semanticVisualDraft[index].locked = true", html)
        self.assertIn("script?.visualAnalysis?.recipe?.overlays", html)
        self.assertIn("/visual-overlays`,", html)
        self.assertIn("bottom_center:'底部居中'", html)
        self.assertIn("function resolveSemanticWindowGeometry(", html)
        self.assertIn("const maximumVisibleHeight = height * 0.37", html)
        self.assertIn("image.style.height = 'auto'", html)
        self.assertIn('id="video-preview-semantic-video"', html)
        self.assertIn("overlayVideo.style.zIndex = '17'", html)
        self.assertIn("当前 ${semanticVisualDraft.length} 项素材", html)
        self.assertIn("语义视觉已冻结", html)
        self.assertNotIn("张配图", html)
        self.assertNotIn("语义配图已冻结", html)

    def test_new_workspace_and_voice_center_use_real_voice_apis(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        voice_center = (FRONTEND_ROOT / "voice-library.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/new/voices", workspace)
        self.assertIn('id="voice-speed-slider"', workspace)
        self.assertIn('min="0.5" max="2" step="0.01"', workspace)
        self.assertNotIn('id="voice-speed-slider" type="range" min="0.5" max="2" step="0.05"', workspace)
        self.assertIn('data-voice-speed="0.8"', workspace)
        self.assertIn('data-voice-speed="0.9"', workspace)
        self.assertIn("function saveVoiceSpeed(rawSpeed)", workspace)
        self.assertIn("voice_settings: { ...(voicePreferences.voice_settings || {}), speed }", workspace)
        self.assertIn("audio.playbackRate = selectedVoiceSpeed()", workspace)
        self.assertIn("/audio/generate", workspace)
        self.assertIn("/audio/status", workspace)
        self.assertIn("digital-human-resolution", workspace)
        self.assertIn('<option value="loop_anchor">首尾同图（默认）</option>', workspace)
        self.assertIn('id="h3-tail-seconds" type="number" min="0" max="1" step="0.1" value="0.1"', workspace)
        self.assertIn("continuity_mode: document.getElementById('h3-continuity').value || 'loop_anchor'", workspace)
        self.assertIn("generation_tail_seconds: Number(document.getElementById('h3-tail-seconds').value || 0.1)", workspace)
        self.assertIn('id="digital-human-resolution" type="hidden" value="1024"', workspace)
        self.assertNotIn('<option value="2048">', workspace)
        self.assertIn("/digital-human-settings", workspace)
        self.assertIn("resolution: selectedDigitalHumanResolution()", workspace)
        self.assertIn("/items/${rowId}/audio/retry", workspace)
        self.assertIn("voice_settings: voicePreferences.voice_settings || {}", workspace)
        self.assertIn("/projects/${activeProject.project_id}/voice", workspace)
        self.assertIn("/api/new/voice-creations", voice_center)
        self.assertIn("/api/new/voices/import", voice_center)
        self.assertIn("submitVoiceCreation", voice_center)
        self.assertIn("importExistingVoice", voice_center)
        self.assertIn('id="import-voice-id"', voice_center)
        self.assertIn('id="import-voice-already-activated"', voice_center)
        self.assertIn("already_activated: alreadyActivated", voice_center)
        self.assertIn("导入本身不合成语音、不触发 ¥9.9", voice_center)
        self.assertIn("saveCreatedVoice", voice_center)
        self.assertIn("生成克隆试听", voice_center)
        self.assertIn("保存到音色库", voice_center)

        self.assertIn("activateSavedVoice", voice_center)
        self.assertIn("deleteSavedVoice", voice_center)
        self.assertIn('id="voice-source-preview"', voice_center)
        self.assertIn("使用该音色生成试听语音，是否继续？", voice_center)
        self.assertIn("使用该音色生成试听语音，是否继续？", workspace)
        self.assertNotIn("首次试听将调用 MiniMax", voice_center)
        self.assertNotIn("提取并注入原型库", voice_center)
        self.assertEqual(voice_center.count('id="voice-task-list"'), 1)
        self.assertNotIn("actions.google.com/sounds", workspace)
        self.assertNotIn("actions.google.com/sounds", voice_center)
        self.assertNotIn("startCloningProgress", voice_center)
        self.assertNotIn("addNewVoiceCard", voice_center)
        self.assertNotIn("setInterval", voice_center)
        self.assertNotIn("原型体验", voice_center)
        self.assertNotIn("原型演示", voice_center)
        self.assertNotIn("原型演示", workspace)
        self.assertIn(
            "['wait', 'failed'].includes(row.getAttribute('data-voice-status'))",
            workspace,
        )
        self.assertNotIn(
            "['idle', 'failed'].includes(row.getAttribute('data-voice-status'))",
            workspace,
        )

    def test_each_project_row_has_smart_audio_video_and_segment_review_actions(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("function runSingleAudio(buttonEl)", html)
        self.assertIn("function runSingleVideo(buttonEl)", html)
        self.assertNotIn('onclick="runSingleVariants(this)"', html)
        self.assertIn('onclick="openH3SegmentPreviewModal(this)"', html)
        self.assertIn("item_ids: [rowId]", html)
        self.assertIn("重新生成声音", html)
        self.assertIn("const pendingScriptSaves = new Map()", html)
        self.assertIn("await retryCloningAudio(buttonEl)", html)
        self.assertNotIn("已复用当前声音", html)
        self.assertIn("复用视频", html)
        self.assertIn("重试这段", html)

    def test_complete_video_flow_keeps_internal_stages_out_of_user_results(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("/h3/prepare", workspace)
        self.assertIn("/h3/confirm", workspace)
        self.assertIn("/composition/status", workspace)
        self.assertIn("const canBackfillSeedvr2 = false", workspace)
        self.assertIn("data-preview-video-url", workspace)
        self.assertIn("/preview-video", workspace)
        self.assertIn("以剪映草稿为准", workspace)
        self.assertIn("生成完整成片", workspace)
        self.assertIn("/postprocess/generate", workspace)
        self.assertIn("/postprocess-settings", workspace)
        self.assertIn("startGlobalPostprocess()", workspace)
        self.assertIn("/h3/overrides", workspace)
        self.assertIn("高级覆盖（可选）", workspace)
        self.assertIn("completedTargets.map((item) => item.id)", workspace)
        final_flow = workspace[
            workspace.index("async function startGlobalFinalVideoGeneration()") :
            workspace.index("async function retryFailedCompositionItems()")
        ]
        self.assertLess(
            final_flow.index("startGlobalH3Generation"),
            final_flow.index("const canPostprocess"),
        )
        self.assertNotIn("startGlobalComposition", final_flow)
        self.assertNotIn("retryFailedCompositionItems", final_flow)
        self.assertIn("data-final-video-url", workspace)
        self.assertIn("video-preview-time", workspace)
        self.assertIn("loadedmetadata", workspace)
        self.assertIn("updatePreviewCaption", workspace)
        self.assertIn("playPreviewBgm", workspace)
        self.assertIn("const playbackDurationUs = sourceDurationUs > 0", workspace)
        self.assertNotIn("(sourceTime - sourceStart) % sourceDuration", workspace)

        self.assertIn("buildBacktimedPreviewBgmPlan", workspace)
        self.assertIn("PREVIEW_BGM_CROSSFADE_SECONDS = 0.2", workspace)
        self.assertIn("previewBgmBaseVolume = 0.18", workspace)
        self.assertIn("script?.postprocessSettings?.bgm_volume", workspace)
        self.assertIn("/postprocess/export", workspace)
        self.assertIn("重新导出带封面 MP4", workspace)
        self.assertIn("封面/设置已更新 · 旧成片已过期", workspace)
        self.assertIn("刷新动态预览", workspace)
        self.assertIn("可编辑剪映草稿；不会生成新的 MP4", workspace)
        self.assertIn('id="video-preview-cover"', workspace)
        self.assertIn('id="video-preview-cover-image"', workspace)
        self.assertIn("const BROWSER_COVER_DURATION_SECONDS = 3 / 30", workspace)
        self.assertIn("previewCoverPrefixPlayed", workspace)
        self.assertIn("BROWSER_COVER_DURATION_SECONDS * 1000", workspace)
        self.assertIn("function updatePreviewCover()", workspace)
        self.assertIn("function activePreviewCoverImage()", workspace)
        self.assertIn("baseVideo?.metadata?.input_image_sha256", workspace)
        self.assertIn("activePreviewCoverTitle", workspace)
        self.assertIn("startPostprocessVideoExport", workspace)
        self.assertIn("ensureProjectItemExport", workspace)
        self.assertIn("waitForProjectItemPostprocess", workspace)
        self.assertIn("本次目标 ${candidateItems.length} 条", workspace)
        self.assertIn("等待正在生成草稿 ${waiting.length} 条", workspace)
        self.assertNotIn("const readyRows = candidateRows.filter", workspace)
        self.assertNotIn("legacy_build_and_export", workspace)
        self.assertIn("已复用冻结剪映草稿生成带封面 MP4", workspace)
        self.assertIn("style.font_size || 14", workspace)
        self.assertIn("style.transform_y ?? (-850 / 1920)", workspace)
        self.assertIn("caption.style.webkitTextStroke = '0px transparent'", workspace)
        self.assertNotIn("caption.scrollWidth > caption.clientWidth", workspace)
        self.assertIn("caption.style.whiteSpace = 'nowrap'", workspace)
        self.assertIn('id="video-preview-title-label"', workspace)
        self.assertIn('id="video-preview-title-headline"', workspace)
        self.assertIn('id="video-preview-disclaimer"', workspace)
        self.assertIn("function updatePreviewDisclaimer()", workspace)
        self.assertIn("const transformY = Number(style.transform_y ?? (-1760 / 1920))", workspace)
        self.assertIn("const fontSize = Number(style.font_size || 6)", workspace)
        self.assertIn("disclaimer.style.opacity = String(Number(style.opacity ?? 0.5))", workspace)
        self.assertIn("非医疗保健科普：仅供参考", workspace)
        self.assertIn("1535 / 1920", workspace)
        self.assertNotIn("1350 / 1920", workspace)
        self.assertIn("headline.textContent = '世界冠军带你自律'", workspace)
        self.assertIn("displayedWidth * Number(style.font_size || 19) / 220", workspace)
        self.assertIn("headline.style.color = '#FFF589'", workspace)
        self.assertIn("const nameplateScale = Number(nameplate.scale || 0.45)", workspace)
        self.assertIn("aspect-ratio: 9 / 16", workspace)
        self.assertIn('id="video-preview-play-button"', workspace)
        self.assertIn("button.classList.toggle('opacity-0', isPlaying)", workspace)
        self.assertIn("/original-materials", workspace)
        self.assertIn("/current-video?filename=", workspace)
        self.assertIn("body: file", workspace)
        self.assertIn("/items`, {", workspace)
        self.assertIn("method: 'POST'", workspace)
        self.assertIn("const targetItemIds = [...selectedProjectItemIds]", workspace)
        self.assertIn("await updateImageMappingScope(targetItemIds, false)", workspace)
        self.assertIn("targetItemIds.length ? uploadedImageIds : []", workspace)
        self.assertIn("按原有全项目图片池规则分配", workspace)
        self.assertNotIn("请先选择换图脚本", workspace)
        self.assertIn('aria-label="删除图片', workspace)
        self.assertIn("fa-trash-can", workspace)
        self.assertNotIn("opacity-0 group-hover:opacity-100 transition-opacity", workspace)
        self.assertNotIn("URL.createObjectURL(file)", workspace)
        self.assertNotIn("00:12 / 00:28", workspace)
        self.assertNotIn("data-base-video-url", workspace)
        self.assertNotIn("下载基础视频", workspace)
        self.assertNotIn("基础视频", workspace)

        self.assertNotIn("生成字幕 + BGM 成片", workspace)
        self.assertNotIn("重试只会重新执行本地剪映后处理", workspace)
        self.assertIn("new FontFace(family, buffer).load()", workspace)
        self.assertIn("fetch(font.preview_url", workspace)
        self.assertIn("applyFontPreviewToElement", workspace)
        self.assertIn("row-subtitle-preview", workspace)
        self.assertIn("字幕预览 · 随姿态", workspace)
        self.assertIn("由当前站姿/坐姿规范自动决定", workspace)
        self.assertIn("const subtitlePreviewText = '这是字幕预览';", workspace)
        self.assertNotIn("const subtitlePreviewSource =", workspace)
        self.assertIn("const displayedBgmIdentity =", workspace)
        self.assertIn("retryRowPostprocessPreview", workspace)
        self.assertIn("preview-retry-${rowId}", workspace)
        self.assertIn("async function regeneratePostprocessPreview(rowId)", workspace)
        self.assertNotIn("openAfter", workspace)
        self.assertNotIn("regeneratePostprocessPreview(rowId, true)", workspace)
        self.assertIn("刷新字幕/BGM/封面预览", workspace)
        self.assertIn("调整字幕/BGM", workspace)
        self.assertIn("force_retry: true", workspace)
        self.assertIn('id="btn-refresh-previews"', workspace)
        self.assertIn("async function refreshBatchPostprocessPreviews()", workspace)
        self.assertIn("preview-batch-refresh-${projectId}-${Date.now()}", workspace)
        self.assertIn("const candidates = selected.length ? selected : filteredProjectItems()", workspace)
        self.assertIn("重新计算字幕断句、ASR 时间绑定、自动 BGM 推荐和三帧封面", workspace)
        self.assertIn("不会重新生成声音、数字人或编码 MP4", workspace)
        self.assertIn("item.status === 'COMPOSITION_READY'", workspace)
        self.assertIn("单条导出失败，继续处理", workspace)
        self.assertIn("const failedExports = []", workspace)
        self.assertIn("const canAdjustPreview = Boolean(activePreviewScript()?.baseVideo)", workspace)
        self.assertNotIn("retryButton.classList.toggle('hidden', !isBrowserCompositionPreview())", workspace)
        self.assertNotIn("将为 ${rows.length} 条视频生成包含字幕和所选背景音乐", workspace)
        self.assertIn("allowed_actions?.retry_postprocess", workspace)
        self.assertIn("async function retryPostprocessExport(rowId)", workspace)
        self.assertIn("preview-export-retry-${rowId}-${Date.now()}", workspace)
        self.assertIn("const targetItems = uploadedScripts.filter", workspace)
        self.assertIn("await startGlobalPostprocess([rowId])", workspace)
        self.assertIn("重新生成新的声音版本", workspace)
        self.assertIn("pendingRows.length ? pendingRows", workspace)
        self.assertNotIn("4B 使用现有 BGM 素材库", workspace)
        self.assertNotIn("4B 字幕为单行", workspace)
        self.assertNotIn("先生成 4A 基础视频", workspace)
        self.assertNotIn("4A：使用最新图片", workspace)
        self.assertNotIn("4B：单行字幕", workspace)
        self.assertNotIn("images.unsplash.com", workspace)
        self.assertNotIn("selectAlignment", workspace)
        self.assertNotIn("modal-stroke-color", workspace)
        self.assertNotIn('id="btn-generate-variants"', workspace)
        self.assertNotIn('id="btn-variant-settings"', workspace)
        self.assertNotIn("正在重新合成第 ${rowId} 条带 BGM 和字幕的视频", workspace)
        self.assertIn("项目运行记录", workspace)
        self.assertIn("downloadProjectDiagnostics", workspace)
        self.assertIn("/diagnostics", workspace)
        self.assertIn("error_code", workspace)
        self.assertNotIn("operation.error_message", workspace)

    def test_composition_failure_toast_distinguishes_network_and_business_errors(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("function latestCompositionFailure", workspace)
        self.assertIn("DIGITAL_HUMAN_CONNECTION_FAILED", workspace)
        self.assertIn("数字人服务器暂时不可用", workspace)
        self.assertIn("数字人任务启动失败", workspace)
        self.assertIn("failure?.error_message", workspace)
        self.assertIn("new Set(failedItems.map((item) => item.id))", workspace)

    def test_failed_source_only_video_can_backfill_without_showing_stale_preview(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("const backfillSeedvr2ButtonHtml = canBackfillSeedvr2", workspace)
        self.assertIn("数字人已完成，高清未完成", workspace)
        self.assertGreaterEqual(workspace.count("${backfillSeedvr2ButtonHtml}"), 2)
        self.assertIn("const activePreviewItemId = activeVideoPreviewRow", workspace)
        self.assertIn("旧视频预览已关闭", workspace)
        self.assertIn("历史视频不会继续冒充当前版本", workspace)

    def test_visible_generation_flow_uses_h3_account_selection_only(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/new/h3/accounts", workspace)
        self.assertIn("selectH3Accounts(accounts)", workspace)
        self.assertIn("selected_account_ids: selected", workspace)
        self.assertIn("/h3/prepare", workspace)
        self.assertIn("/h3/confirm", workspace)
        final_start = workspace.index("async function startGlobalFinalVideoGeneration")
        final_end = workspace.index("async function retryFailedCompositionItems", final_start)
        visible_final_flow = workspace[final_start:final_end]
        self.assertNotIn("confirmRunningHubCost", visible_final_flow)
        self.assertNotIn("runningHubSelectionRequestFields", visible_final_flow)

    def test_composition_poll_errors_are_deduplicated_and_backed_off(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("let compositionStatusPollFailureCount = 0", workspace)
        self.assertIn("const COMPOSITION_STATUS_WARNING_THRESHOLD = 3", workspace)
        self.assertIn(
            "compositionStatusPollFailureCount === COMPOSITION_STATUS_WARNING_THRESHOLD",
            workspace,
        )
        self.assertIn("连续 ${COMPOSITION_STATUS_WARNING_THRESHOLD} 次刷新失败", workspace)
        self.assertIn("Math.min(compositionStatusPollFailureCount - 1, 3)", workspace)
        self.assertIn("不会重复提交 RunningHub 任务", workspace)

    def test_composition_poll_refreshes_lightweight_snapshots_while_cloud_sync_is_slow(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("let compositionStatusRequestProjectId = null", workspace)
        self.assertIn("function scheduleCompositionSnapshotPoll(projectId)", workspace)
        self.assertIn("workspaceApi(`/api/new/projects/${projectId}`)", workspace)
        self.assertIn("compositionStatusRequestProjectId = projectId", workspace)
        self.assertIn("scheduleCompositionSnapshotPoll(projectId)", workspace)

    def test_generation_ui_and_late_poll_responses_are_scoped_to_current_project(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("function resetProjectScopedGenerationState()", workspace)
        self.assertIn("if (projectChanged) resetProjectScopedGenerationState();", workspace)
        self.assertIn("syncProjectInputs(project, { allowProjectSwitch: true });", workspace)
        self.assertIn("incomingProjectId !== currentProjectId", workspace)
        self.assertIn("if (activeProject?.project_id !== projectId) return;", workspace)
        self.assertIn("compositionStatusRequestProjectId === activeProject?.project_id", workspace)
        self.assertNotIn("if (ltxMode && ltxActive)", workspace)
        self.assertNotIn("else if (ltxMode && isGeneratingLtx)", workspace)
        self.assertIn("if (h3Mode && isGeneratingH3)", workspace)
        self.assertIn("else if (h3Mode && h3Active)", workspace)

    def test_variant_workspace_is_replaced_by_wrapping_h3_segment_review(self) -> None:
        workspace = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        table = workspace[
            workspace.index('id="synthesis-table"') : workspace.index('</table>')
        ]
        self.assertIn(">片段检查</th>", table)
        self.assertNotIn(">变体数</th>", table)
        self.assertNotIn(">变体预览</th>", table)
        self.assertNotIn(">再补 X 个变体</th>", table)
        self.assertIn('id="h3-segment-preview-modal"', workspace)
        self.assertIn("xl:grid-cols-5", workspace)
        self.assertIn("overflow-y-auto overflow-x-hidden", workspace)
        self.assertIn("preload=\"metadata\"", workspace)
        self.assertIn("重试这段", workspace)
        self.assertIn("/h3-segments/${segmentNumber}/preview", workspace)
        self.assertIn("segment.local_preview_ready === true", workspace)
        self.assertIn("baseMatchesSegment", workspace)
        self.assertNotIn('id="btn-generate-variants"', workspace)
        self.assertNotIn('id="btn-variant-settings"', workspace)

        # Keep legacy APIs and stored outputs readable while the workspace stops
        # offering new variant generation.
        self.assertIn("/variants/generate", workspace)
        self.assertIn("/variants/status", workspace)
        self.assertIn("/variants/supplement", workspace)
        self.assertIn("/variants/retry", workspace)
        self.assertIn("method: 'DELETE'", workspace)
        self.assertIn("最大差异优先", workspace)
        self.assertIn('id="variant-use-stickers" type="checkbox" checked', workspace)
        self.assertNotIn("variant-cover-modal", workspace)
        self.assertNotIn("rowVariantCovers", workspace)
        self.assertIn("字幕字体和背景音乐继承模块 4B", workspace)
        self.assertIn("D:\\auto\\月.日\\当日批次号", workspace)
        self.assertIn('aspect-[9/16]', workspace)
        self.assertIn("/script-source?filename=", workspace)
        self.assertNotIn("variant-use-subtitles", workspace)
        self.assertNotIn("setTimeout(() => {\n                    markRowVariantsReady", workspace)
        paths = {route.path for route in create_app(self.settings).routes}
        self.assertIn("/api/new/variant-options", paths)
        self.assertIn("/api/new/projects/{project_id}/variant-settings", paths)
        self.assertIn("/api/new/projects/{project_id}/variants/generate", paths)
        self.assertIn("/api/new/projects/{project_id}/variants/status", paths)
        self.assertIn("/api/new/projects/{project_id}/items/{item_id}/variants/supplement", paths)
        self.assertIn("/api/new/projects/{project_id}/items/{item_id}/variants/retry", paths)
        self.assertIn("/api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}", paths)
        self.assertIn(
            "/api/new/projects/{project_id}/items/{item_id}/h3-segments/{segment_number}/preview",
            paths,
        )

    def test_module_7_gallery_uses_real_results_and_portrait_preview(self) -> None:
        index = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        gallery = (FRONTEND_ROOT / "gallery.html").read_text(encoding="utf-8")
        self.assertIn("/api/new/gallery", gallery)
        self.assertIn("/api/new/gallery/downloads", gallery)
        self.assertIn("/api/new/gallery/deletions", gallery)
        self.assertIn('id="delete-button"', gallery)
        self.assertIn("删除选中", gallery)
        self.assertIn("deleteSelected", gallery)
        self.assertIn("portrait-frame", gallery)
        self.assertIn("defaultPostprocessFontIdentity", index)
        self.assertIn("[overflow-wrap:anywhere]", index)
        self.assertIn("table-fixed", index)
        self.assertIn("select-all-global", gallery)
        self.assertIn("全选本批次", gallery)
        self.assertIn("batch-modal-grid", gallery)
        self.assertIn("renderBatchOverview", gallery)
        self.assertIn("9 / 16", gallery)
        self.assertIn("filter-project", gallery)
        self.assertIn("filter-date", gallery)
        self.assertIn("filter-batch", gallery)
        self.assertNotIn("images.unsplash.com", gallery)
        self.assertNotIn("Video Card 1", gallery)
        paths = {route.path for route in create_app(self.settings).routes}
        self.assertIn("/api/new/gallery", paths)
        self.assertIn("/api/new/gallery/downloads", paths)
        self.assertIn("/api/new/gallery/deletions", paths)

    def test_new_pages_require_login_but_login_and_logo_are_public(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            for path in (
                "/app/new",
                "/app/new/generate",
                "/app/new/gallery",
                "/app/new/voices",
                "/app/new/templates",
            ):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 303, path)
                self.assertEqual(
                    response.headers["location"],
                    f"/app/new/login?next={path}",
                )

            login = client.get("/app/new/login")
            self.assertEqual(login.status_code, 200)
            self.assertIn("/api/auth/login", login.text)
            self.assertNotIn("demo_vip@shanjian.ai", login.text)
            self.assertNotIn("模拟扫码成功", login.text)

            logo = client.get("/app-static/new/logo.png")
            self.assertEqual(logo.status_code, 200)
            self.assertEqual(logo.headers["content-type"], "image/png")

            tailwind = client.get("/app-static/new/tailwind.generated.css")
            self.assertEqual(tailwind.status_code, 200)
            self.assertEqual(tailwind.headers["content-type"], "text/css; charset=utf-8")
            self.assertIn(".hidden{display:none}", tailwind.text)

            fontawesome = client.get(
                "/app-static/new/vendor/fontawesome/css/all.min.css"
            )
            self.assertEqual(fontawesome.status_code, 200)
            self.assertEqual(
                fontawesome.headers["content-type"], "text/css; charset=utf-8"
            )

            font = client.get(
                "/app-static/new/vendor/fontawesome/webfonts/fa-solid-900.woff2"
            )
            self.assertEqual(font.status_code, 200)
            self.assertEqual(font.headers["content-type"], "font/woff2")

    def test_digital_account_login_opens_all_new_routes_and_logout_closes_them(self) -> None:
        user = {"user_id": "center-user", "username": "tester", "enabled": True}

        def verify(_client, token):
            return user if token == "center-token" else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify",
            new=verify,
        ):
            with TestClient(create_app(self.settings)) as client:
                login = client.post(
                    "/api/auth/login",
                    json={
                        "username": "tester",
                        "password": "pass123",
                        "next": "/app/new/gallery",
                    },
                )
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(login.json()["next"], "/app/new/gallery")
                self.assertIn("HttpOnly", login.headers["set-cookie"])

                expected_files = {
                    "/app/new": "index.html",
                    "/app/new/generate": "index.html",
                    "/app/new/gallery": "gallery.html",
                    "/app/new/voices": "voice-library.html",
                    "/app/new/templates": "templates.html",
                }
                for path, filename in expected_files.items():
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertEqual(
                        response.text.replace("\r\n", "\n"),
                        (FRONTEND_ROOT / filename)
                        .read_text(encoding="utf-8")
                        .replace("\r\n", "\n"),
                    )
                    self.assertIn("/api/auth/session", response.text)
                    self.assertIn("/api/auth/logout", response.text)
                    self.assertIn("current-user-name", response.text)

                session = client.get("/api/auth/session")
                self.assertTrue(session.json()["authenticated"])
                self.assertEqual(session.json()["username"], "tester")
                self.assertEqual(session.json()["ltx_workbench_url"], "http://127.0.0.1:8792")
                self.assertEqual(session.json()["workbench_environment"], "local")
                self.assertEqual(session.json()["workbench_environment_label"], "本地测试")

                logout = client.post("/api/auth/logout")
                self.assertEqual(logout.status_code, 200)
                self.assertIn("Max-Age=0", logout.headers["set-cookie"])
                client.cookies.clear()

                closed = client.get("/app/new", follow_redirects=False)
                self.assertEqual(closed.status_code, 303)
                self.assertEqual(closed.headers["location"], "/app/new/login?next=/app/new")

    def test_legacy_video_mutations_are_rejected_before_any_engine_call(self) -> None:
        user = {"user_id": "center-user", "username": "tester", "enabled": True}

        def verify(_client, token):
            return user if token == "center-token" else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify",
            new=verify,
        ):
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                requests = (
                    ("put", "/api/new/projects/project-1/generation-mode", {"mode": "runninghub_digital_human"}),
                    ("put", "/api/new/projects/project-1/items/item-1/ltx/source-video", None),
                    ("post", "/api/new/projects/project-1/ltx/generate", {}),
                    ("post", "/api/new/projects/project-1/ltx/refresh", {}),
                    ("post", "/api/new/projects/project-1/items/item-1/ltx/retry", {}),
                    ("post", "/api/new/projects/project-1/composition/generate", {}),
                    ("post", "/api/new/projects/project-1/items/item-1/composition/retry", {}),
                    ("post", "/api/new/projects/project-1/items/item-1/composition/seedvr2-backfill", {}),
                )
                for method, path, payload in requests:
                    response = getattr(client, method)(path, json=payload)
                    self.assertEqual(response.status_code, 409, path)
                    self.assertIn("只支持多参考", response.json()["detail"])

    def test_logged_in_login_page_only_redirects_inside_new_app(self) -> None:
        user = {"user_id": "center-user", "username": "tester", "enabled": True}

        def verify(_client, token):
            return user if token == "center-token" else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify",
            new=verify,
        ):
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                accepted = client.get(
                    "/app/new/login?next=/app/new/voices", follow_redirects=False
                )
                self.assertEqual(accepted.headers["location"], "/app/new/voices")

                rejected = client.get(
                    "/app/new/login?next=https://example.com", follow_redirects=False
                )
                self.assertEqual(rejected.headers["location"], "/app/new")

    def test_revoked_digital_account_session_cannot_keep_new_page_open(self) -> None:
        user = {"user_id": "center-user", "username": "tester", "enabled": True}
        account_enabled = True

        def verify(_client, token):
            return user if token == "center-token" and account_enabled else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify",
            new=verify,
        ):
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                self.assertEqual(client.get("/app/new").status_code, 200)

                account_enabled = False
                revoked = client.get("/app/new", follow_redirects=False)
                self.assertEqual(revoked.status_code, 303)
                self.assertEqual(
                    revoked.headers["location"], "/app/new/login?next=/app/new"
                )


if __name__ == "__main__":
    unittest.main()
