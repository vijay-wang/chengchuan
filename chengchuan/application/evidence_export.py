"""Atomic evidence export shared by market research and company dossiers."""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path


def export_bundle(bundle, path, title, markdown_tables=None):
    """Export user-selected evidence atomically; preserve metadata for other AI tools."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        text = json.dumps(
            asdict(bundle),
            ensure_ascii=False,
            indent=2,
            default=lambda v: v.isoformat(),
            allow_nan=False,
        )
    else:
        parts = [f"# 澄川研究卡 · {title}", "", str(bundle.request), "", *bundle.notes]
        for table in markdown_tables if markdown_tables is not None else bundle.tables:
            meta = table.meta
            parts.extend(
                (
                    "",
                    "## " + table.title,
                    "",
                    f"来源：{meta.source} {meta.source_url}",
                    f"业务日：{meta.business_day or '未提供'}；源更新：{meta.source_updated_at or '未提供'}；"
                    f"源发布：{meta.published_at or '未提供'}",
                    f"取得：{meta.fetched_at.isoformat()}；首次采集：{meta.first_seen_at}；本次观察：{meta.observed_at}；版本：{meta.revision}",
                    f"完整：{meta.complete}；覆盖：{meta.coverage}；{table.notice}",
                    "",
                )
            )
            if table.document:
                parts.append(table.document)
            if table.columns:

                def escape(s):
                    return (
                        str(s if s is not None else "—").replace("|", "\\|").replace("\n", "<br>")
                    )

                parts.extend(
                    (
                        "| " + " | ".join(escape(c.caption) for c in table.columns) + " |",
                        "| " + " | ".join("---" for _ in table.columns) + " |",
                    )
                )
                parts.extend("| " + " | ".join(escape(v) for v in row) + " |" for row in table.rows)
        text = "\n".join(parts) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".research-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
