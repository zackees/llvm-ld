#!/usr/bin/env python3
"""Tests for tools/ci_jobs.py (#55, #56): the job table, the cache policy and the runner.

Stdlib only. Run: python -m unittest discover -s tests -p 'test_ci_jobs.py'.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ci_jobs  # noqa: E402

TEMPLATE_USE = re.compile(r"uses: \./\.github/actions/ci-job\n\s+with:\n\s+job: (\S+)")


class JobTableTest(unittest.TestCase):
    def test_every_template_use_names_a_declared_job(self):
        used = set()
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            used |= set(TEMPLATE_USE.findall(workflow.read_text()))
        self.assertTrue(used, "no workflow uses the ci-job template")
        self.assertEqual(sorted(used - set(ci_jobs.JOBS)), [])

    def test_no_workflow_uses_sccache_or_the_old_templates(self):
        migrated = ("link-benchmark.yml", "ci.yml", "correctness.yml", "benchmark.yml")
        for workflow in (ROOT / ".github" / "workflows" / name for name in migrated):
            text = workflow.read_text()
            for gone in ("sccache", "zccache-build", "pgo-setup", "record-timing"):
                self.assertNotIn(gone, text, f"{workflow.name} still mentions {gone}")

    def test_template_pins_the_same_zccache_as_the_runner(self):
        text = (ROOT / ".github" / "actions" / "ci-job" / "action.yml").read_text()
        self.assertIn(f"zackees/zccache@{ci_jobs.ZCCACHE_VERSION}", text)
        self.assertIn(f"zackees/zccache/action/cleanup@{ci_jobs.ZCCACHE_VERSION}", text)

    def test_artifact_jobs_have_a_directory(self):
        for job in ci_jobs.JOBS.values():
            if job.artifact_key:
                self.assertTrue(job.artifact_dir, job.name)


class CachePolicyTest(unittest.TestCase):
    def test_only_main_and_dispatches_save(self):
        main = {"GITHUB_REF": "refs/heads/main"}
        self.assertTrue(ci_jobs.saves_caches({**main, "GITHUB_EVENT_NAME": "push"}))
        self.assertTrue(ci_jobs.saves_caches({**main, "GITHUB_EVENT_NAME": "schedule"}))
        self.assertTrue(ci_jobs.saves_caches({"GITHUB_REF": "refs/heads/x", "GITHUB_EVENT_NAME": "workflow_dispatch"}))
        self.assertFalse(ci_jobs.saves_caches({"GITHUB_REF": "refs/pull/1/merge", "GITHUB_EVENT_NAME": "pull_request"}))
        self.assertFalse(ci_jobs.saves_caches({"GITHUB_REF": "refs/tags/v1", "GITHUB_EVENT_NAME": "push"}))

    def test_warm_means_zero_misses(self):
        self.assertTrue(ci_jobs.is_warm({"status": "ok", "hits": 10, "misses": 0}))
        self.assertFalse(ci_jobs.is_warm({"status": "ok", "hits": 10, "misses": 1}))
        self.assertFalse(ci_jobs.is_warm({"status": "error"}))
        self.assertFalse(ci_jobs.is_warm(None))


class FloorTest(unittest.TestCase):
    job = ci_jobs.Job("t-floor", lambda args: None, warm_max_minutes=5)

    def test_warm_run_over_the_floor_fails(self):
        self.assertIn("> floor 5", ci_jobs.check_floor(self.job, {"job": "x", "seconds": 400, "warm": True}))

    def test_cold_run_over_the_floor_is_only_reported(self):
        self.assertIsNone(ci_jobs.check_floor(self.job, {"job": "x", "seconds": 4000, "warm": False}))

    def test_warm_run_under_the_floor_passes(self):
        self.assertIsNone(ci_jobs.check_floor(self.job, {"job": "x", "seconds": 200, "warm": True}))

    def test_every_build_job_outside_link_benchmark_has_a_floor(self):
        for job in ci_jobs.JOBS.values():
            # link-benchmark has its own floors (bench_floors.json); release legs are rarely warm.
            if job.cache_group and not job.name.startswith(("bench-", "release-")):
                self.assertIsNotNone(job.warm_max_minutes, job.name)


class ReleaseJobTest(unittest.TestCase):
    def test_each_triple_has_its_own_cache_group(self):
        job = ci_jobs.JOBS["release-build"]
        groups = {ci_jobs.cache_group(job, ci_jobs.argparse.Namespace(triple=t))
                  for t in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-musl")}
        self.assertEqual(groups, {"release-x86_64-unknown-linux-gnu", "release-aarch64-unknown-linux-musl"})

    def test_kinds(self):
        self.assertEqual([ci_jobs.release_kind(t) for t in ("x86_64-pc-windows-msvc", "aarch64-unknown-linux-musl",
                                                            "x86_64-apple-darwin", "aarch64-unknown-linux-gnu")],
                         ["windows", "musl", "macos", "linux"])

    def test_container_builds_are_never_warm(self):
        job = ci_jobs.Job("t-container", lambda args: setattr(args, "unobserved", True), cache_group="g")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"RUNNER_TEMP": tmp, "GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}), \
                mock.patch.dict(ci_jobs.JOBS, {job.name: job}), \
                mock.patch.object(ci_jobs, "session_start", return_value="s"), \
                mock.patch.object(ci_jobs, "session_end", return_value={"status": "ok", "misses": 0}):
            ci_jobs.main(["run", job.name])
            record = json.loads(next(pathlib.Path(tmp, "timing").glob("*.json")).read_text())
        self.assertFalse(record["warm"])


class RunnerTest(unittest.TestCase):
    def run_job(self, job, *argv):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"RUNNER_TEMP": tmp, "GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}), \
                mock.patch.dict(ci_jobs.JOBS, {job.name: job}):
            status = ci_jobs.main(["run", job.name, *argv])
            records = [json.loads(p.read_text()) for p in pathlib.Path(tmp, "timing").glob("*.json")]
        return status, records

    def test_cached_job_is_skipped_and_warm(self):
        calls = []
        job = ci_jobs.Job("t-cached", lambda args: calls.append(1), cache_group="g")
        status, records = self.run_job(job, "--cached", "true", "--label", "plain")
        self.assertEqual((status, calls), (0, []))
        self.assertEqual(records[0]["job"], "plain")
        self.assertTrue(records[0]["warm"])

    def test_compiling_job_without_zccache_is_cold(self):
        job = ci_jobs.Job("t-build", lambda args: None, cache_group="g")
        with mock.patch.object(ci_jobs, "session_start", return_value=None):
            status, records = self.run_job(job)
        self.assertEqual(status, 0)
        self.assertFalse(records[0]["warm"])

    def test_session_stats_decide_warmth_and_are_recorded(self):
        job = ci_jobs.Job("t-warm", lambda args: None, cache_group="g")
        with mock.patch.object(ci_jobs, "session_start", return_value="s1"), \
                mock.patch.object(ci_jobs, "session_end", return_value={"status": "ok", "hits": 5, "misses": 0,
                                                                        "compilations": 5}):
            _, records = self.run_job(job)
        self.assertTrue(records[0]["warm"])
        self.assertEqual(records[0]["zccache"]["hits"], 5)

    def test_failed_command_fails_the_job_but_still_records_timing(self):
        def fail(args):
            raise ci_jobs.subprocess.CalledProcessError(2, ["false"])
        job = ci_jobs.Job("t-fail", fail)
        status, records = self.run_job(job)
        self.assertEqual(status, 1)
        self.assertEqual(len(records), 1)

    def test_a_failure_under_zccache_is_retried_once_with_the_cache_bypassed(self):
        calls = []

        def flaky(args):
            calls.append(os.environ.get("ZCCACHE_DISABLE"))
            if len(calls) == 1:
                raise ci_jobs.subprocess.CalledProcessError(113, ["ninja"])
        job = ci_jobs.Job("t-flaky", flaky, cache_group="g")
        with mock.patch.dict(os.environ, {}), mock.patch.object(ci_jobs, "session_start", return_value=None):
            os.environ.pop("ZCCACHE_DISABLE", None)
            status, records = self.run_job(job)
        self.assertEqual((status, calls), (0, [None, "1"]))
        self.assertFalse(records[0]["warm"])
        self.assertIn("bypassed", records[0]["detail"])

    def test_a_real_error_still_fails_after_the_retry(self):
        def broken(args):
            raise ci_jobs.subprocess.CalledProcessError(1, ["ninja"])
        job = ci_jobs.Job("t-broken", broken, cache_group="g")
        with mock.patch.dict(os.environ, {}), mock.patch.object(ci_jobs, "session_start", return_value=None):
            os.environ.pop("ZCCACHE_DISABLE", None)
            status, _ = self.run_job(job)
        self.assertEqual(status, 1)

    def test_post_phase_adds_its_time_to_the_record(self):
        job = ci_jobs.Job("t-post", lambda args: None, post=lambda args: None)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"RUNNER_TEMP": tmp, "GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}), \
                mock.patch.dict(ci_jobs.JOBS, {job.name: job}):
            self.assertEqual(ci_jobs.main(["run", job.name]), 0)
            self.assertEqual(ci_jobs.main(["post", job.name]), 0)
            record = json.loads(next(pathlib.Path(tmp, "timing").glob("*.json")).read_text())
        self.assertTrue(record["warm"])

    def test_measurement_only_job_is_warm(self):
        job = ci_jobs.Job("t-measure", lambda args: None)
        _, records = self.run_job(job, "--label", "cells:debug")
        self.assertEqual(records[0]["job"], "cells:debug")
        self.assertTrue(records[0]["warm"])


if __name__ == "__main__":
    unittest.main()
