import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
import subprocess


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import x_bookmark_digest as digest  # noqa: E402


TEST_CATEGORY = next(
    str(item["name"]) for item in digest.load_config().get("categories", [])
    if isinstance(item, dict) and item.get("name")
)

def row(tweet_id, source):
    return digest.normalize_row(
        {"id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}", "text": "test"}, source
    )


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.conn = digest.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_sources_are_canonical_and_deduplicated(self):
        self.assertEqual(row("1", "bookmarks")["sources"], ["bookmark"])
        digest.ingest(self.conn, [row("1", "bookmark"), row("1", "like")])
        stored = self.conn.execute("SELECT sources_json FROM items WHERE id='1'").fetchone()[0]
        self.assertEqual(json.loads(stored), ["bookmark", "like"])

    def test_pending_claim_prevents_duplicate_reminder(self):
        digest.ingest(self.conn, [row("1", "bookmark")])
        first = digest.pending(self.conn, 24, False)
        second = digest.pending(self.conn, 24, False)
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 0)
        forced = digest.pending(self.conn, 24, False, force=True)
        self.assertEqual(forced["count"], 1)

    def test_previous_day_window_excludes_other_calendar_days(self):
        digest.ingest(self.conn, [row("1", "bookmark"), row("2", "like")])
        start, end, target = digest.previous_local_day_window()
        self.conn.execute(
            "UPDATE items SET first_seen_at=?, last_digest_at=NULL WHERE id='1'",
            (start.replace("00:00:00Z", "01:00:00Z"),),
        )
        self.conn.execute(
            "UPDATE items SET first_seen_at=?, last_digest_at=NULL WHERE id='2'",
            (end,),
        )
        self.conn.execute("UPDATE items SET last_digest_at=? WHERE id='2'", (digest.now_iso(),))
        self.conn.commit()
        payload = digest.pending(self.conn, 24, False, claim=False, window="previous_day")
        self.assertEqual(payload["target_date"], target)
        self.assertEqual([item["id"] for item in payload["items"]], ["1"])

    def test_previous_day_catches_unclaimed_older_unread_items(self):
        digest.ingest(self.conn, [row("1", "like")])
        self.conn.execute(
            "UPDATE items SET first_seen_at='2020-01-01T00:00:00Z', last_digest_at=NULL WHERE id='1'"
        )
        self.conn.commit()
        payload = digest.pending(self.conn, 24, False, claim=False, window="previous_day")
        self.assertEqual([item["id"] for item in payload["items"]], ["1"])

    def test_establish_baseline_only_initializes_empty_store(self):
        self.assertTrue(digest.establish_baseline(self.conn))
        digest.ingest(self.conn, [row("1", "bookmark")], bootstrap=False)
        self.assertEqual(self.conn.execute("SELECT status FROM items WHERE id='1'").fetchone()[0], "unread")
        self.assertFalse(digest.establish_baseline(self.conn))

    def test_ingest_tracks_new_source_membership_on_existing_tweet(self):
        digest.ingest(self.conn, [row("1", "like")])
        self.conn.execute("UPDATE items SET status='read', last_digest_at=? WHERE id='1'", (digest.now_iso(),))
        self.conn.commit()
        digest.ingest(self.conn, [row("1", "bookmark")])
        state = self.conn.execute(
            "SELECT status, sources_json, latest_source_observed_at, last_digest_at FROM items WHERE id='1'"
        ).fetchone()
        self.assertEqual(state[0], "unread")
        self.assertEqual(json.loads(state[1]), ["bookmark", "like"])
        self.assertIsNotNone(state[2])
        self.assertIsNone(state[3])

    def test_remove_calls_unbookmark_and_unlike_for_both_sources(self):
        digest.ingest(self.conn, [row("1", "bookmark"), row("1", "like")])
        calls = []

        def fake_run(args, check=True):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        args = Namespace(id="1", source="both", confirm="REMOVE", db=str(self.db))
        with patch.object(digest, "run_cli", side_effect=fake_run):
            digest.cmd_remove(args)
        self.assertEqual([call[1] for call in calls], ["unbookmark", "unlike"])
        row_state = self.conn.execute("SELECT status, sources_json FROM items WHERE id='1'").fetchone()
        self.assertEqual(row_state[0], "removed")
        self.assertEqual(json.loads(row_state[1]), [])

    def test_render_rejects_low_confidence_without_review_category(self):
        annotations = {
            "items": [{
                "id": "1", "title": "原贴标题", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.4, "summary": "摘要", "key_points": [],
                "recommended_action": "阅读", "priority": 2,
            }]
        }
        with self.assertRaises(digest.DigestError):
            digest.render(annotations)

    def test_render_keeps_original_title_and_all_media(self):
        annotations = {
            "items": [{
                "id": "1", "title": "原贴第一行", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.9, "summary": "摘要", "key_points": ["一", "二", "三", "四", "五"],
                "recommended_action": "阅读", "priority": 2,
                "url": "https://x.com/i/status/1", "author": "author", "sources": ["like"],
                "media_urls": ["https://pbs.twimg.com/media/abc.jpg", "https://video.twimg.com/a.mp4"],
            }]
        }
        output = digest.render(annotations)
        self.assertIn("### 1. 原贴第一行", output)
        self.assertIn("原贴包含配图", output)
        self.assertIn("打开原贴图片", output)
        self.assertNotIn("![原贴配图", output)
        self.assertIn("播放/打开媒体", output)
        self.assertIn("要点：", output)
        self.assertNotIn("合计≤200字", output)

    def test_render_can_enable_inline_media_previews_explicitly(self):
        annotations = {
            "items": [{
                "id": "1", "title": "原贴第一行", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.9, "summary": "摘要", "key_points": ["要点"],
                "recommended_action": "阅读", "priority": 2,
                "url": "https://x.com/i/status/1", "author": "author", "sources": ["like"],
                "media_urls": ["https://pbs.twimg.com/media/abc.jpg"],
            }]
        }
        with patch.object(digest, "defaults", return_value={"inline_media_previews": True}):
            output = digest.render(annotations)
        self.assertIn("![原贴配图 1](https://pbs.twimg.com/media/abc.jpg?format=jpg&name=large)", output)

    def test_render_card_has_queue_progress_and_four_actions(self):
        annotations = {
            "items": [{
                "id": "1", "title": "第一条原贴", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.9, "summary": "摘要", "key_points": ["要点一"],
                "recommended_action": "阅读", "priority": 2,
                "url": "https://x.com/i/status/1", "author": "author", "sources": ["like"],
                "media_urls": [],
            }, {
                "id": "2", "title": "第二条原贴", "category": "待复核", "secondary_category": None,
                "confidence": 0.5, "summary": "摘要", "key_points": ["需要打开原帖确认"],
                "recommended_action": "打开原帖", "priority": 3,
                "url": "https://x.com/i/status/2", "author": "author", "sources": ["bookmark"],
                "media_urls": [],
            }]
        }
        output = digest.render_card(annotations, 1, 0)
        self.assertIn("> `━━━━ ● ● ○ ○`  `▶`", output)
        self.assertIn("> **Step 1 of 2**", output)
        self.assertIn("第 1/2 条", output)
        self.assertIn("本条完成后剩余 1 条", output)
        self.assertIn("1. **read**，移出喜欢", output)
        self.assertIn("2. **keep**", output)
        self.assertIn("3. **纳入 Obsidian 知识库**", output)
        self.assertIn("4. **调整分类**", output)
        self.assertNotIn("没有配图", output)

    def test_render_rejects_key_points_over_200_characters(self):
        annotations = {
            "items": [{
                "id": "1", "title": "原贴标题", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.9, "summary": "摘要", "key_points": ["字" * 201],
                "recommended_action": "阅读", "priority": 2,
            }]
        }
        with self.assertRaises(digest.DigestError):
            digest.render(annotations)

    def test_render_card_session_advances_only_after_receipt(self):
        annotations = {
            "items": [{
                "id": "1", "title": "第一条原贴", "category": TEST_CATEGORY, "secondary_category": None,
                "confidence": 0.9, "summary": "摘要", "key_points": ["要点一"],
                "recommended_action": "阅读", "priority": 2,
                "url": "https://x.com/i/status/1", "author": "author", "sources": ["like"],
                "media_urls": [],
            }, {
                "id": "2", "title": "第二条原贴", "category": "待复核", "secondary_category": None,
                "confidence": 0.5, "summary": "摘要", "key_points": ["需要打开原帖确认"],
                "recommended_action": "打开原帖", "priority": 3,
                "url": "https://x.com/i/status/2", "author": "author", "sources": ["bookmark"],
                "media_urls": [],
            }]
        }
        session = {"item_ids": ["1", "2"], "completed": []}
        first = digest.render_card(annotations, session=session)
        self.assertIn("第 1/2 条", first)
        session["completed"].append({"id": "1", "action": "keep", "result": "已标记 keep"})
        second = digest.render_card(annotations, session=session)
        self.assertIn("第 2/2 条", second)
        self.assertIn("已处理 1 条", second)

    def test_review_record_requires_queue_order(self):
        session_path = Path(self.temp.name) / "review-session.json"
        session_path.write_text(json.dumps({"item_ids": ["1", "2"], "completed": []}), encoding="utf-8")
        with self.assertRaises(digest.DigestError):
            digest.cmd_review_record(Namespace(session=str(session_path), id="2", action="keep", result="已标记 keep"))
        digest.cmd_review_record(Namespace(session=str(session_path), id="1", action="keep", result="已标记 keep"))
        session = json.loads(session_path.read_text())
        self.assertEqual(len(session["completed"]), 1)
        self.assertEqual(session["completed"][0]["id"], "1")

    def annotation_queue(self):
        return {"generated_at": "2026-09-30T00:00:00Z", "since": "2026-09-29T00:00:00Z", "items": [
            {"id": "1", "title": "第一条", "category": TEST_CATEGORY, "secondary_category": None,
             "confidence": 0.9, "summary": "摘要", "key_points": ["一个要点"],
             "recommended_action": "阅读", "priority": 2, "url": "https://x.com/i/status/1",
             "author": "author", "sources": ["like"], "media_urls": [], "text": "正文"},
            {"id": "2", "title": "第二条", "category": TEST_CATEGORY, "secondary_category": None,
             "confidence": 0.9, "summary": "摘要", "key_points": ["另一个要点"],
             "recommended_action": "阅读", "priority": 2, "url": "https://x.com/i/status/2",
             "author": "author", "sources": ["bookmark"], "media_urls": [], "text": "正文"},
            {"id": "3", "title": "第三条", "category": TEST_CATEGORY, "secondary_category": None,
             "confidence": 0.9, "summary": "摘要", "key_points": ["第三个要点"],
             "recommended_action": "阅读", "priority": 2, "url": "https://x.com/i/status/3",
             "author": "author", "sources": ["like"], "media_urls": [], "text": "正文"},
        ]}

    def write_annotation_session(self):
        annotations_path = Path(self.temp.name) / "annotations.json"
        annotations = self.annotation_queue()
        annotations_path.write_text(json.dumps(annotations, ensure_ascii=False), encoding="utf-8")
        session_path = Path(self.temp.name) / "session.json"
        digest.cmd_review_start(Namespace(annotations=str(annotations_path), session=str(session_path), replace=False, claim=False, db=str(self.db)))
        return annotations_path, session_path, annotations

    def test_review_apply_keep_updates_state_and_receipt(self):
        digest.ingest(self.conn, [row("1", "like"), row("2", "bookmark"), row("3", "like")])
        annotations_path, session_path, _ = self.write_annotation_session()
        digest.cmd_review_apply(Namespace(
            annotations=str(annotations_path), session=str(session_path), id="1", action="keep",
            category=None, confirm=None, result=None, operation_id=None, db=str(self.db),
        ))
        state = self.conn.execute("SELECT status, last_digest_at FROM items WHERE id='1'").fetchone()
        self.assertEqual(state[0], "keep")
        self.assertIsNotNone(state[1])
        session = json.loads(session_path.read_text())
        self.assertEqual(session["completed"][0]["id"], "1")

    def test_review_apply_is_idempotent(self):
        digest.ingest(self.conn, [row("1", "like"), row("2", "bookmark"), row("3", "like")])
        annotations_path, session_path, _ = self.write_annotation_session()
        args = Namespace(annotations=str(annotations_path), session=str(session_path), id="1", action="keep",
                         category=None, confirm=None, result=None, operation_id="op-1", db=str(self.db))
        digest.cmd_review_apply(args)
        digest.cmd_review_apply(args)
        session = json.loads(session_path.read_text())
        self.assertEqual(len(session["completed"]), 1)

    def test_review_start_refuses_overwrite_and_batch_render(self):
        annotations_path, session_path, annotations = self.write_annotation_session()
        with self.assertRaises(digest.DigestError):
            digest.cmd_review_start(Namespace(annotations=str(annotations_path), session=str(session_path), replace=False, claim=False, db=str(self.db)))
        output = digest.render_cards(annotations, 1, 3, 0)
        self.assertEqual(output.count("请选择"), 3)

    def test_parse_batch_reply_maps_ordered_lines_and_category_forms(self):
        session = {"item_ids": ["1", "2", "3"], "completed": []}
        parsed = digest.parse_batch_reply(f"2\n3\n4｜{TEST_CATEGORY}", session, 3)
        self.assertEqual(parsed["decisions"], [
            {"id": "1", "action": "keep"},
            {"id": "2", "action": "obsidian"},
            {"id": "3", "action": "category", "category": TEST_CATEGORY},
        ])
        parsed = digest.parse_batch_reply(f"4 {TEST_CATEGORY}", session, 3)
        self.assertEqual(parsed["decisions"][0]["category"], TEST_CATEGORY)

    def test_parse_batch_reply_rejects_invalid_or_excess_lines(self):
        session = {"item_ids": ["1", "2"], "completed": []}
        with self.assertRaises(digest.DigestError):
            digest.parse_batch_reply("9", session, 3)
        with self.assertRaises(digest.DigestError):
            digest.parse_batch_reply("2\n2\n2", session, 3)

    def test_parse_reply_command_is_registered(self):
        parser = digest.build_parser()
        parsed = parser.parse_args([
            "parse-reply", "--session", "session.json", "--reply", "reply.txt", "--count", "3"
        ])
        self.assertIs(parsed.func, digest.cmd_parse_reply)

    def test_pending_cli_defaults_to_previous_day_window(self):
        parsed = digest.build_parser().parse_args(["pending"])
        self.assertEqual(parsed.window, "previous_day")

    def test_example_config_has_no_personal_categories(self):
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        names = {item["name"] for item in example["categories"]}
        self.assertEqual(names, {"工具与产品", "研究与学习", "行业与市场", "工作与成长"})

    def test_review_apply_obsidian_uses_pending_import_fallback(self):
        digest.ingest(self.conn, [row("1", "like"), row("2", "bookmark"), row("3", "like")])
        annotations_path, session_path, _ = self.write_annotation_session()
        pending_dir = Path(self.temp.name) / "pending"
        with patch.object(digest, "defaults", return_value={"database": str(self.db), "draft_dir": str(pending_dir)}):
            digest.cmd_review_apply(Namespace(
                annotations=str(annotations_path), session=str(session_path), id="1", action="obsidian",
                category=None, confirm=None, result=None, operation_id=None, db=str(self.db),
            ))
        self.assertEqual(self.conn.execute("SELECT status FROM items WHERE id='1'").fetchone()[0], "read")
        self.assertTrue((pending_dir / "obsidian-pending" / "X Digest" / "1.md").exists())

    def test_review_apply_read_requires_confirmation_and_records_sources(self):
        digest.ingest(self.conn, [row("1", "bookmark"), row("1", "like"), row("2", "bookmark"), row("3", "like")])
        annotations_path, session_path, _ = self.write_annotation_session()
        args = Namespace(annotations=str(annotations_path), session=str(session_path), id="1", action="read",
                         category=None, confirm="wrong", result=None, operation_id=None, db=str(self.db))
        with self.assertRaises(digest.DigestError):
            digest.cmd_review_apply(args)
        calls = []
        def fake_run(command, check=True):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        args.confirm = "READ:1"
        with patch.object(digest, "run_cli", side_effect=fake_run):
            digest.cmd_review_apply(args)
        self.assertEqual(json.loads(self.conn.execute("SELECT sources_json FROM items WHERE id='1'").fetchone()[0]), [])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
