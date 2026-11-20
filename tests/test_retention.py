"""月结明细过保留期清理（purge）行为测试。"""

from __future__ import annotations

import io
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coldchain import facts, knowledge, retention
from coldchain.app import create_app
from coldchain.db import connect, init_db
from coldchain.errors import ValidationError


def csv_bytes(rows, header="流水号,SKU,出库时刻,箱数"):
    lines = [header]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


# 两个 SKU、同 SKU 同时刻多笔、不同温区命中点，便于验证计数口径。
MARCH_ROWS = [
    ("OUT-001", "SKU-A", "2026-03-01T01:00:00Z", 12),
    ("OUT-002", "SKU-A", "2026-03-01T01:00:00Z", 3),   # 与 OUT-001 同 SKU 同时刻
    ("OUT-003", "SKU-B", "2026-03-05T23:30:00Z", 7),
    ("OUT-004", "SKU-C", "2026-03-31T23:59:59Z", 2),
]


class PurgeAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "purge.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()
        self.upload(MARCH_ROWS)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, rows, filename="m.csv", month=None):
        data = {"file": (io.BytesIO(csv_bytes(rows)), filename)}
        if month:
            data["month"] = month
        return self.client.post(
            "/api/months", data=data, content_type="multipart/form-data"
        )

    def register(self, sku, intervals, reason="c", base=None):
        return self.client.post(
            "/api/versions",
            json={"sku": sku, "intervals": intervals, "reason": reason,
                  "base_version": base},
        )

    def purge(self, month="2026-03", confirm=True):
        return self.client.post(
            f"/api/months/{month}/purge", json={"confirm": confirm}
        )

    def report(self, month="2026-03", version=None):
        query = f"/api/report?month={month}"
        if version is not None:
            query += f"&version={version}"
        return self.client.get(query).get_json()


class PurgeFlowTests(PurgeAppTestCase):
    def test_purge_removes_ledger_rows_but_keeps_compact_and_sequence_data(self):
        before = self.report()
        self.assertFalse(before["details_purged"])
        response = self.purge()
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["month"], "2026-03")
        self.assertEqual(body["retained_row_count"], 4)
        self.assertEqual(body["retained_total_boxes"], 24)
        self.assertFalse(body["already_purged"])

        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"], 0
            )
            retained = conn.execute(
                "SELECT sku, occurred_at_us, boxes FROM retained_shipments"
            ).fetchall()
            self.assertEqual(len(retained), 4)
            # 精简事实不含流水号与来源行等逐笔展示字段。
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM retained_sequences"
                ).fetchone()["c"],
                4,
            )
            seqs = {
                row["sequence_no"]
                for row in conn.execute("SELECT sequence_no FROM retained_sequences")
            }
            self.assertEqual(seqs, {"OUT-001", "OUT-002", "OUT-003", "OUT-004"})
            purged = conn.execute("SELECT * FROM purged_months").fetchall()
            self.assertEqual(len(purged), 1)
            self.assertEqual(purged[0]["month"], "2026-03")
        finally:
            conn.close()

        after = self.report()
        self.assertTrue(after["details_purged"])
        self.assertIsNotNone(after["purged_at"])
        # 汇总总量保持不变。
        self.assertEqual(after["total_boxes"], before["total_boxes"])
        self.assertEqual(after["total_shipments"], before["total_shipments"])
        # 逐笔明细不再带流水号/时刻文本。
        for bucket in after["zones"]:
            for item in bucket["shipments"]:
                self.assertNotIn("sequence_no", item)
                self.assertNotIn("occurred_at", item)
        for item in after["unclassified"]["shipments"]:
            self.assertNotIn("sequence_no", item)

    def test_months_list_marks_purged_month(self):
        months = self.client.get("/api/months").get_json()["months"]
        self.assertFalse(months[0]["details_purged"])
        self.assertEqual(self.purge().status_code, 201)
        months = self.client.get("/api/months").get_json()["months"]
        self.assertTrue(months[0]["details_purged"])
        self.assertIsNotNone(months[0]["details_purged_at"])

    def test_report_still_classifies_under_any_historical_or_new_version(self):
        # v1: SKU-A 全月冷藏 -> 15 箱（12+3，两笔分别计数），其余未归类。
        self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
            reason="v1", base=None,
        )
        # v2: SKU-A 拆成冷冻/冷藏，SKU-B 冷冻。
        self.register(
            "SKU-A",
            [
                {"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z",
                 "valid_to": "2026-03-02T00:00:00Z"},
                {"zone": "冷藏", "valid_from": "2026-03-02T00:00:00Z", "valid_to": ""},
            ],
            reason="v2-a", base=1,
        )
        self.register(
            "SKU-B",
            [{"zone": "冷冻", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
            reason="v2-b", base=2,
        )

        before_v1 = self.report(version=1)
        before_v3 = self.report(version=3)
        self.assertEqual(
            {z["zone"]: (z["boxes"], z["shipment_count"]) for z in before_v1["zones"]},
            {"冷藏": (15, 2)},
        )

        self.assertEqual(self.purge().status_code, 201)

        # 清理后按历史版号 v1 与最新版 v3 重算，统计结果与清理前完全一致。
        def counts(payload):
            return {
                "zones": {
                    z["zone"]: (z["boxes"], z["shipment_count"]) for z in payload["zones"]
                },
                "unclassified": (
                    payload["unclassified"]["boxes"],
                    payload["unclassified"]["shipment_count"],
                ),
                "total_boxes": payload["total_boxes"],
                "total_shipments": payload["total_shipments"],
                "classified_boxes": payload["classified_boxes"],
            }

        after_v1 = self.report(version=1)
        after_v3 = self.report(version=3)
        self.assertEqual(counts(before_v1), counts(after_v1))
        self.assertEqual(counts(before_v3), counts(after_v3))
        self.assertTrue(after_v1["details_purged"])

        # 清理之后再新增知识版号（v4：SKU-C 冷藏），仍能用保留的精简事实重算。
        self.register(
            "SKU-C",
            [{"zone": "冷藏", "valid_from": "2026-03-31T00:00:00Z", "valid_to": ""}],
            reason="v4-after-purge", base=3,
        )
        after_v4 = self.report(version=4)
        zone_map = {z["zone"]: (z["boxes"], z["shipment_count"]) for z in after_v4["zones"]}
        # SKU-A 在 03-01 落入冷冻（12+3=15，两笔分别计数），SKU-B 冷冻 7，
        # SKU-C 冷藏 2；保留的精简事实仍支撑新版重算。
        self.assertEqual(zone_map["冷冻"], (22, 3))
        self.assertEqual(zone_map["冷藏"], (2, 1))
        self.assertEqual(after_v4["unclassified"]["boxes"], 0)
        # 旧版号口径不被新版影响。
        self.assertEqual(
            {z["zone"]: z["boxes"] for z in self.report(version=1)["zones"]},
            {"冷藏": 15},
        )

    def test_same_sku_same_instant_multiple_shipments_count_separately(self):
        self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
            reason="v1", base=None,
        )
        self.purge()
        report = self.report()
        cold = next(z for z in report["zones"] if z["zone"] == "冷藏")
        self.assertEqual(cold["boxes"], 15)
        self.assertEqual(cold["shipment_count"], 2)  # 两笔分别计数，不合并

    def test_reupload_of_purged_month_still_rejected(self):
        self.assertEqual(self.purge().status_code, 201)
        response = self.upload(MARCH_ROWS)
        self.assertEqual(response.status_code, 409)
        # 用不同文件重传该月同样被拒绝。
        response = self.upload(
            [("OUT-900", "SKU-Z", "2026-03-02T00:00:00Z", 1)]
        )
        self.assertEqual(response.status_code, 409)
        months = self.client.get("/api/months").get_json()["months"]
        self.assertEqual(len(months), 1)

    def test_other_months_cannot_reuse_purged_sequence_numbers(self):
        self.assertEqual(self.purge().status_code, 201)
        # 其他月份复用已清理月份的流水号 -> 409。
        response = self.upload(
            [("OUT-001", "SKU-Z", "2026-04-01T00:00:00Z", 1)]
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("OUT-001", response.get_json()["message"])
        # 全新流水号的其他月份可以正常上传。
        ok = self.upload([("OUT-100", "SKU-Z", "2026-04-01T00:00:00Z", 1)])
        self.assertEqual(ok.status_code, 201)
        # 新月份自身的查重仍生效（其流水在 shipments 表）。
        clash = self.upload([("OUT-100", "SKU-Q", "2026-05-01T00:00:00Z", 1)])
        self.assertEqual(clash.status_code, 409)

    def test_repeated_confirmation_is_idempotent(self):
        first = self.purge()
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.get_json()["already_purged"])
        second = self.purge()
        self.assertEqual(second.status_code, 200)
        body = second.get_json()
        self.assertTrue(body["already_purged"])
        self.assertEqual(body["retained_row_count"], 4)
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM purged_months").fetchone()["c"], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM retained_shipments"
                ).fetchone()["c"],
                4,
            )
            audits = conn.execute(
                "SELECT action FROM audit_log WHERE action = 'purge_month'"
            ).fetchall()
            self.assertEqual(len(audits), 1)  # 幂等确认不重复写审计
        finally:
            conn.close()

    def test_purge_requires_explicit_confirmation(self):
        for payload in ({}, {"confirm": False}, {"confirm": "false"}):
            response = self.client.post("/api/months/2026-03/purge", json=payload)
            self.assertEqual(response.status_code, 400, payload)
        conn = connect(self.db_path)
        try:
            # 未确认时原月结完整保留。
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"], 4
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM purged_months").fetchone()["c"], 0
            )
        finally:
            conn.close()

    def test_purge_unknown_month_404_and_bad_month_400(self):
        self.assertEqual(
            self.client.post("/api/months/2026-02/purge", json={"confirm": True}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/months/not-a-month/purge", json={"confirm": True}).status_code,
            400,
        )

    def test_retained_and_purged_tables_are_immutable(self):
        self.purge()
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE retained_shipments SET boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM retained_shipments")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE retained_sequences SET sequence_no = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM retained_sequences")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE purged_months SET purged_at = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM purged_months")
            # 普通 shipments 的更新/删除保护依旧存在（删除触发器已重装）。
            trigger = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND name = 'shipments_no_delete'"
            ).fetchone()
            self.assertIsNotNone(trigger)
            # 空表 DELETE 不触发行级触发器；插入另一月份后删除必须仍被拦截。
            conn.execute(
                "INSERT INTO months(month, source_filename, source_sha256, row_count,"
                " total_boxes, frozen_at) VALUES('2026-04','a','h',1,1,'2026-04-01T00:00:00.000000Z')"
            )
            conn.execute(
                "INSERT INTO shipments(month, sequence_no, sku, occurred_at,"
                " occurred_at_us, boxes, source_row)"
                " VALUES('2026-04','OUT-X','SKU-Z','2026-04-01T00:00:00.000000Z',0,1,2)"
            )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM shipments")
        finally:
            conn.close()

    def test_failed_purge_rolls_back_and_keeps_original_month(self):
        conn = connect(self.db_path)
        try:
            # 让 retained_shipments 写入必然失败。
            conn.execute(
                "CREATE TRIGGER boom_purge BEFORE INSERT ON retained_shipments"
                " BEGIN SELECT RAISE(ABORT, 'injected purge failure'); END"
            )
        finally:
            conn.close()
        with self.assertRaises(ValidationError):
            retention.purge_month(connect(self.db_path), "2026-03", confirmed=True)

        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"], 4
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM purged_months").fetchone()["c"], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM retained_shipments"
                ).fetchone()["c"],
                0,
            )
            # 删除触发器必须随回滚/重装依旧生效。
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM shipments")
            conn.execute("DROP TRIGGER boom_purge")
        finally:
            conn.close()
        # 失败后原月报仍可读，且可以重新成功清理。
        report = self.report()
        self.assertFalse(report["details_purged"])
        self.assertEqual(report["total_boxes"], 24)
        self.assertEqual(self.purge().status_code, 201)

    def test_restart_keeps_summary_and_blocks_old_sequences(self):
        self.register(
            "SKU-A",
            [{"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z", "valid_to": ""}],
            reason="v1", base=None,
        )
        before = self.report(version=1)
        self.purge()

        restarted = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = restarted.test_client()
        after = client.get("/api/report?month=2026-03&version=1").get_json()
        self.assertTrue(after["details_purged"])
        self.assertEqual(
            {z["zone"]: z["boxes"] for z in after["zones"]},
            {z["zone"]: z["boxes"] for z in before["zones"]},
        )
        self.assertEqual(after["total_shipments"], 4)
        # 重启后旧流水号仍被拦截。
        response = client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv_bytes(
                [("OUT-002", "SKU-Z", "2026-04-01T00:00:00Z", 1)]
            )), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)
        # 重传已清理月份同样被拦截。
        response = client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv_bytes(MARCH_ROWS)), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)


class PurgeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "concurrent-purge.sqlite3")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        response = client.post(
            "/api/months",
            data={"file": (io.BytesIO(csv_bytes(MARCH_ROWS)), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)

    def test_parallel_purges_serialize_to_one_result(self):
        self._seed()
        outcomes = []
        barrier = threading.Barrier(5)

        def worker():
            conn = connect(self.db_path)
            barrier.wait()
            try:
                outcomes.append(retention.purge_month(conn, "2026-03", True))
            except Exception as exc:  # pragma: no cover - 失败也记录
                outcomes.append(("error", str(exc)))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 5)
        self.assertEqual(sum(1 for r in outcomes if not r["already_purged"]), 1)
        self.assertEqual(sum(1 for r in outcomes if r["already_purged"]), 4)
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM retained_shipments"
                ).fetchone()["c"],
                4,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM purged_months").fetchone()["c"], 1
            )
        finally:
            conn.close()

    def test_purge_interleaved_with_upload_and_report_never_shows_half_state(self):
        self._seed()
        report_conn = connect(self.db_path)
        purge_conn = connect(self.db_path)
        upload_conn = connect(self.db_path)
        try:
            # 读事务在清理提交前打开：必须看到完整的清理前状态。
            with knowledge.transaction(report_conn, immediate=False):
                month_row = report_conn.execute(
                    "SELECT month FROM months WHERE month = '2026-03'"
                ).fetchone()
                self.assertIsNotNone(month_row)
                retention.purge_month(purge_conn, "2026-03", True)

                # 同一快照内：旧 shipments 仍在，新表不可见。
                self.assertEqual(
                    report_conn.execute(
                        "SELECT COUNT(*) AS c FROM shipments WHERE month = '2026-03'"
                    ).fetchone()["c"],
                    4,
                )
                self.assertEqual(
                    report_conn.execute(
                        "SELECT COUNT(*) AS c FROM purged_months"
                    ).fetchone()["c"],
                    0,
                )

            # 清理提交后：上传新月份时旧流水号立即被拦。
            with self.assertRaises(Exception):
                facts.ingest_month(
                    upload_conn,
                    csv_bytes([("OUT-001", "SKU-Z", "2026-04-01T00:00:00Z", 1)]),
                    filename="apr.csv",
                )
            # 清理后的新连接读到完整的清理后状态。
            fresh = connect(self.db_path)
            try:
                self.assertEqual(
                    fresh.execute(
                        "SELECT COUNT(*) AS c FROM shipments"
                    ).fetchone()["c"],
                    0,
                )
                self.assertTrue(retention.is_purged(fresh, "2026-03"))
            finally:
                fresh.close()
        finally:
            report_conn.close()
            purge_conn.close()
            upload_conn.close()


class PurgeWebTests(PurgeAppTestCase):
    def test_web_purge_flow_and_report_page_markers(self):
        # 未确认（不带勾选）-> 400。
        response = self.client.post(
            "/report/purge", data={"month": "2026-03"}
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            "/report/purge",
            data={"month": "2026-03", "confirm": "true"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/report?month=2026-03", response.headers["Location"])

        page = self.client.get("/report?month=2026-03").get_data(as_text=True)
        self.assertIn("明细已清理", page)
        self.assertIn("重复确认", page)
        # 清理后页面不再渲染流水号逐笔表格内容。
        self.assertNotIn("OUT-001", page)

        # 幂等的页面再次确认。
        response = self.client.post(
            "/report/purge",
            data={"month": "2026-03", "confirm": "true"},
        )
        self.assertEqual(response.status_code, 302)

        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("明细已清理", index)


if __name__ == "__main__":
    unittest.main()
