"""Shared plain-text evidence presentation; never loads remote resources."""

from chengchuan.domain.models import SHANGHAI


def evidence_text(table, record=None):
    m = table.meta
    lines = [
        table.title,
        "",
        f"来源：{m.source}",
        m.source_url,
        f"业务日：{m.business_day or '源未提供'}",
        f"源更新时间：{m.source_updated_at or '未提供'}",
        f"源发布时间：{m.published_at or '未提供'}",
        f"取得时间：{m.fetched_at.astimezone(SHANGHAI).isoformat(timespec='seconds')}",
        f"首次采集：{m.first_seen_at or '未归档'}",
        f"版本：{m.revision[:16] or '未归档'}",
        f"覆盖：{m.coverage or '源未提供总数'}",
        "完整性：" + ("未发现截断" if m.complete else "部分／不可用"),
        "本机缓存" if m.cached else "本次接口取得",
        "",
        table.notice,
    ]
    if record is not None:
        lines.extend(("", "所选记录"))
        lines.extend(
            f"{c.caption}：{record.get(c.key) if record.get(c.key) is not None else '—'}"
            for c in table.columns
        )
    if table.document:
        lines.extend(("", table.document))
    return "\n".join(lines)
