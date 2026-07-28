import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const projectRoot = path.resolve(import.meta.dirname, "..");
const targetDir = path.join(projectRoot, "app", "static", "templates");
const previewDir = path.join(projectRoot, "tests", ".template-preview");
const artifactOutputDir =
  process.env.BATCH_TEMPLATE_OUTPUT_DIR || path.join(previewDir, "outputs");

await fs.mkdir(targetDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
await fs.mkdir(artifactOutputDir, { recursive: true });

const templates = [
  {
    key: "script",
    headers: ["脚本编号", "脚本内容"],
    sample: [
      "SCRIPT-001",
      "今天给大家介绍这款产品。请按照一行一条完整脚本填写。",
    ],
    widths: [18, 80],
    notes: [
      ["项目", "说明"],
      ["脚本编号", "每行必须唯一，用于历史记录和分段子任务归类。"],
      ["脚本内容", "填写一条完整口播脚本；系统只调用一次 MiniMax，再按约 30 秒、最长 45 秒切分音频。"],
      ["素材顺序", "图片或视频必须按照表格行顺序上传；提交前页面会再次显示配对结果。"],
    ],
  },
  {
    key: "digital_human",
    headers: [
      "任务编号",
      "图片文件",
      "总参考音频（上传模式填写）",
      "口播脚本（语音生成模式填写）",
      "提示词",
      "左人物音频",
      "右人物音频",
    ],
    sample: [
      "TASK-001",
      "person-001.png",
      "voice-001.mp3",
      "今天给大家介绍这款产品。",
      "人物自然地说话，镜头保持稳定。",
      "",
      "",
    ],
    widths: [16, 24, 28, 42, 42, 22, 22],
    notes: [
      ["适用模式", "说明"],
      ["上传音频", "填写图片文件、总参考音频和提示词；口播脚本可以留空。"],
      ["脚本生成语音", "填写图片文件、口播脚本和提示词；总参考音频留空。只支持单人模式。"],
      ["文件匹配", "文件名必须包含扩展名，并与上传文件的完整名称一致。"],
    ],
  },
  {
    key: "ltx_lip_sync",
    headers: [
      "任务编号",
      "源视频文件",
      "音频文件（上传模式填写）",
      "口播脚本（语音生成模式填写）",
      "视频正向提示词",
    ],
    sample: [
      "TASK-001",
      "source-001.mp4",
      "voice-001.mp3",
      "今天给大家介绍这款产品。",
      "一名女性用中文说：“今天给大家介绍这款产品。”",
    ],
    widths: [16, 24, 28, 42, 52],
    notes: [
      ["适用模式", "说明"],
      ["上传音频", "填写源视频、音频文件和视频正向提示词；口播脚本可以留空。"],
      ["脚本生成语音", "填写源视频、口播脚本和视频正向提示词；音频文件留空。"],
      ["正向提示词", "只写人物、语言和与口播脚本完全一致的台词，不写动作、镜头或画面。"],
    ],
  },
];

for (const spec of templates) {
  const existingPath = path.join(targetDir, `${spec.key}-batch-template.xlsx`);
  try {
    const existing = await SpreadsheetFile.importXlsx(
      await FileBlob.load(existingPath),
    );
    const before = await existing.render({
      sheetName: existing.worksheets.getItemAt(0).name,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    await fs.writeFile(
      path.join(previewDir, `${spec.key}-before.png`),
      new Uint8Array(await before.arrayBuffer()),
    );
  } catch {
    // The first generation may not have an existing template to preview.
  }

  const workbook = Workbook.create();
  const taskSheet = workbook.worksheets.add("批量任务");
  taskSheet.showGridLines = false;
  taskSheet.getRangeByIndexes(0, 0, 2, spec.headers.length).values = [
    spec.headers,
    spec.sample,
  ];
  taskSheet.getRangeByIndexes(0, 0, 1, spec.headers.length).format = {
    fill: "#1769D1",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
    rowHeight: 34,
    borders: { preset: "outside", style: "thin", color: "#0E4A98" },
  };
  taskSheet.getRangeByIndexes(1, 0, 1, spec.headers.length).format = {
    fill: "#F5F8FC",
    wrapText: true,
    rowHeight: 46,
    borders: {
      insideHorizontal: { style: "thin", color: "#D9E2EC" },
      bottom: { style: "thin", color: "#D9E2EC" },
    },
  };
  spec.widths.forEach((width, index) => {
    taskSheet.getRangeByIndexes(0, index, 2, 1).format.columnWidth = width;
  });
  taskSheet.freezePanes.freezeRows(1);

  const noteSheet = workbook.worksheets.add("使用说明");
  noteSheet.showGridLines = false;
  noteSheet.getRangeByIndexes(0, 0, spec.notes.length, 2).values = spec.notes;
  noteSheet.getRange("A1:B1").format = {
    fill: "#1769D1",
    font: { bold: true, color: "#FFFFFF" },
    rowHeight: 28,
  };
  noteSheet.getRange(`A2:B${spec.notes.length}`).format = {
    wrapText: true,
    borders: {
      insideHorizontal: { style: "thin", color: "#D9E2EC" },
    },
  };
  noteSheet.getRange("A1:A10").format.columnWidth = 20;
  noteSheet.getRange("B1:B10").format.columnWidth = 72;
  noteSheet.freezePanes.freezeRows(1);

  const inspected = await workbook.inspect({
    kind: "table",
    sheetId: "批量任务",
    range: `A1:${String.fromCharCode(64 + spec.headers.length)}2`,
    include: "values,formulas",
    tableMaxRows: 4,
    tableMaxCols: 10,
  });
  process.stdout.write(`${spec.key}: ${inspected.ndjson}\n`);

  const after = await workbook.render({
    sheetName: "批量任务",
    autoCrop: "all",
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(
    path.join(previewDir, `${spec.key}-after.png`),
    new Uint8Array(await after.arrayBuffer()),
  );
  const notesPreview = await workbook.render({
    sheetName: "使用说明",
    autoCrop: "all",
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(
    path.join(previewDir, `${spec.key}-notes.png`),
    new Uint8Array(await notesPreview.arrayBuffer()),
  );

  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(existingPath);
  await output.save(
    path.join(artifactOutputDir, `${spec.key}-batch-template.xlsx`),
  );
  // Some artifact-tool runtimes externalize verbose inspection output next to
  // the workbook. These diagnostics are not part of the downloadable template.
  await fs.rm(`${existingPath}.inspect.ndjson`, { force: true });
  await fs.rm(
    path.join(
      artifactOutputDir,
      `${spec.key}-batch-template.xlsx.inspect.ndjson`,
    ),
    { force: true },
  );
}
