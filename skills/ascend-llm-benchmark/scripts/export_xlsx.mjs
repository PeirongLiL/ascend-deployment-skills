#!/usr/bin/env node
// Standard 29-column performance workbook; run with the supported Artifact Tool runtime.
import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const args = process.argv.slice(2);
if (args.length < 2 || args[0] === "--help") {
  console.log("Usage: node export_xlsx.mjs SUMMARY.json OUTPUT.xlsx [--preview]");
  process.exit(args[0] === "--help" ? 0 : 2);
}
const [source, output] = args;
try { await fs.access(output); throw new Error("Output exists; choose a new path"); }
catch (e) { if (e.code !== "ENOENT") throw e; }
const data = JSON.parse(await fs.readFile(source, "utf8"));
if (!Array.isArray(data.rows) || !data.rows.length) throw new Error("No measured cases to export");
const headers = [
  "模型", "并行度", "提示长度", "输出长度", "测试总时长(s)", "总请求数", "成功请求数", "失败请求数",
  "平均吞吐（token/s）", "总吞吐量(token/s)", "平均QPS", "Goodput(QPS)", "平均延迟(s)", "首token平均延迟(s)",
  "单token平均延迟(s)", "平均输入token数", "平均输出token数", "P50延迟(s)", "P50首token延迟(s)",
  "P50单token延迟(s)", "P90延迟(s)", "P90首token延迟(s)", "P90单token延迟(s)", "P99延迟(s)",
  "P99首token延迟(s)", "P99单token延迟(s)", "API URL", "温度", "流式输出"
];
function literal(v) { return typeof v === "string" && /^[=+@]/.test(v) ? "'" + v : v; }
const rows = data.rows.map(row => headers.map(h => literal(row[h] ?? null)));
const wb = Workbook.create();
const sh = wb.worksheets.add("工作表 1");
sh.showGridLines = true;
const last = rows.length + 1;
sh.getRange(`A1:AC${last}`).values = [headers, ...rows];
sh.getRange(`A1:AC${last}`).format = { font: { name: "Arial", size: 10 }, rowHeight: 24, columnWidth: 21, verticalAlignment: "center" };
sh.getRange("A1:AC1").format = { fill: "#BFBFBF", font: { bold: true, color: "#111111" }, wrapText: true,
  horizontalAlignment: "center", rowHeight: 44, borders: { preset: "all", style: "thin", color: "#A6A6A6" } };
sh.getRange(`A2:AC${last}`).format.borders = { preset: "all", style: "thin", color: "#D0D0D0" };
sh.getRange(`B2:AB${last}`).format.numberFormat = "0.0000";
sh.getRange(`B2:D${last}`).format.numberFormat = "0";
sh.getRange(`F2:H${last}`).format.numberFormat = "0";
sh.getRange(`P2:Q${last}`).format.numberFormat = "0.0";
sh.getRange(`AA1:AA${last}`).format.columnWidth = 48;
sh.freezePanes.freezeRows(1);
sh.tables.add(`A1:AC${last}`, true, "PerformanceResults");
const conditions = wb.worksheets.add("运行条件");
conditions.showGridLines = false;
const c = data.config ?? {};
const metadata = [
  ["字段", "值"], ["状态", data.state ?? "未知"], ["开始时间(UTC)", data.started_at ?? null],
  ["结束时间(UTC)", data.ended_at ?? null], ["端点模式", c.endpoint ?? "completions"],
  ["缓存场景", c.cache_mode ?? "未知"], ["SLO", JSON.stringify(c.slo ?? {})],
  ["预热请求数/工况", c.warmup_requests ?? null], ["预热输出 token", c.warmup_output_tokens ?? "与工况相同"],
  ["重复次数", c.repeats ?? 1], ["部署配置", JSON.stringify(c.deployment ?? {})],
  ["统计说明", "主表仅含正式请求；空白为未测得；部分完成用例不代表完整协议已完成。"],
  ["时延口径", "TTFT 为首有效内容；TPOT 到末有效内容；E2E 到完整流结束；未将 SSE chunk 当 token。"],
  ["来源", path.basename(source)]
].map(row => row.map(literal));
conditions.getRange(`A1:B${metadata.length}`).values = metadata;
conditions.getRange(`A1:B${metadata.length}`).format = { font: { name: "Arial", size: 10 }, rowHeight: 25, verticalAlignment: "center" };
conditions.getRange(`A1:A${metadata.length}`).format.columnWidth = 24;
conditions.getRange(`B1:B${metadata.length}`).format.columnWidth = 96;
conditions.getRange("A1:B1").format = { fill: "#BFBFBF", font: { bold: true } };
conditions.getRange("B3:B4").format.numberFormat = "yyyy-mm-dd hh:mm:ss";
conditions.getRange(`B2:B${metadata.length}`).format.wrapText = true;
conditions.getRange(`A2:B${metadata.length}`).format.autofitRows();
wb.recalculate();
const check = await wb.inspect({ kind: "table", range: `工作表 1!A1:Q${Math.min(last, 5)}`, include: "values", tableMaxRows: 5, tableMaxCols: 17 });
await fs.mkdir(path.dirname(output), { recursive: true });
await fs.writeFile(output + ".inspect.ndjson", check.ndjson);
const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!", options: { useRegex: true, maxResults: 30 } });
await fs.writeFile(output + ".errors.ndjson", errors.ndjson);
if (args.includes("--preview")) {
  for (const [name, sheetName, range] of [
    ["conditions", "运行条件", `A1:B${metadata.length}`],
    ["metrics", "工作表 1", `I1:Q${Math.min(last, 8)}`],
    ["requests", "工作表 1", `A1:H${Math.min(last, 8)}`]
  ]) {
    const image = await wb.render({ sheetName, range, scale: 1.5, format: "png" });
    await fs.writeFile(output + `.${name}.png`, new Uint8Array(await image.arrayBuffer()));
  }
}
await (await SpreadsheetFile.exportXlsx(wb)).save(output);
console.log(JSON.stringify({ output, cases: rows.length, columns: headers.length }));
