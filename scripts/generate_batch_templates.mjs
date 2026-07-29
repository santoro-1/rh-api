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
    headers: ["任务编号", "口播脚本"],
    sample: [
      "SCRIPT-001",
      "今天给大家介绍这款产品。请按照一行一条完整脚本填写。",
    ],
    widths: [22, 80],
    notes: [
      ["项目", "说明"],
      ["任务编号", "每行必须唯一，用于历史记录和分段子任务归类。"],
      ["口播脚本", "每行填写一条完整口播脚本；系统只调用一次 MiniMax，再按约 30 秒、最长 45 秒切分音频。"],
      ["素材顺序", "图片或视频必须按照表格行顺序上传；提交前页面会再次显示配对结果。"],
    ],
  },
  {
    key: "digital_human",
    headers: ["任务编号", "提示词"],
    sample: [
      "TASK-001",
      "人物自然地说话，镜头保持稳定。",
    ],
    widths: [22, 80],
    notes: [
      ["项目", "说明"],
      ["表格内容", "只填写任务编号和提示词，不填写任何图片或音频文件名。"],
      ["素材对应", "图片和总参考音频在网页分开上传；各自第 1、2、3……项对应表格第 1、2、3……行。"],
      ["双人模式", "左人物音频、右人物音频也分别上传，并按各自序号对应同一行。"],
    ],
  },
  {
    key: "ltx_lip_sync",
    headers: ["任务编号", "口播脚本"],
    sample: [
      "TASK-001",
      "今天给大家介绍这款产品。",
    ],
    widths: [22, 80],
    notes: [
      ["项目", "说明"],
      ["表格内容", "只填写任务编号和口播脚本，不填写视频或音频文件名。"],
      ["素材对应", "视频和音频在网页分开上传；各自第 1、2、3……项对应表格第 1、2、3……行。"],
      ["口播脚本", "只填一次；系统自动用于对口型，生成语音模式下还会用它生成音频。"],
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
  noteSheet.getRange(`A1:A${spec.notes.length}`).format.columnWidth = 24;
  noteSheet.getRange(`A2:A${spec.notes.length}`).format.wrapText = false;
  noteSheet.getRange(`B1:B${spec.notes.length}`).format.columnWidth = 72;
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
