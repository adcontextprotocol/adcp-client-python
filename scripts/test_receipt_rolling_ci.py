"""Workflow/control regressions; the actual receipt SDK test is unchanged."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

import receipt_rolling_ci as ci


class ReceiptCIControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            "GITHUB_REPOSITORY": "adcontextprotocol/adcp-client-python",
            "RECEIPT_SOURCE_HEAD": "1" * 40,
            "GITHUB_SHA": "2" * 40,
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        self.scope = ci.identity(self.env)

    def artifacts(self):
        target = self.root / "artifacts"
        target.mkdir()
        rows = []
        for index, artifact in enumerate(ci.IDS, 1):
            name = ci.artifact_name(self.scope, artifact)
            path = target / name
            path.mkdir()
            ci.write_json(path / "attempted.json", {**self.scope, "artifact": artifact})
            ci.write_json(
                path / "collection.json", [{"node": ci.node(a), "parameter": a} for a in ci.IDS]
            )
            (path / "stdout.log").write_bytes(b"safe workflow-control fixture\n")
            (path / "stderr.log").write_bytes(b"")
            builds = [
                {"artifact": a, "pin": ci.PINS[a], "wheel_sha256": "a" * 64}
                for a in sorted({artifact, "b21"})
            ]
            scenarios = [
                {
                    "artifact": artifact,
                    "notifications": n,
                    "pin": ci.PINS[artifact],
                    "parent": ci.PINS["b21"],
                    "wheel_sha256": "a" * 64,
                    "before_after_exercises": 2,
                }
                for n in ci.notifications(artifact)
            ]
            m = {
                "version": 1,
                "identity": self.scope,
                "artifact": artifact,
                "history_pin": ci.PINS[artifact],
                "selected_node": ci.node(artifact),
                "checked_out_head": self.scope["source_head"],
                "complete": True,
                "execution": {
                    "exit": 0,
                    "timed_out": False,
                    "seconds": 1.5,
                    "timeout_seconds": 900,
                },
                "command": [
                    sys.executable,
                    "scripts/reporting_test_harness.py",
                    sys.executable,
                    "-m",
                    "pytest",
                    ci.node(artifact),
                    "-v",
                    "-s",
                    "-ra",
                    "--color=no",
                    "--junitxml=/private/junit.xml",
                ],
                "raw_outputs": {
                    "stdout.log": {"bytes": 100, "sha256": "b" * 64},
                    "stderr.log": {"bytes": 0, "sha256": "c" * 64},
                },
                "pytest": {
                    "node": ci.node(artifact),
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                },
                "scenarios": scenarios,
                "builds": builds,
                "files": {n: ci.file_identity(path / n) for n in ci.FILES - {"manifest.json"}},
                "error": None,
            }
            ci.write_json(path / "manifest.json", m)
            rows.append(
                {
                    "id": index,
                    "name": name,
                    "expired": False,
                    "workflow_run": {"id": 123, "head_sha": self.scope["source_head"]},
                }
            )
        return rows, target

    def test_actual_eight_fixture_parameters_and_workflow(self):
        ci.verify_history()
        ci.verify_workflow()
        ci.verify_collection(ci.collect())

    def test_pull_request_and_push_source_identity(self):
        self.assertNotEqual(self.scope["source_head"], self.scope["event_sha"])
        env = {**self.env, "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "1" * 40}
        self.assertEqual(ci.identity(env)["source_head"], ci.identity(env)["event_sha"])
        with self.assertRaises(ci.ContractError):
            ci.identity({**env, "GITHUB_SHA": "3" * 40})
        for key, value in (
            ("GITHUB_RUN_ATTEMPT", "0"),
            ("GITHUB_RUN_ID", "../123"),
            ("RECEIPT_SOURCE_HEAD", "not-a-sha"),
            ("GITHUB_EVENT_NAME", "pull_request_target"),
        ):
            with self.subTest(key=key), self.assertRaises(ci.ContractError):
                ci.identity({**self.env, key: value})

    def test_complete_eight_artifact_aggregate(self):
        rows, target = self.artifacts()
        ci.verify_artifacts(rows, target, self.scope, "success", "success")
        self.assertEqual(sum(len(ci.notifications(a)) for a in ci.IDS), 12)

    def test_push_aggregate_uses_source_and_event_sha(self):
        self.scope = ci.identity({**self.env, "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "1" * 40})
        rows, target = self.artifacts()
        ci.verify_artifacts(rows, target, self.scope, "success", "success")
        rows[0]["workflow_run"]["head_sha"] = "2" * 40
        with self.assertRaises(ci.ContractError):
            ci.verify_artifacts(rows, target, self.scope, "success", "success")

    def test_matrix_and_download_must_both_succeed(self):
        rows, target = self.artifacts()
        for result in ("failure", "cancelled", "skipped", ""):
            for matrix, download in ((result, "success"), ("success", result)):
                with (
                    self.subTest(matrix=matrix, download=download),
                    self.assertRaises(ci.ContractError),
                ):
                    ci.verify_artifacts(rows, target, self.scope, matrix, download)

    def test_missing_extra_duplicate_and_wrong_api_identity(self):
        rows, target = self.artifacts()
        corruptions = [rows[:-1], rows + [rows[0]], rows[:-1] + [rows[0]]]
        for field, value in (
            ("id", rows[1]["id"]),
            ("id", True),
            ("name", ci.artifact_name(self.scope, "extra")),
            ("expired", True),
        ):
            bad = copy.deepcopy(rows)
            bad[0][field] = value
            corruptions.append(bad)
        for field, value in (("id", 124), ("head_sha", "3" * 40)):
            bad = copy.deepcopy(rows)
            bad[0]["workflow_run"][field] = value
            corruptions.append(bad)
        for bad in corruptions:
            with self.subTest(case=corruptions.index(bad)), self.assertRaises(ci.ContractError):
                ci.verify_artifacts(bad, target, self.scope, "success", "success")

    def test_closed_typed_manifest_and_failed_or_missing_test(self):
        rows, target = self.artifacts()
        path = target / rows[0]["name"] / "manifest.json"
        original = ci.strict_json(path.read_bytes())
        mutations = [
            lambda m: m.update(extra=True),
            lambda m: m.update(version=True),
            lambda m: m.update(complete=False),
            lambda m: m.update(execution=None),
            lambda m: m["execution"].update(exit=1),
            lambda m: m["execution"].update(exit=False),
            lambda m: m["execution"].update(timed_out=True),
            lambda m: m["execution"].update(seconds=0),
            lambda m: m["execution"].update(timeout_seconds=1800),
            lambda m: m["command"].__setitem__(5, ci.node("b21")),
            lambda m: m["raw_outputs"]["stdout.log"].update(bytes=0),
            lambda m: m["files"]["stderr.log"].update(bytes=False),
            lambda m: m["builds"].__setitem__(0, None),
            lambda m: m["builds"][0].update(wheel_sha256=42),
            lambda m: m.update(pytest=None),
            lambda m: m["pytest"].update(passed=True),
            lambda m: m["pytest"].update(skipped=1),
            lambda m: m.update(selected_node=ci.node("b21")),
            lambda m: m.update(history_pin="3" * 40),
            lambda m: m.update(checked_out_head="3" * 40),
            lambda m: m["identity"].update(source_head="3" * 40),
            lambda m: m["identity"].update(event_sha="3" * 40),
            lambda m: m["identity"].update(run_id="124"),
            lambda m: m["identity"].update(run_attempt="1"),
            lambda m: m.update(scenarios=[]),
            lambda m: m["scenarios"].append(m["scenarios"][0]),
            lambda m: m["scenarios"][0].update(notifications=0),
            lambda m: m["scenarios"][0].update(notifications=True),
        ]
        for index, mutate in enumerate(mutations):
            bad = copy.deepcopy(original)
            mutate(bad)
            ci.write_json(path, bad)
            with self.subTest(index=index), self.assertRaises(ci.ContractError):
                ci.verify_artifacts(rows, target, self.scope, "success", "success")
        ci.write_json(path, original)
        ci.verify_artifacts(rows, target, self.scope, "success", "success")

    def test_file_missing_extra_changed_and_symlink(self):
        rows, target = self.artifacts()
        path = target / rows[0]["name"] / "stdout.log"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(ci.ContractError):
            ci.verify_artifacts(rows, target, self.scope, "success", "success")
        path.write_bytes(b"changed\n")
        with self.assertRaises(ci.ContractError):
            ci.verify_artifacts(rows, target, self.scope, "success", "success")
        path.unlink()
        path.symlink_to(target / rows[1]["name"] / "stdout.log")
        with self.assertRaises(ci.ContractError):
            ci.verify_artifacts(rows, target, self.scope, "success", "success")
        path.unlink()
        path.write_bytes(original)
        extra = target / "unexpected"
        extra.mkdir()
        with self.assertRaises(ci.ContractError):
            ci.verify_artifacts(rows, target, self.scope, "success", "success")

    def test_collection_missing_duplicate_extra_and_wrong_parameter(self):
        rows = [{"node": ci.node(a), "parameter": a} for a in ci.IDS]
        for bad in (
            rows[:-1],
            rows + [rows[0]],
            rows[:-1] + [rows[0]],
            [{**rows[0], "parameter": "b21"}, *rows[1:]],
        ):
            with self.assertRaises(ci.ContractError):
                ci.verify_collection(bad)

    def test_workflow_matrix_mutation_controls(self):
        import yaml

        workflow = yaml.safe_load((ci.ROOT / ".github/workflows/ci.yml").read_text())
        for values in (list(ci.IDS[:-1]), [*ci.IDS, "extra"], [*ci.IDS[:-1], "beta15"]):
            bad = copy.deepcopy(workflow)
            bad["jobs"]["pg-reporting-receipt-compatibility-shard"]["strategy"]["matrix"][
                "artifact"
            ] = values
            path = self.root / "ci.yml"
            path.write_text(yaml.safe_dump(bad))
            with self.assertRaises(ci.ContractError):
                ci.verify_workflow(path)
        for job in (
            "pg-reporting-receipt-compatibility-shard",
            "pg-reporting-receipt-compatibility",
        ):
            bad = copy.deepcopy(workflow)
            bad["jobs"][job]["env"]["RECEIPT_SOURCE_HEAD"] = "${{ github.sha }}"
            path.write_text(yaml.safe_dump(bad))
            with self.assertRaises(ci.ContractError):
                ci.verify_workflow(path)
            bad = copy.deepcopy(workflow)
            checkout = next(
                s
                for s in bad["jobs"][job]["steps"]
                if s.get("uses", "").startswith("actions/checkout@")
            )
            checkout["with"]["ref"] = "${{ github.sha }}"
            path.write_text(yaml.safe_dump(bad))
            with self.assertRaises(ci.ContractError):
                ci.verify_workflow(path)

    def test_workflow_rejects_reintroduced_locale_pin(self):
        import yaml

        workflow = yaml.safe_load((ci.ROOT / ".github/workflows/ci.yml").read_text())
        path = self.root / "ci.yml"
        for value in ("--encoding=UTF8 --lc-collate=C --lc-ctype=C", "--locale=en_US.utf8", ""):
            bad = copy.deepcopy(workflow)
            bad["jobs"]["pg-reporting-receipt-compatibility-shard"]["services"]["postgres"]["env"][
                "POSTGRES_INITDB_ARGS"
            ] = value
            path.write_text(yaml.safe_dump(bad))
            with self.subTest(value=value), self.assertRaises(ci.ContractError):
                ci.verify_workflow(path)

    def test_history_rejects_changed_integrated_artifact(self):
        root = self.root / "history"
        directory = root / "tests/conformance/reporting"
        directory.mkdir(parents=True)
        materializer = directory / "test_reporting_materializer_rolling.py"
        receipt = root / ci.TEST
        original = {key: ci.PINS[key] for key in ci.IDS[:-1]}
        materializer.write_text("ARTIFACTS = " + repr(original))
        receipt.write_text("B21 = " + repr(ci.PINS["b21"]))
        ci.verify_history(root)
        for artifact in ci.IDS:
            changed = dict(original)
            if artifact == "b21":
                receipt.write_text("B21 = " + repr("f" * 40))
            else:
                changed[artifact] = "f" * 40
                materializer.write_text("ARTIFACTS = " + repr(changed))
            with self.subTest(artifact=artifact), self.assertRaises(ci.ContractError):
                ci.verify_history(root)
            materializer.write_text("ARTIFACTS = " + repr(original))
            receipt.write_text("B21 = " + repr(ci.PINS["b21"]))

    def test_junit_requires_one_actual_pass(self):
        path = self.root / "junit.xml"
        case = (
            f'<testcase classname="{ci.TEST[:-3].replace("/", ".")}" name="{ci.FUNCTION}[beta15]"/>'
        )

        def document(tests="1", skipped="0", cases=case):
            return (
                f'<testsuites><testsuite tests="{tests}" skipped="{skipped}" '
                f'failures="0" errors="0">{cases}</testsuite></testsuites>'
            )

        path.write_text(document())
        self.assertEqual(ci.junit_result(path, "beta15")["passed"], 1)
        for raw in (
            document(tests="0", cases=""),
            document(tests="2", cases=case + case),
            document(skipped="1"),
            document(cases=case.replace("beta15", "b21")),
            '<!DOCTYPE test [<!ENTITY x "unsafe">]>' + document(),
        ):
            path.write_text(raw)
            with self.assertRaises(ci.ContractError):
                ci.junit_result(path, "beta15")

    def test_private_output_is_not_published(self):
        projection = ci.SafeOutput("beta15")
        for raw in (
            b"private database body",
            b"password=nonproduction-control",
            b"untrusted value in 1.0s",
            b'{"provider_response":"private"}',
        ):
            self.assertEqual(projection.line(raw), b"[non-allowlisted output omitted]\n")
        self.assertIn(
            b"1 passed", projection.line(b"================ 1 passed in 1.00s ================")
        )
        with self.assertRaises(ci.ContractError):
            ci.strict_json('{"exit":0,"exit":1}')
        with self.assertRaises(ci.ContractError):
            ci.strict_json('{"seconds":NaN}')
        with self.assertRaises(ci.ContractError):
            projection.line(b'{"frozen_build":null}')

    def test_child_failure_and_inner_timeout_keep_safe_partial_output(self):
        for index, (script, seconds, expected) in enumerate(
            (
                ("print('private body', flush=True); raise SystemExit(7)", 10, 7),
                ("import time; print('private body', flush=True); time.sleep(60)", 0.2, 124),
            )
        ):
            private = self.root / f"private-{index}"
            public = self.root / f"public-{index}"
            private.mkdir()
            public.mkdir()
            result = ci.execute(
                [sys.executable, "-c", script],
                private,
                public,
                ci.SafeOutput("beta15"),
                timeout=seconds,
            )
            self.assertEqual(result["exit"], expected)
            self.assertIn(b"private body", (private / "stdout.log").read_bytes())
            self.assertEqual(
                (public / "stdout.log").read_bytes(), b"[non-allowlisted output omitted]\n"
            )


if __name__ == "__main__":
    unittest.main()
