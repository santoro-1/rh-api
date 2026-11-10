from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "apps" / "processor" / "frontend"


class BatchEditorFrontendTest(unittest.TestCase):
    def test_primary_batch_flow_uses_web_editor_instead_of_excel_upload(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("批量任务编辑器", html)
        self.assertIn('id="uploadSelectedExcelDraftsBtn"', html)
        self.assertIn('id="applyBatchDefaultsBtn"', html)
        self.assertIn('id="selectAllBatchRows"', html)
        self.assertIn('id="deleteSelectedBatchRowsBtn"', html)
        self.assertNotIn('id="excelBatchFile"', html)

    def test_personal_asset_workspace_supports_management(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        for element_id in (
            "refreshPersonalAssetsBtn",
            "personalAssetSearch",
            "personalAssetKindFilter",
            "personalAssetStatusFilter",
            "personalAssetRows",
            "personalAssetPreviewDialog",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('/api/local-assets?include_deleted=true', script)
        self.assertIn('method: "DELETE"', script)
        self.assertIn('/restore', script)
        self.assertIn('enabled: enabled.checked', script)
        self.assertIn('id="localAssetsWorkspaceChoice"', html)
        self.assertIn('health.local_file_access', script)
        self.assertNotIn('id="readExcelBatchBtn"', html)
        self.assertNotIn("下载固定模板", html)

    def test_batch_upload_creates_rows_and_bulk_controls_are_bound(self) -> None:
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn("addBatchTask(mother)", script)
        self.assertIn("applyBatchDefaultsToSelected", script)
        self.assertIn("state.batchSelectedRowIds", script)
        self.assertIn('$("applyBatchDefaultsBtn").addEventListener', script)
        self.assertIn('$("selectAllBatchRows").addEventListener', script)
        self.assertIn('["batchDefaultSticker", "sticker", "all"]', script)
        self.assertIn('["batchDefaultFont", "font", "all"]', script)

    def test_standalone_mode_uses_local_picker_and_output_folder_actions(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('name="processingMode" value="local"', html)
        self.assertIn('id="selectLocalVideoBtn"', html)
        self.assertIn('id="selectLocalOutputFolderBtn"', html)
        self.assertIn('id="personalAssetsWorkspace"', html)
        self.assertIn('id="uploadPersonalAssetsBtn"', html)
        self.assertIn('/api/local/media-reference', script)
        self.assertIn('/api/local/select-output-folder', script)
        self.assertIn('/api/config/personal-library-root', script)
        self.assertIn('"打开所在文件夹"', script)

    def test_digital_human_inbox_can_pull_and_import_exact_caption_tasks(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('name="workspaceMode" value="digital_human"', html)
        self.assertIn('id="digitalHumanTaskList"', html)
        self.assertIn('/api/digital-human/tasks?limit=50', script)
        self.assertIn('一键导入工作台', script)
        self.assertIn('job.captions.cues = state.digitalHumanCaptionCues', script)
        self.assertIn('window.setInterval', script)

    def test_admin_page_exposes_simple_internal_user_management(self) -> None:
        html = (FRONTEND_ROOT / "assets.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "assets.js").read_text(encoding="utf-8")
        self.assertIn('id="createUserForm"', html)
        self.assertIn('id="userRows"', html)
        self.assertIn('apiFetch("/api/admin/users"', script)
        self.assertIn('method: "PATCH"', script)

    def test_processing_mode_can_navigate_between_local_and_shared_servers(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")
        self.assertNotIn('id="sharedServerUrl"', html)
        self.assertNotIn('id="openSharedServerBtn"', html)
        self.assertIn('id="configureSharedWorkspaceBtn"', html)
        self.assertIn("管理其他工作台", html)
        self.assertIn("health.shared_processor_url", script)
        self.assertIn("openSharedMachine", script)
        self.assertIn("SHARED_WORKSPACES_STORAGE_KEY", script)
        self.assertIn("window.localStorage.setItem", script)
        self.assertIn("probeSharedWorkspace", script)
        self.assertIn("activeJobs", script)
        self.assertIn("window.location.assign(`${serverUrl}/app`)", script)
        self.assertIn('"/api/auth/handoff-to?target=local&next=/app"', script)
        self.assertIn('return "http://127.0.0.1:8010/app"', script)
        self.assertNotIn("sharedModeInput.disabled = state.localFileAccess", script)
        self.assertIn('$("localAssetsWorkspaceChoice").classList.toggle("hidden", !state.localFileAccess)', script)

    def test_shared_processor_build_has_a_separate_release_package(self) -> None:
        build_script = (PROJECT_ROOT / "scripts" / "build" / "build_processor.ps1").read_text(
            encoding="utf-8"
        )
        wrapper = (PROJECT_ROOT / "build_shared_processor.ps1").read_text(encoding="utf-8")
        self.assertIn('JianyingRenderServer-shared-windows-x64.zip', build_script)
        self.assertIn('$ProcessorConfig.deployment_mode = "shared"', build_script)
        self.assertIn('$ProcessorConfig.host = "0.0.0.0"', build_script)
        self.assertIn('$ProcessorConfig.shared_processor_url = ""', build_script)
        self.assertIn('$ProcessorConfig.auth_authority = "false"', build_script)
        self.assertIn('-DeploymentMode shared', wrapper)

    def test_full_deployment_build_can_embed_a_remote_digital_human_url(self) -> None:
        build_script = (PROJECT_ROOT / "scripts" / "build" / "build_processor.ps1").read_text(
            encoding="utf-8"
        )
        wrapper = (PROJECT_ROOT / "build_deployment.ps1").read_text(encoding="utf-8")
        guide = (PROJECT_ROOT / "docs" / "FAST_BUILD.md").read_text(encoding="utf-8")
        self.assertIn('[string]$DigitalHumanServerUrl = ""', build_script)
        self.assertIn("$ProcessorConfig.digital_human_server_url = $DigitalHumanServerUrl", build_script)
        self.assertIn("UpdateOnly excludes data/processor_config.json", build_script)
        self.assertNotIn("$SemanticVisualSource", build_script)
        self.assertIn("UpdateOnly package must not contain a data directory", build_script)
        self.assertIn("$ProcessorArguments.DigitalHumanServerUrl = $DigitalHumanServerUrl", wrapper)
        self.assertIn('-DigitalHumanServerUrl "https://video.lanyingjk01.com"', guide)

    def test_first_install_build_can_exclude_public_libraries(self) -> None:
        build_script = (PROJECT_ROOT / "scripts" / "build" / "build_processor.ps1").read_text(
            encoding="utf-8"
        )
        wrapper = (PROJECT_ROOT / "build_deployment.ps1").read_text(encoding="utf-8")
        guide = (PROJECT_ROOT / "docs" / "FAST_BUILD.md").read_text(encoding="utf-8")

        self.assertIn("[switch]$WithoutLibraries", build_script)
        self.assertIn("JianyingRenderServer-no-libraries-windows-x64.zip", build_script)
        self.assertIn("Skipping public libraries for first-install code package", build_script)
        self.assertIn('Join-Path $LibrariesDir "semantic_visual_library"', build_script)
        self.assertIn('schema = "jyd.semantic-visual-catalog.v3"', build_script)
        self.assertIn('assets = @()', build_script)
        self.assertIn("$ProcessorArguments.WithoutLibraries = $true", wrapper)
        self.assertIn("-WithoutLibraries", guide)
        self.assertIn("首次安装", guide)

    def test_release_packages_include_plain_language_guides(self) -> None:
        processor_build = (PROJECT_ROOT / "scripts" / "build" / "build_processor.ps1").read_text(
            encoding="utf-8"
        )
        collector_build = (PROJECT_ROOT / "scripts" / "build" / "build_collector.ps1").read_text(
            encoding="utf-8"
        )
        agent_build = (PROJECT_ROOT / "scripts" / "build" / "build_agent.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("PROCESSOR_UPDATE.md", processor_build)
        self.assertIn("SHARED_PROCESSOR_QUICK_START.md", processor_build)
        self.assertIn("README-PROCESSOR.md", processor_build)
        self.assertIn("README-COLLECTOR.md", collector_build)
        self.assertIn("README-AGENT.md", agent_build)
        self.assertIn("START-HERE.txt", processor_build)
        self.assertIn("START-HERE.txt", collector_build)
        self.assertIn("START-HERE.txt", agent_build)

    def test_visual_variant_suite_is_exposed_as_three_batch_dimensions(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('id="useVisualVariant"', html)
        self.assertIn('id="mirrorIntervalSeconds"', html)
        self.assertIn('id="variantRatioSquare"', html)
        self.assertIn('id="variantRatioThreeFour"', html)
        self.assertIn('id="cornerStickerOpacity"', html)
        self.assertIn('id="cornerStickerOpacity" type="range" min="0" max="100" step="1" value="50"', html)
        self.assertNotIn('id="cornerStickerVisible"', html)
        self.assertNotIn('id="cornerStickerScale"', html)
        self.assertIn('key: "mirror"', script)
        self.assertIn('key: "layout"', script)
        self.assertIn('key: "corner_sticker"', script)
        self.assertIn('const corners = ["top_left", "top_right", "bottom_left", "bottom_right"]', script)
        self.assertIn('visible_ratio: visibleRatio', script)

    def test_batch_editor_supports_visual_variant_suite(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('id="batchDefaultVisual"', html)
        self.assertIn('id="batchMirrorIntervalSeconds"', html)
        self.assertIn('id="batchVariantRatioSquare"', html)
        self.assertIn('id="batchVariantRatioThreeFour"', html)
        self.assertNotIn('id="batchCornerStickerVisible"', html)
        self.assertNotIn('id="batchCornerStickerScale"', html)
        self.assertIn("excelVisualDimensions(row)", script)
        self.assertIn('row.visual === "enabled" ? 3 : 0', script)
        self.assertIn('dimensions.push(...excelVisualDimensions(row))', script)
        self.assertIn('key: "corner_sticker"', script)
        self.assertIn('apiFetch("/api/assets/corner-stickers")', script)

    def test_personal_asset_workspace_collects_selected_asset_kinds(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('name="workspaceMode" value="assets"', html)
        self.assertIn('name="personalAssetKind" value="corner_stickers"', html)
        self.assertIn('id="personalAssetDraftSelect"', html)
        self.assertIn('id="uploadPersonalAssetsBtn"', html)
        self.assertIn('upload: true', script)
        self.assertIn('server_url: window.location.origin', script)

    def test_health_status_is_not_coupled_to_optional_asset_loading(self) -> None:
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('health = await apiFetch("/api/health")', script)
        self.assertIn('const assetResults = await Promise.allSettled', script)
        self.assertIn('health.execution_mode === "embedded"', script)
        self.assertIn('"本机处理机在线"', script)
        self.assertIn('"网站服务离线"', script)
        self.assertNotIn('$("apiStatus").textContent = "处理机离线"', script)

    def test_local_output_panel_uses_a_separate_source_grid_row(self) -> None:
        styles = (FRONTEND_ROOT / "product.css").read_text(encoding="utf-8")

        self.assertIn(".source-section > #localOutputPanel { grid-column: 1; grid-row: 4; }", styles)
        self.assertIn(".source-section > .source-preview { grid-column: 2; grid-row: 2 / 5; }", styles)

    def test_cover_uses_preview_time_and_is_fixed_for_each_source(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")
        styles = (FRONTEND_ROOT / "product.css").read_text(encoding="utf-8")

        self.assertIn('id="useCover"', html)
        self.assertIn('id="useCover" type="checkbox" checked', html)
        self.assertIn('id="coverFrameTimeSeconds"', html)
        self.assertIn('id="usePreviewTimeBtn"', html)
        self.assertIn('id="coverTextLine1"', html)
        self.assertIn('id="coverTextLine2"', html)
        self.assertIn('id="editSingleCoverBtn"', html)
        self.assertIn('id="batchCoverOverlayAlpha"', html)
        self.assertIn('class="cover-safe-frame"', html)
        self.assertIn("job.cover = coverJobConfig", script)
        self.assertIn("frame_count: 3", script)
        self.assertIn("preview.currentTime.toFixed(2)", script)
        self.assertIn('["seeking", "seeked", "timeupdate"]', script)
        self.assertNotIn('dimensions.push({ key: "cover"', script)
        self.assertIn(".source-section > .cover-panel { grid-column: 1 / -1; grid-row: 5; }", styles)

    def test_batch_editor_can_choose_a_cover_frame_for_each_mother(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('id="batchDefaultCover"', html)
        self.assertIn('id="batchCoverDialog"', html)
        self.assertIn('id="batchCoverPreview"', html)
        self.assertIn("openBatchCoverEditor", script)
        self.assertIn("syncBatchCoverTimeFromPreview", script)
        self.assertIn("job.cover = coverJobConfig", script)
        self.assertIn('frame_source: "preview_material"', script)
        self.assertNotIn('dimensions.push({ key: "cover"', script)

    def test_visual_suite_and_cover_are_enabled_by_default(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="useVisualVariant" type="checkbox" checked', html)
        self.assertIn('<option value="enabled" selected>使用套装</option>', html)
        self.assertIn('<option value="enabled" selected>制作，每行单独选画面</option>', html)

    def test_audio_crop_and_corner_sticker_opacity_controls_are_exposed(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        for element_id in (
            "bgmVolume", "originalVolume", "cornerStickerOpacity",
            "cropOffsetY", "cropZoom", "batchBgmVolume", "batchOriginalVolume",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertNotIn('id="cornerStickerVisible"', html)
        self.assertNotIn('id="cornerStickerScale"', html)
        self.assertIn("const visibleRatio = 0.05", script)
        self.assertIn("const scale = 0.1", script)
        self.assertIn("stickerOpacity: 0.5", script)
        self.assertIn("original_video_volume", script)
        self.assertIn("crop_offset_y", script)
        self.assertIn("opacity", script)

    def test_every_range_control_gets_a_synchronized_number_input(self) -> None:
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")
        styles = (FRONTEND_ROOT / "product.css").read_text(encoding="utf-8")

        self.assertIn("function enhanceRangeInputs()", script)
        self.assertIn("function syncRangeNumberInputs()", script)
        self.assertIn("numberInput.type = \"number\"", script)
        self.assertIn('document.querySelectorAll(\'input[type="range"]\')', script)
        self.assertIn('range.dispatchEvent(new Event("input", { bubbles: true }))', script)
        self.assertIn("enhanceRangeInputs();", script)
        self.assertIn(".range-with-number", styles)
        self.assertIn(".range-number-input", styles)

    def test_local_asset_manager_includes_templates_and_seven_day_trash_copy(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('<option value="template">母版</option>', html)
        self.assertIn("删除后保留 7 天", html)
        self.assertIn('template: "母版"', script)
        self.assertIn('item.kind === "template" ? refreshMothers()', script)
        self.assertIn("回收站（7天后清理）", script)

    def test_batch_visual_controls_use_crop_preview_and_row_opacity(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")
        styles = (FRONTEND_ROOT / "product.css").read_text(encoding="utf-8")

        self.assertNotIn('id="openBatchCropPreviewBtn"', html)
        self.assertNotIn('id="openBatchCornerPreviewBtn"', html)
        self.assertIn('id="batchVisualPreviewDialog"', html)
        self.assertIn('id="batchVisualPreviewVideo"', html)
        self.assertNotIn('id="batchCornerPreviewStatus"', html)
        self.assertNotIn('id="batchCornerStickerPreview"', html)
        self.assertIn('cropButton.addEventListener("click", openBatchVisualPreview)', script)
        self.assertNotIn('cornerButton.addEventListener("click", openBatchVisualPreview)', script)
        self.assertIn("updateBatchVisualOpacity", script)
        self.assertIn("saveBatchVisualPreview", script)
        self.assertNotIn("sticker.content_bounds", script)
        self.assertIn(".batch-visual-cell", styles)
        self.assertIn(".batch-corner-opacity", styles)
        self.assertIn('class="batch-volume-stack"', html)
        self.assertIn(".batch-media-adjustments", styles)

    def test_cover_editor_uses_controls_instead_of_dragging(self) -> None:
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('id="batchCoverFont"', html)
        self.assertIn('id="batchCoverOverlayY"', html)
        self.assertIn('id="batchCoverLine1X"', html)
        self.assertIn('id="batchCoverLine2Y"', html)
        self.assertNotIn('id="batchCoverFrameScale"', html)
        self.assertNotIn("beginCoverDrag", script)

    def test_batches_request_random_unique_combinations(self) -> None:
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('selection: { mode: "random"', script)
        self.assertIn("随机抽取", script)

    def test_wide_batch_table_does_not_expand_the_page(self) -> None:
        styles = (FRONTEND_ROOT / "product.css").read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(0, 1fr)", styles)
        self.assertIn(".workspace > *, .section, .excel-preview { min-width: 0; }", styles)
        self.assertIn(".excel-table-wrap { width: 100%; min-width: 0; max-width: 100%; overflow-x: auto;", styles)

    def test_output_folder_prefers_the_embedded_processor_picker(self) -> None:
        script = (FRONTEND_ROOT / "product.js").read_text(encoding="utf-8")

        self.assertIn('apiFetch("/api/local/select-output-folder"', script)
        self.assertIn('collectorFetch("/api/local/select-output-folder"', script)
        self.assertIn('id="localOutputStatus"', (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
