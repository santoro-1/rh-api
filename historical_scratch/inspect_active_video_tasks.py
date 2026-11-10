import json
import sqlite3


database = "/opt/runninghub-video/data/app.db"
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row


def columns(table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def value(alias, table_columns, name, output_name=None):
    output_name = output_name or name
    return (
        f"{alias}.{name} AS {output_name}"
        if name in table_columns
        else f"NULL AS {output_name}"
    )


enhancement_columns = columns("generation_task_enhancements")
task_columns = columns("generation_tasks")
attempt_columns = columns("generation_task_attempts")

enhancement_query = f"""
SELECT
    e.id AS enhancement_id,
    e.status AS enhancement_status,
    {value('e', enhancement_columns, 'remote_task_id')},
    {value('e', enhancement_columns, 'submitted_at')},
    {value('e', enhancement_columns, 'updated_at')},
    {value('e', enhancement_columns, 'auto_retry_after')},
    {value('e', enhancement_columns, 'auto_retry_count')},
    t.id AS generation_task_id,
    t.status AS generation_status,
    {value('t', task_columns, 'audio_duration_seconds')},
    {value('t', task_columns, 'runninghub_task_id', 'digital_human_remote_task_id')},
    i.row_key,
    s.segment_index,
    b.id AS batch_id,
    b.name AS batch_name
FROM generation_task_enhancements e
JOIN generation_tasks t ON t.id = e.generation_task_id
LEFT JOIN generation_segments s ON s.id = t.segment_id
LEFT JOIN generation_batch_items i ON i.id = COALESCE(s.batch_item_id, t.batch_item_id)
LEFT JOIN generation_batches b ON b.id = i.batch_id
WHERE e.status NOT IN ('SUCCESS', 'FAILED', 'CANCELLED')
ORDER BY COALESCE(e.updated_at, e.submitted_at) ASC
"""

active_enhancements = [dict(row) for row in connection.execute(enhancement_query)]

generation_query = f"""
SELECT
    t.id AS generation_task_id,
    t.status AS generation_status,
    {value('t', task_columns, 'runninghub_task_id', 'remote_task_id')},
    {value('t', task_columns, 'runninghub_submitted_at', 'submitted_at')},
    {value('t', task_columns, 'updated_at')},
    {value('t', task_columns, 'audio_duration_seconds')},
    i.row_key,
    s.segment_index,
    b.id AS batch_id,
    b.name AS batch_name,
    e.status AS enhancement_status,
    e.remote_task_id AS enhancement_remote_task_id
FROM generation_tasks t
LEFT JOIN generation_task_enhancements e ON e.generation_task_id = t.id
LEFT JOIN generation_segments s ON s.id = t.segment_id
LEFT JOIN generation_batch_items i ON i.id = COALESCE(s.batch_item_id, t.batch_item_id)
LEFT JOIN generation_batches b ON b.id = i.batch_id
WHERE t.status IN ('UPLOADING', 'SUBMITTED', 'RUNNING')
ORDER BY COALESCE(t.updated_at, t.runninghub_submitted_at) ASC
"""

active_generations = [dict(row) for row in connection.execute(generation_query)]

print(
    json.dumps(
        {
            "active_enhancements": active_enhancements,
            "active_generation_tasks": active_generations,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
)
