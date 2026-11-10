import json
import sqlite3

connection = sqlite3.connect("file:/opt/runninghub-video/data/app.db?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
tables = [
    "generation_task_enhancements",
    "generation_tasks",
    "generation_segments",
    "generation_batch_items",
    "generation_batches",
]
print(
    json.dumps(
        {
            table: [dict(row) for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in tables
        },
        ensure_ascii=False,
        indent=2,
    )
)
