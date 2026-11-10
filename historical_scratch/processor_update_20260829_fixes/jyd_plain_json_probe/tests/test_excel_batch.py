from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.excel_batch import parse_excel_batch_workbook
from jyd_probe.web_api import _expand_excel_batch_payload


def _inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _number_cell(reference: str, value: int) -> str:
    return f'<c r="{reference}"><v>{value}</v></c>'


def build_workbook(rows: list[list[str | int]]) -> bytes:
    xml_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(values):
            if value == "":
                continue
            reference = f"{chr(ord('A') + column_index)}{row_index}"
            cells.append(
                _number_cell(reference, value)
                if isinstance(value, int)
                else _inline_cell(reference, value)
            )
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="批量任务" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        '</Relationships>'
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


class ExcelBatchParserTest(unittest.TestCase):
    def test_reads_fixed_template_without_task_name_or_ids(self) -> None:
        content = build_workbook(
            [
                ["剪映批量任务模板", "", "", "", "", "", ""],
                ["启用", "剪辑母版", "背景音乐", "视频特效", "全屏贴纸", "字幕字体", "生成数量"],
                ["是", "母版A", "分类轮换：轻快", "全部轮换", "不替换", "固定：字体A", 20],
                ["否", "母版B", "不替换", "固定：特效A", "全部轮换", "不替换", ""],
                ["", "", "", "", "", "", ""],
            ]
        )

        result = parse_excel_batch_workbook(content)

        self.assertEqual(result["sheet_name"], "批量任务")
        self.assertEqual(result["header_row"], 2)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(result["rows"][0]["template"], "母版A")
        self.assertEqual(result["rows"][0]["count"], 20)
        self.assertNotIn("task_name", result["rows"][0])

    def test_rejects_workbook_without_fixed_headers(self) -> None:
        content = build_workbook([["随便的表头", "值"]])
        with self.assertRaisesRegex(ValueError, "没有找到固定模板表头"):
            parse_excel_batch_workbook(content)


class ExcelBatchExpansionTest(unittest.TestCase):
    @staticmethod
    def row(name: str, row_number: int) -> dict:
        return {
            "enabled": True,
            "row_number": row_number,
            "task_name": name,
            "job": {"source": {"type": "template", "template_id": name}, "output": {}},
            "dimensions": [
                {
                    "key": "bgm",
                    "label": "音乐",
                    "mode": "product",
                    "candidates": [
                        {"id": "m1", "append": {"audios": [{"library_identity": "m1"}]}},
                        {"id": "m2", "append": {"audios": [{"library_identity": "m2"}]}},
                    ],
                },
                {
                    "key": "effect",
                    "label": "特效",
                    "mode": "fixed",
                    "candidates": [
                        {"id": "e1", "append": {"effects": [{"effect_json_path": "e1.json"}]}},
                    ],
                },
            ],
            "selection": {"mode": "balanced", "limit": 2},
        }

    def test_interleaves_outputs_from_excel_rows(self) -> None:
        jobs, variants = _expand_excel_batch_payload(
            {"rows": [self.row("母版A", 5), self.row("母版B", 6)], "max_jobs": 500}
        )

        self.assertEqual(len(jobs), 4)
        self.assertEqual(
            [variant["task_name"] for variant in variants],
            ["母版A", "母版B", "母版A", "母版B"],
        )
        self.assertEqual(
            [variant["excel_row_number"] for variant in variants],
            [5, 6, 5, 6],
        )
        self.assertEqual([variant["row_output_index"] for variant in variants], [1, 1, 2, 2])


if __name__ == "__main__":
    unittest.main()
