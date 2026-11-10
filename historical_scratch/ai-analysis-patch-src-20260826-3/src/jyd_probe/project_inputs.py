from __future__ import annotations

import csv
from io import BytesIO, StringIO
from pathlib import PurePosixPath
import re
from typing import Any
from xml.etree import ElementTree as ET
import zipfile


MAX_SCRIPT_FILE_BYTES = 5 * 1024 * 1024
MAX_SCRIPT_ROWS = 500
MAX_XLSX_FILES = 250
MAX_XLSX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_PROJECT_IMAGE_BYTES = 200 * 1024 * 1024

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_ROW_KEY_HEADERS = {
    "id",
    "任务id",
    "任务编号",
    "编号",
    "序号",
    "脚本id",
    "脚本编号",
    "rowkey",
    "row_key",
}
_SCRIPT_HEADERS = {
    "脚本内容",
    "脚本",
    "文案",
    "口播文案",
    "内容",
    "script",
    "scripttext",
    "script_text",
}
_ARTICLE_TYPE_HEADERS = {
    "文章类型",
    "内容类型",
    "脚本类型",
    "article_type",
    "articletype",
}
_ASSIGNED_ACCOUNT_HEADERS = {
    "分配账号",
    "账号",
    "账号编号",
    "assigned_account",
    "assignedaccount",
}


def parse_project_script_file(content: bytes, filename: str) -> dict[str, Any]:
    clean_filename = str(filename or "").strip()
    if not content:
        raise ValueError("脚本文件为空")
    if len(content) > MAX_SCRIPT_FILE_BYTES:
        raise ValueError("脚本文件不能超过 5 MB")

    suffix = PurePosixPath(clean_filename.replace("\\", "/")).suffix.lower()
    if suffix == ".csv":
        table, source_name = _read_csv(content), "CSV"
    elif suffix == ".xlsx":
        table, source_name = _read_xlsx(content)
    elif suffix == ".xls":
        raise ValueError("暂不支持旧版 .xls，请另存为 .xlsx 或 .csv 后上传")
    else:
        raise ValueError("只支持 .xlsx 和 .csv 脚本文件")

    (
        header_index,
        row_key_column,
        script_column,
        article_type_column,
        assigned_account_column,
    ) = _find_headers(table)
    metadata_columns = {
        column
        for column in (article_type_column, assigned_account_column)
        if column is not None
    }
    allowed_columns = {row_key_column, script_column, *metadata_columns}
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row_number, values in table[header_index + 1 :]:
        if not any(str(value).strip() for value in values):
            continue
        if any(
            str(value).strip()
            for column, value in enumerate(values)
            if column not in allowed_columns
        ):
            raise ValueError(
                f"第 {row_number} 行包含多余列，模板只允许填写"
                "任务ID、脚本内容、文章类型、分配账号"
            )
        row_key = _text_at(values, row_key_column)
        script_text = _text_at(values, script_column)
        if not row_key:
            raise ValueError(f"第 {row_number} 行的 ID 不能为空")
        if not script_text:
            raise ValueError(f"第 {row_number} 行的脚本内容不能为空")
        if len(row_key) > 80:
            raise ValueError(f"第 {row_number} 行的 ID 不能超过 80 个字符")
        if len(script_text) > 50_000:
            raise ValueError(f"第 {row_number} 行的脚本不能超过 50000 个字符")
        article_type = (
            _text_at(values, article_type_column)
            if article_type_column is not None
            else ""
        )
        assigned_account = (
            _text_at(values, assigned_account_column)
            if assigned_account_column is not None
            else ""
        )
        if article_type_column is not None:
            if not article_type:
                raise ValueError(f"第 {row_number} 行的文章类型不能为空")
            if not assigned_account:
                raise ValueError(f"第 {row_number} 行的分配账号不能为空")
            if len(article_type) > 120:
                raise ValueError(f"第 {row_number} 行的文章类型不能超过 120 个字符")
            if len(assigned_account) > 120:
                raise ValueError(f"第 {row_number} 行的分配账号不能超过 120 个字符")
        if row_key in seen:
            raise ValueError(f"脚本行编号重复: {row_key}")
        seen.add(row_key)
        parsed_row = {"row_key": row_key, "script_text": script_text}
        if article_type_column is not None:
            parsed_row.update(
                {
                    "article_type": article_type,
                    "assigned_account": assigned_account,
                }
            )
        rows.append(parsed_row)
        if len(rows) > MAX_SCRIPT_ROWS:
            raise ValueError(f"单个项目最多包含 {MAX_SCRIPT_ROWS} 条脚本")

    if not rows:
        raise ValueError("脚本文件中没有可导入的数据")
    return {
        "schema": "jyd.project-script-preview.v1",
        "filename": clean_filename,
        "source_name": source_name,
        "header_row": table[header_index][0],
        "total_rows": len(rows),
        "rows": rows,
    }


def detect_project_image(content: bytes, filename: str) -> tuple[str, str]:
    if not content:
        raise ValueError("图片文件为空")
    if len(content) > MAX_PROJECT_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 200 MB")
    suffix = PurePosixPath(str(filename or "").replace("\\", "/")).suffix.lower()
    if content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"):
        detected, content_type, allowed = ".jpg", "image/jpeg", {".jpg", ".jpeg"}
    elif (
        len(content) >= 33
        and content.startswith(b"\x89PNG\r\n\x1a\n")
        and content[12:16] == b"IHDR"
        and int.from_bytes(content[16:20], "big") > 0
        and int.from_bytes(content[20:24], "big") > 0
    ):
        detected, content_type, allowed = ".png", "image/png", {".png"}
    elif len(content) >= 16 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        detected, content_type, allowed = ".webp", "image/webp", {".webp"}
    else:
        raise ValueError("图片内容无效，只支持 JPG、PNG、WEBP")
    if suffix and suffix not in allowed:
        raise ValueError("图片扩展名与文件内容不一致")
    return content_type, detected


def _normalize_header(value: Any) -> str:
    return re.sub(r"[\s\-]+", "", str(value or "").strip().lower())


def _text_at(values: list[Any], index: int) -> str:
    if index >= len(values):
        return ""
    value = values[index]
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value if value is not None else "").strip()


def _find_headers(
    table: list[tuple[int, list[Any]]],
) -> tuple[int, int, int, int | None, int | None]:
    normalized_row_keys = {_normalize_header(value) for value in _ROW_KEY_HEADERS}
    normalized_scripts = {_normalize_header(value) for value in _SCRIPT_HEADERS}
    normalized_article_types = {
        _normalize_header(value) for value in _ARTICLE_TYPE_HEADERS
    }
    normalized_assigned_accounts = {
        _normalize_header(value) for value in _ASSIGNED_ACCOUNT_HEADERS
    }
    for index, (_, values) in enumerate(table[:20]):
        normalized = [_normalize_header(value) for value in values]
        row_key_column = next(
            (column for column, value in enumerate(normalized) if value in normalized_row_keys),
            None,
        )
        script_column = next(
            (column for column, value in enumerate(normalized) if value in normalized_scripts),
            None,
        )
        if row_key_column is not None and script_column is not None:
            article_type_column = next(
                (
                    column
                    for column, value in enumerate(normalized)
                    if value in normalized_article_types
                ),
                None,
            )
            assigned_account_column = next(
                (
                    column
                    for column, value in enumerate(normalized)
                    if value in normalized_assigned_accounts
                ),
                None,
            )
            if (article_type_column is None) != (assigned_account_column is None):
                raise ValueError("文章类型、分配账号两列必须同时存在")
            expected_columns = {row_key_column, script_column}
            if article_type_column is not None:
                expected_columns.update({article_type_column, assigned_account_column})
            actual_columns = {
                column for column, value in enumerate(values) if str(value).strip()
            }
            if actual_columns != expected_columns:
                raise ValueError(
                    "脚本模板只能包含任务ID、脚本内容，或再加文章类型、分配账号两列"
                )
            return (
                index,
                row_key_column,
                script_column,
                article_type_column,
                assigned_account_column,
            )
    raise ValueError(
        "没有找到固定表头，请保留任务ID、脚本内容、文章类型、分配账号四列"
    )


def _read_csv(content: bytes) -> list[tuple[int, list[Any]]]:
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text:
        raise ValueError("CSV 编码无效，请使用 UTF-8 或 GB18030")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    return [
        (row_number, list(values))
        for row_number, values in enumerate(csv.reader(StringIO(text), dialect), start=1)
    ]


def _read_xlsx(content: bytes) -> tuple[list[tuple[int, list[Any]]], str]:
    if not content.startswith(b"PK"):
        raise ValueError("请上传有效的 .xlsx 文件")
    try:
        with zipfile.ZipFile(BytesIO(content), "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_XLSX_FILES:
                raise ValueError("Excel 内部文件数量异常")
            total_size = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Excel 包含不安全的文件路径")
                total_size += max(0, info.file_size)
                if total_size > MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise ValueError("Excel 解压后内容过大")
            shared_strings = _xlsx_shared_strings(archive)
            sheet_name, sheet_path = _xlsx_first_sheet(archive)
            return _xlsx_rows(archive, sheet_path, shared_strings), sheet_name
    except zipfile.BadZipFile as exc:
        raise ValueError("Excel 文件已损坏或不是有效的 .xlsx 文件") from exc


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _xlsx_first_sheet(archive: zipfile.ZipFile) -> tuple[str, str]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise ValueError("Excel 缺少工作表结构") from exc
    targets = {
        node.attrib.get("Id", ""): node.attrib.get("Target", "")
        for node in relations.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("Excel 中没有工作表")
    sheet = next(iter(sheets.findall(f"{{{_MAIN_NS}}}sheet")), None)
    if sheet is None:
        raise ValueError("Excel 中没有工作表")
    name = str(sheet.attrib.get("name", "Sheet1"))
    target = targets.get(sheet.attrib.get(f"{{{_REL_NS}}}id", ""), "")
    normalized = target.lstrip("/")
    if not normalized.startswith("xl/"):
        normalized = f"xl/{normalized}"
    return name, str(PurePosixPath(normalized))


def _xlsx_rows(
    archive: zipfile.ZipFile, sheet_path: str, shared_strings: list[str]
) -> list[tuple[int, list[Any]]]:
    try:
        root = ET.fromstring(archive.read(sheet_path))
    except KeyError as exc:
        raise ValueError("Excel 工作表文件不存在") from exc
    data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if data is None:
        return []
    result: list[tuple[int, list[Any]]] = []
    for fallback_row, row_node in enumerate(data.findall(f"{{{_MAIN_NS}}}row"), start=1):
        row_number = int(row_node.attrib.get("r", fallback_row) or fallback_row)
        cells: dict[int, Any] = {}
        for fallback_column, cell in enumerate(row_node.findall(f"{{{_MAIN_NS}}}c")):
            reference = cell.attrib.get("r", "")
            column = _xlsx_column(reference) if reference else fallback_column
            cells[column] = _xlsx_cell(cell, shared_strings)
        width = max(cells, default=-1) + 1
        result.append((row_number, [cells.get(index, "") for index in range(width)]))
    return result


def _xlsx_cell(cell: ET.Element, shared_strings: list[str]) -> Any:
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
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", raw):
        return float(raw)
    return raw


def _xlsx_column(reference: str) -> int:
    match = re.match(r"([A-Za-z]+)", reference)
    if not match:
        return 0
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1
