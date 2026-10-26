import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apify_x_probe import normalize, classify_post, build_report, setup_db, add_to_staging, unique_handles


class TestApifyXProbe(unittest.TestCase):
    def setUp(self):
        self.row = {
            "type": "tweet", "id": "1234567890123456789", "authorHandle": "yoneyamai",
            "createdAt": datetime.now(timezone.utc).isoformat(), "text": "新作発表",
            "media": [{"type": "photo"}], "isQuote": False
        }

    def test_original_and_url(self):
        doc, reason = normalize(self.row, {"yoneyamai"}, datetime.now(timezone.utc) - timedelta(days=30), False)
        self.assertEqual(reason, "accepted")
        self.assertEqual(doc["url"], "https://x.com/yoneyamai/status/1234567890123456789")
        self.assertEqual(doc["text_preview"], "")
        self.assertEqual(doc["media_count"], 1)

    def test_retweets_rejected(self):
        self.row["retweetedTweet"] = {"id": "42"}
        self.assertEqual(classify_post(self.row), "repost")
        doc, reason = normalize(self.row, {"yoneyamai"}, datetime.now(timezone.utc) - timedelta(days=30), False)
        self.assertIsNone(doc)
        self.assertEqual(reason, "repost")

    def test_quote_kept(self):
        self.row["isQuote"] = True
        self.assertEqual(classify_post(self.row), "quote")

    def test_wrong_author_rejected(self):
        doc, reason = normalize(self.row, {"ui_shig"}, datetime.now(timezone.utc) - timedelta(days=30), False)
        self.assertIsNone(doc)
        self.assertEqual(reason, "author_mismatch")

    def test_idempotency(self):
        report, rows = build_report([self.row, self.row], ["yoneyamai"], 30, False)
        self.assertEqual(report["accepted_unique_posts"], 1)
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(str(Path(directory) / "test.sqlite"))) as conn, conn:
                setup_db(conn)
                self.assertEqual(add_to_staging(conn, rows), (1, 0))
                self.assertEqual(add_to_staging(conn, rows), (0, 1))

    def test_handle_normalization(self):
        self.assertEqual(unique_handles(["@MikaPikaZo", "mikapikazo"]), ["MikaPikaZo"])


if __name__ == '__main__':
    unittest.main()
