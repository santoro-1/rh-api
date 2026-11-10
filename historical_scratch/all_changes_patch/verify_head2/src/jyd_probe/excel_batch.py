from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
import re
from typing import Any
from xml.etree import ElementTree as ET
import zipfile


MAX_EXCEL_BYTES = 5 * 1024 * 1024
MAX_EXCEL_FILES = 200
MAX_EXCEL_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_EXCEL_ROWS = 200

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_HEADER_ALIASES = {
    "enabled": {"启用", "是否启用"},
    "template": {"剪辑母版", "母版"},
    "audio": {"背景音乐", "音乐"},
    "effect": {"视频特效", "特效"},
    "sticker": {"全屏贴纸", "贴纸"},
    "font": {"字幕字体", "字体"},
    "count": {"生成数量", "数量"},
}
_OPTIONAL_HEADER_ALIASES = {"task_name": {"任务名称", "任务名"}}


def parse_excel_batch_workbook(content: bytes) -> dict[str, Any]:
    if not content:
        raise ValueError("Excel 文件为空")
    if len(content) > MAX_EXCEL_BYTES:
        raise ValueError("Excel 文件不能超过 5 MB")
    if not content.startswith(b"PK"):
        raise ValueError("请上传 .xlsx 格式的 Excel 文件")

    try:
        with zipfile.ZipFile(BytesIO(content), "r") as archive:
            _validate_archive(archive)
            shared_strings = _read_shared_strings(archive)
            sheet_name, sheet_path = _find_task_sheet(archive)
            rows = _read_sheet_rows(archive, sheet_path, shared_strings)
    except zipfile.BadZipFile as exc:
        raise ValueError("Excel 文件已损坏或不是有效的 .xlsx 文件") from exc

    header_index, header_map = _find_header(rows)
    parsed_rows: list[dict[str, Any]] = []
    for row_number, row in rows[header_index + 1 :]:
        record = {
            key: _cell_value(row.get(column_index, ""))
            for key, column_index in header_map.items()
        }
        if not any(str(value).strip() for value in record.values()):
            continue
        record["row_number"] = row_number
        parsed_rows.append(record)
        if len(parsed_rows) > MAX_EXCEL_ROWS:
            raise ValueError(f"Excel 最多支持 {MAX_EXCEL_ROWS} 行任务")

    if not parsed_rows:
        raise ValueError("Excel 中没有找到任务，请从表头下一行开始填写")
    return {
        "sheet_name": sheet_name,
        "header_row": rows[header_index][0],
        "rows": parsed_rows,
        "total_rows": len(parsed_rows),
    }


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_EXCEL_FILES:
        raise ValueError("Excel 内部文件数量异常")
    total_size = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Excel 包含不安全的文件路径")
        total_size += max(0, info.file_size)
        if total_size > MAX_EXCEL_UNCOMPRESSED_BYTES:
            raise ValueError("Excel 解压后内容过大")


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(f"{{{_MAIN_NS}}}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))
    return values


def _find_task_sheet(archive: zipfile.ZipFile) -> tuple[str, str]:
    try:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        relations_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise ValueError("Excel 缺少工作表结构") from exc

    targets = {
        relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
        for relation in relations_root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    sheets = workbook_root.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("Excel 中没有工作表")

    candidates: list[tuple[str, str]] = []
    for sheet in sheets.findall(f"{{{_MAIN_NS}}}sheet"):
        name = str(sheet.attrib.get("name", "")).strip()
        relation_id = sheet.attrib.get(f"{{{_REL_NS}}}id", "")
        target = targets.get(relation_id, "")
        if not target:
            continue
        normalized = target.lstrip("/")
        if not normalized.startswith("xl/"):
            normalized = f"xl/{normalized}"
        candidates.append((name, str(PurePosixPath(normalized))))

    if not candidates:
        raise ValueError("Excel 中没有可读取的工作表")
    return next((item for item in candidates if item[0] == "批量任务"), candidates[0])


def _read_sheet_rows(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[tuple[int, dict[int, Any]]]:
    try:
        root = ET.fromstring(archive.read(sheet_path))
    except KeyError as exc:
        raise ValueError("Excel 工作表文件不存在") from exc

    data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if data is None:
        return []
    rows: list[tuple[int, dict[int, Any]]] = []
    for fallback_row, row_node in enumerate(data.findall(f"{{{_MAIN_NS}}}row"), start=1):
        row_number = int(row_node.attrib.get("r", fallback_row) or fallback_row)
        values: dict[int, Any] = {}
        for fallback_column, cell in enumerate(row_node.findall(f"{{{_MAIN_NS}}}c")):
            reference = cell.attrib.get("r", "")
            column_index = _column_index(reference) if reference else fallback_column
            values[column_index] = _read_cell(cell, shared_strings)
        rows.append((row_number, values))
    return rows


def _read_cell(cell: ET.Element, shared_strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{_MAIN_NS}}}is")
        return "" if inline is None else "".join(
            node.text or "" for node in inline.iter(f"{{{_MAIN_NS}}}t")
        )
    value_node = cell.find(f"{{{_MAIN_NS}}}v")
    raw = "" if value_node is None else value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e"}:
        return raw
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", raw):
        return float(raw)
    return raw


def _column_index(reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", reference)
    if not match:
        return 0
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _find_header(
    rows: list[tuple[int, dict[int, Any]]],
) -> tuple[int, dict[str, int]]:
    for row_index, (_, values) in enumerate(rows[:20]):
        normalized = {
            str(value).strip().replace(" ", ""): column
            for column, value in values.items()
            if str(value).strip()
        }
        header_map: dict[str, int] = {}
        for key, aliases in _HEADER_ALIASES.items():
            column = next(
                (normalized[alias.replace(" ", "")] for alias in aliases if alias.replace(" ", "") in normalized),
                None,
            )
            if column is not None:
                header_map[key] = column
        for key, aliases in _OPTIONAL_HEADER_ALIASES.items():
            column = next(
                (normalized[alias.replace(" ", "")] for alias in aliases if alias.replace(" ", "") in normalized),
                None,
            )
            if column is not None:
                header_map[key] = column
        if all(key in header_map for key in _HEADER_ALIASES):
            return row_index, header_map

    required = "、".join(next(iter(aliases)) for aliases in _HEADER_ALIASES.values())
    raise ValueError(f"没有找到固定模板表头，请保留这些列：{required}")


def _cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value
