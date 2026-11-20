"""Month-detail retention: purge per-shipment rows after the retention period.

A frozen month may be *purged* once the per-shipment ledger has passed its
retention period. Purging is deliberately narrow and all-or-nothing:

* the online ``shipments`` rows for that month (the 逐笔流水, including source
  row, canonical timestamp text and sequence numbers) are removed;
* a compact fact set is retained so zone summaries can still be recomputed
  under any historical or future knowledge version (only ``sku``, the UTC
  outbound instant and ``boxes`` -- one row per shipment, so multiple
  shipments of one SKU at the same instant still count separately);
* every sequence number is retained separately so re-uploads of that month
  and reuse of an old sequence number by another month keep being rejected.

The purge runs in one ``BEGIN IMMEDIATE`` transaction. Interleaved report
reads therefore observe either the complete pre-purge or the complete
post-purge state. If any write fails, the transaction rolls back and the
original monthly settlement is untouched. Repeating the confirmation is
idempotent: an already purged month simply returns the recorded result.
"""

from __future__ import annotations

import sqlite3

from .db import SHIPMENTS_DELETE_TRIGGER, transaction
from .errors import NotFoundError, ValidationError
from .timeutil import now_str, validate_month


def _validated_month(month):
    try:
        return validate_month(month)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _confirmed(confirmed):
    if isinstance(confirmed, bool):
        return confirmed
    return str(confirmed or "").strip().lower() in {"1", "true", "yes", "on", "确认"}


def purged_month_row(conn, month):
    return conn.execute(
        "SELECT * FROM purged_months WHERE month = ?", (month,)
    ).fetchone()


def is_purged(conn, month):
    return purged_month_row(conn, month) is not None


def list_purged_months(conn):
    return conn.execute(
        "SELECT month, purged_at, retained_row_count, retained_total_boxes"
        " FROM purged_months ORDER BY month DESC"
    ).fetchall()


def purge_month(conn, month, confirmed):
    """Purge one frozen month's shipment details.

    Returns a result dict. Repeating the call for an already purged month
    returns the stored result without changing anything.
    """
    month = _validated_month(month)
    if not _confirmed(confirmed):
        raise ValidationError("清理月结明细必须显式确认（参数 confirm 为真值）")

    with transaction(conn):
        month_row = conn.execute(
            "SELECT month, row_count, total_boxes FROM months WHERE month = ?",
            (month,),
        ).fetchone()
        if month_row is None:
            raise NotFoundError(f"月份 {month} 尚未上传出库事实，无法清理")

        existing = purged_month_row(conn, month)
        if existing is not None:
            # Idempotent confirmation: reuse the recorded purge result.
            return {
                "month": month,
                "purged_at": existing["purged_at"],
                "retained_row_count": existing["retained_row_count"],
                "retained_total_boxes": existing["retained_total_boxes"],
                "already_purged": True,
            }

        shipments = conn.execute(
            "SELECT sequence_no, sku, occurred_at_us, boxes"
            " FROM shipments WHERE month = ? ORDER BY id",
            (month,),
        ).fetchall()
        row_count = len(shipments)
        total_boxes = sum(row["boxes"] for row in shipments)
        if row_count != month_row["row_count"] or total_boxes != month_row["total_boxes"]:
            # Defensive: the immutable facts should always match the month row.
            raise ValidationError("月结汇总与逐笔流水不一致，已中止清理且未改动任何数据")

        purged_at = now_str()
        try:
            # The shipments DELETE trigger is the only path allowed to remove
            # ledger rows; suspend it for this transaction and reinstall it
            # before committing. DDL is transactional in SQLite, so a failure
            # at any point restores the trigger and all rows on rollback.
            conn.execute("DROP TRIGGER shipments_no_delete")

            conn.executemany(
                "INSERT INTO retained_shipments(month, sku, occurred_at_us, boxes)"
                " VALUES(?,?,?,?)",
                [
                    (month, row["sku"], row["occurred_at_us"], row["boxes"])
                    for row in shipments
                ],
            )
            conn.executemany(
                "INSERT INTO retained_sequences(month, sequence_no) VALUES(?,?)",
                [(month, row["sequence_no"]) for row in shipments],
            )
            conn.execute("DELETE FROM shipments WHERE month = ?", (month,))
            conn.execute(
                "INSERT INTO purged_months(month, purged_at, retained_row_count,"
                " retained_total_boxes) VALUES(?,?,?,?)",
                (month, purged_at, row_count, total_boxes),
            )
        except sqlite3.DatabaseError as exc:
            raise ValidationError(f"清理写入失败，已整体回滚，原月结保持不变：{exc}") from exc
        finally:
            # A single CREATE TRIGGER statement via execute() stays inside the
            # current transaction (executescript() would implicitly COMMIT).
            conn.execute(SHIPMENTS_DELETE_TRIGGER)

        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (
                purged_at,
                "purge_month",
                month,
                None,
                f"清理 {month} 逐笔流水：保留 {row_count} 条精简事实 / {total_boxes} 箱"
                " 及查重流水号",
            ),
        )

    return {
        "month": month,
        "purged_at": purged_at,
        "retained_row_count": row_count,
        "retained_total_boxes": total_boxes,
        "already_purged": False,
    }
