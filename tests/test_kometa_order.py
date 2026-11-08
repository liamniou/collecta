import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

_data = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(_data.cleanup)
os.environ["DATA_DIR"] = _data.name
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import config, main, models
from app.models import APPROVED, AWAITING_APPROVAL, Job, Match, Schedule, get_session
from app.publishers import kometa


class CollectionOrderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.yml"
        self.config_path.write_text("libraries:\n  Films:\n    collection_files:\n")
        self.addCleanup(patch.stopall)
        patch.object(config, "KOMETA_CONFIG_DIR", self.directory.name).start()
        patch.object(config, "KOMETA_LIBRARY", "Films").start()

    def collection(self):
        return yaml.safe_load((self.root / "collecta-test-list.yml").read_text())["collections"]["Test list"]

    def test_default_order_for_both_builders(self):
        for tmdb_list_id in ("", "123"):
            with self.subTest(tmdb_list_id=tmdb_list_id):
                kometa.create_collection("Test list", [{"tmdb_id": 1}], tmdb_list_id=tmdb_list_id)
                collection = self.collection()
                self.assertEqual(collection["collection_order"], "year.desc")
                self.assertEqual(collection["sync_mode"], "sync")
                self.assertTrue(collection["visible_home"])
                self.assertTrue(collection["visible_shared"])
                self.assertTrue(collection["visible_library"])
                self.assertEqual(collection["tmdb_list" if tmdb_list_id else "tmdb_movie"],
                                 123 if tmdb_list_id else 1)
        self.assertEqual(self.config_path.read_text().count("- file:"), 1)

    def test_all_options_and_rewriting_existing_order(self):
        for order in config.KOMETA_ORDER_OPTIONS:
            with self.subTest(order=order):
                detail = kometa.create_collection(
                    "Test list", [{"tmdb_id": 1}], tmdb_list_id="123", collection_order=order)
                self.assertEqual(self.collection()["collection_order"], order)
                self.assertIn(config.KOMETA_ORDER_OPTIONS[order], detail)
        kometa.create_collection("Test list", [{"tmdb_id": 1}])
        self.assertEqual(self.collection()["collection_order"], "year.desc")

    def test_direct_builder_supports_explicit_sort_options(self):
        for order in ("year.asc", "title.asc", "title.desc"):
            with self.subTest(order=order):
                kometa.create_collection("Test list", [{"tmdb_id": 1}], collection_order=order)
                self.assertEqual(self.collection()["collection_order"], order)

    def test_invalid_orders_do_not_write_files(self):
        for order in ("random", "", "year.desc\n    visible_home: false", "custom"):
            with self.subTest(order=order):
                with self.assertRaises(ValueError):
                    kometa.create_collection("Test list", [{"tmdb_id": 1}], collection_order=order)
                self.assertFalse((self.root / "collecta-test-list.yml").exists())


class OrderAPITests(unittest.TestCase):
    def setUp(self):
        models.Base.metadata.drop_all(models.engine)
        models.init_db()
        self.client = main.app.test_client()
        with get_session() as session:
            session.add(Job(id="test", source_url="https://example.test/list",
                            list_name="Test list", status=AWAITING_APPROVAL))
            session.add(Match(job_id="test", tmdb_id=1, title="Movie", year=2000, state=APPROVED))

    def test_default_and_saved_order_survive_sessions(self):
        self.assertEqual(self.client.get("/api/jobs/test").json["kometa_order"], "year.desc")
        for order in ("year.asc", "title.asc", "title.desc"):
            response = self.client.post("/api/jobs/test/kometa-order", json={"kometa_order": order})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {"kometa_order": order})
            self.assertEqual(self.client.get("/api/jobs/test").json["kometa_order"], order)
        models.init_db()
        self.assertEqual(self.client.get("/api/jobs/test").json["kometa_order"], "title.desc")

    def test_invalid_inputs_leave_order_unchanged(self):
        for data in ({}, {"kometa_order": None}, {"kometa_order": []},
                     {"kometa_order": "random"}, {"kometa_order": ""}, [], "year.asc"):
            with self.subTest(data=data):
                response = self.client.post("/api/jobs/test/kometa-order", json=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
                self.assertEqual(self.client.get("/api/jobs/test").json["kometa_order"], "year.desc")

    def test_missing_job(self):
        response = self.client.post("/api/jobs/missing/kometa-order", json={"kometa_order": "year.asc"})
        self.assertEqual(response.status_code, 404)

    def test_source_order_requires_tmdb_list(self):
        response = self.client.post("/api/jobs/test/kometa-order", json={"kometa_order": "custom"})
        self.assertEqual(response.status_code, 400)
        with get_session() as session:
            session.get(Job, "test").tmdb_list_id = "123"
        response = self.client.post("/api/jobs/test/kometa-order", json={"kometa_order": "custom"})
        self.assertEqual(response.status_code, 200)

    def test_manual_and_scheduled_publish_use_saved_order(self):
        self.client.post("/api/jobs/test/kometa-order", json={"kometa_order": "title.desc"})
        with patch.object(kometa, "create_collection", return_value="Wrote collection") as create, \
                patch.object(kometa, "trigger_run", return_value="Triggered run"), \
                patch.object(main, "_parse_into_job", return_value=(1, 1, 1)):
            response = self.client.post("/api/jobs/test/kometa")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(create.call_args.kwargs["collection_order"], "title.desc")
            with get_session() as session:
                schedule = Schedule(source_url="https://example.test/list", job_id="test",
                                    publish_kometa=True)
                session.add(schedule)
                session.flush()
                schedule_id = schedule.id
            main._run_schedule(schedule_id)
            self.assertEqual(create.call_count, 2)
            self.assertEqual(create.call_args.kwargs["collection_order"], "title.desc")

    def test_page_contains_all_options(self):
        response = self.client.get("/job/test")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'for="kometaOrder"', response.data)
        for order, label in config.KOMETA_ORDER_OPTIONS.items():
            self.assertIn(f'<option value="{order}">{label}</option>'.encode(), response.data)


class MigrationTests(unittest.TestCase):
    def test_existing_jobs_default_to_year_desc_without_losing_data(self):
        models.Base.metadata.drop_all(models.engine)
        with models.engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, source_url TEXT, list_name TEXT, "
                "status TEXT, stage TEXT, source_text TEXT, error TEXT, content_type TEXT, "
                "tmdb_list_id TEXT, radarr_status TEXT, radarr_checked_at DATETIME, created_at DATETIME)"
            )
            conn.exec_driver_sql(
                "INSERT INTO jobs (id, source_url, list_name, tmdb_list_id) "
                "VALUES ('legacy', 'https://example.test/list', 'Berlinale Winners', '123')"
            )
        models.init_db()
        models.init_db()
        with get_session() as session:
            job = session.get(Job, "legacy")
            self.assertEqual(job.kometa_order, "year.desc")
            self.assertEqual(job.list_name, "Berlinale Winners")
            self.assertEqual(job.tmdb_list_id, "123")


if __name__ == "__main__":
    unittest.main()
