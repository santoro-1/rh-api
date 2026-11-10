from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from lxml import etree


INPUTS = [
    Path(r"C:\Users\san\Desktop\2026-08-08.docx"),
    Path(r"C:\Users\san\Desktop\2026-08-10-文案.docx"),
]
OUTPUT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\script_inventory_analysis\documents.json")
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def table_rows(table):
    return [[cell.text for cell in row.cells] for row in table.rows]


def xml_text(path: Path) -> list[str]:
    with ZipFile(path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.xpath(".//w:p", namespaces={"w": W_NS}):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces={"w": W_NS})).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


results = []
for path in INPUTS:
    document = Document(path)
    results.append(
        {
            "input_path": str(path),
            "paragraphs": [p.text for p in document.paragraphs if p.text.strip()],
            "tables": [table_rows(table) for table in document.tables],
            "headers": [
                p.text
                for section in document.sections
                for p in section.header.paragraphs
                if p.text.strip()
            ],
            "footers": [
                p.text
                for section in document.sections
                for p in section.footer.paragraphs
                if p.text.strip()
            ],
            "xml_paragraphs": xml_text(path),
        }
    )

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        [
            {
                "input_path": item["input_path"],
                "paragraphs": len(item["paragraphs"]),
                "tables": len(item["tables"]),
                "xml_paragraphs": len(item["xml_paragraphs"]),
            }
            for item in results
        ],
        ensure_ascii=False,
        indent=2,
    )
)
