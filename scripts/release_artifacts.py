"""Build/accept candidates without write credentials; verify them without installing.

The privileged jobs only download and inspect bytes. Recovery reuses distribution
bytes from a failed guarded run, but produces NEW installed acceptance in the
current run. It never inherits an old authorization or rebuilds published bytes.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import venv
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

from scripts.normalize_pyproject_prerelease import pep440_prerelease
from scripts.release_gate import (
    REPOSITORY,
    SHA256,
    WORKFLOWS,
    Context,
    GitHub,
    fresh,
    gate,
    positive_id,
    require,
    validate_run,
)

ROOT = Path(__file__).resolve().parent.parent
TEST_DIRECTORY = "tests/conformance/reporting"
MANIFEST = "acceptance.json"
MAX_UNPACKED = 1024 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def tracked(directory: str) -> list[str]:
    return [path for path in git("ls-files", "-z", directory).split("\0") if path]


def versions() -> tuple[str, str]:
    semver = json.loads((ROOT / ".release-please-manifest.json").read_text())["."]
    require(
        re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?", semver),
        "unsupported release version",
    )
    project = re.search(
        r"^\[project\]\s*$((?:(?!^\[).)*)(?=^\[|\Z)",
        (ROOT / "pyproject.toml").read_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    require(project is not None, "missing project section")
    matches = re.findall(
        r'^version\s*=\s*"([^"\n]+)"\s*$', project.group(1) if project else "", re.MULTILINE
    )
    require(len(matches) == 1, "missing or ambiguous project version")
    version = matches[0]
    require(version == pep440_prerelease(semver), "manifest and project versions differ")
    return version, "v" + semver


def distribution_names(version: str) -> set[str]:
    return {f"adcp-{version}-py3-none-any.whl", f"adcp-{version}.tar.gz"}


def safe_name(name: str) -> None:
    path = PurePosixPath(name)
    require(
        name and not path.is_absolute() and ".." not in path.parts and str(path) == name,
        "unsafe archive member",
    )
    require("\\" not in name and "\0" not in name, "unsafe archive member")


def inventory(path: Path, version: str) -> dict[str, str]:
    """Hash every regular member; reject duplicates, links and ambiguous paths."""
    members: dict[str, str] = {}
    metadata: dict[str, bytes] = {}

    def record(name: str, stream: Any) -> None:
        safe_name(name)
        require(name not in members, "duplicate archive member")
        value = hashlib.sha256()
        capture = name.endswith("/METADATA") or name == f"adcp-{version}/PKG-INFO"
        content = bytearray()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
            if capture:
                content.extend(block)
                require(len(content) <= 1024 * 1024, "oversize distribution metadata")
        members[name] = value.hexdigest()
        if capture:
            metadata[name] = bytes(content)

    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            require(
                sum(entry.file_size for entry in archive.infolist()) <= MAX_UNPACKED,
                "oversize wheel",
            )
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                require(not stat.S_ISLNK(entry.external_attr >> 16), "wheel contains a link")
                with archive.open(entry) as stream:
                    record(entry.filename, stream)
        expected_metadata = f"adcp-{version}.dist-info/METADATA"
        require(
            all(name.startswith(("adcp/", f"adcp-{version}.dist-info/")) for name in members),
            "wheel contains files outside the SDK and its metadata",
        )
    else:
        with tarfile.open(path, "r:gz") as sdist:
            entries = sdist.getmembers()
            require(sum(entry.size for entry in entries) <= MAX_UNPACKED, "oversize sdist")
            for member in entries:
                if member.isdir():
                    continue
                require(member.isfile(), "sdist contains a link or special file")
                tar_stream = sdist.extractfile(member)
                require(tar_stream is not None, "sdist member is unreadable")
                if tar_stream is not None:
                    with tar_stream:
                        record(member.name, tar_stream)
        expected_metadata = f"adcp-{version}/PKG-INFO"
        require(
            all(name.startswith(f"adcp-{version}/") for name in members),
            "sdist contains files outside its distribution root",
        )
    require(set(metadata) == {expected_metadata}, "missing or ambiguous package metadata")
    parsed = email.parser.BytesParser().parsebytes(metadata[expected_metadata])
    require(parsed.get_all("Name") == ["adcp"], "wrong distribution name")
    require(parsed.get_all("Version") == [version], "wrong distribution version")
    return members


def expected_package() -> dict[str, str]:
    """Derive the complete SDK payload from tracked source, including schema data."""
    result = {
        path.removeprefix("src/"): file_digest(ROOT / path)
        for path in tracked("src/adcp")
        if Path(path).suffix in {".py", ".pyi", ".json", ".sql"}
        or Path(path).name in {"py.typed", "ADCP_VERSION", "UPSTREAM_COMMIT"}
    }
    protocol = (ROOT / "src/adcp/ADCP_VERSION").read_text().strip()
    current = protocol if "-" in protocol else ".".join(protocol.split(".")[:2])
    for bundle in {"2.5", "3.0", "3.1", current}:
        paths = [
            path
            for path in tracked(f"schemas/cache/{bundle}")
            if path.endswith(".json") and Path(path).name != ".hashes.json"
        ]
        require(paths, f"missing tracked schema bundle: {bundle}")
        result.update(
            {
                path.replace("schemas/cache/", "adcp/_schemas/", 1): file_digest(ROOT / path)
                for path in paths
            }
        )
    require(
        "adcp/__init__.py" in result and "adcp/py.typed" in result, "incomplete source inventory"
    )
    return result


def test_inventory() -> dict[str, str]:
    files = {
        path: file_digest(ROOT / path) for path in tracked(TEST_DIRECTORY) if path.endswith(".py")
    }
    require(
        any(Path(path).name.startswith("test_") for path in files),
        "installed acceptance suite missing",
    )
    return files


def snapshot(directory: Path, version: str) -> dict[str, Any]:
    expected = expected_package()
    files: dict[str, Any] = {}
    for name in sorted(distribution_names(version)):
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "missing distribution")
        contents = inventory(path, version)
        if name.endswith(".whl"):
            payload = {path: sha for path, sha in contents.items() if path.startswith("adcp/")}
            require(payload == expected, "wheel package inventory differs from exact source")
        else:
            prefix = f"adcp-{version}/"
            for source in tracked("src/adcp"):
                if source.removeprefix("src/") in expected:
                    require(
                        contents.get(prefix + source) == file_digest(ROOT / source),
                        "sdist source mismatch",
                    )
            for member, sha in expected.items():
                if member.startswith("adcp/_schemas/"):
                    source = member.replace("adcp/_schemas/", "schemas/cache/", 1)
                    require(contents.get(prefix + source) == sha, "sdist schema inventory mismatch")
            for source in ("pyproject.toml", "setup.py", "MANIFEST.in"):
                require(
                    contents.get(prefix + source) == file_digest(ROOT / source),
                    "sdist build inputs differ",
                )
        files[name] = {
            "sha256": file_digest(path),
            "size": path.stat().st_size,
            "inventory": contents,
        }
    return files


# Executed with python -I inside a NEW venv, outside the repository. No editable
# install, PYTHONPATH, checkout imports, or package execution in privileged jobs.
INSTALLED_PROBE = """
import hashlib, importlib.metadata, json, pathlib, sys
spec = json.loads(pathlib.Path(sys.argv[1]).read_text())
dist = importlib.metadata.distribution('adcp')
assert dist.version == spec['version']
actual = {
    str(p): hashlib.sha256(pathlib.Path(dist.locate_file(p)).read_bytes()).hexdigest()
    for p in dist.files
    if str(p).startswith('adcp/') and '__pycache__' not in p.parts
}
assert actual == spec['package'], 'installed inventory differs from accepted wheel/source'
import adcp, adcp.types
assert pathlib.Path(adcp.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
for module in (adcp, adcp.types):
    for name in module.__all__:
        getattr(module, name)
print(json.dumps({'version': dist.version, 'python': sys.version.split()[0]}))
"""


def junit_result(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    require(suites, "installed test report is missing")
    result = {
        key: sum(int(suite.attrib[key]) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    require(result["tests"] > 0, "installed tests did not run")
    require(
        all(result[key] == 0 for key in ("failures", "errors", "skipped")),
        "installed tests did not all pass",
    )
    return result


def installed_acceptance(path: Path, version: str) -> dict[str, Any]:
    require(
        os.environ.get("ADCP_PG_TEST_URL"), "installed reporting acceptance requires PostgreSQL"
    )
    package = expected_package()
    tests = test_inventory()
    with tempfile.TemporaryDirectory(prefix="adcp-installed-") as temporary:
        work = Path(temporary)
        venv.EnvBuilder(with_pip=True, symlinks=True).create(work / "venv")
        python = work / "venv/bin/python"
        environment = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "GH_TOKEN", "GITHUB_TOKEN"):
            environment.pop(key, None)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                f"{path.resolve()}[pg]",
                "pytest==8.4.2",
                "pytest-asyncio==1.2.0",
            ],
            cwd=work,
            env=environment,
            check=True,
        )
        specification = work / "specification.json"
        specification.write_bytes(canonical({"version": version, "package": package}))
        probe = subprocess.check_output(
            [str(python), "-I", "-c", INSTALLED_PROBE, str(specification)],
            cwd=work,
            env=environment,
            text=True,
        )
        identity = json.loads(probe)
        require(identity["python"].startswith("3.10."), "installed acceptance must use Python 3.10")
        for source in tests:
            destination = work / "suite" / Path(source).relative_to(TEST_DIRECTORY)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, destination)
        report = work / "results.xml"
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pytest",
                "-p",
                "pytest_asyncio.plugin",
                "-c",
                "/dev/null",
                "-o",
                "asyncio_mode=auto",
                "--junitxml",
                str(report),
                str(work / "suite"),
                "-q",
            ],
            cwd=work,
            env=environment,
            check=True,
        )
        return {
            **identity,
            "artifact_sha256": file_digest(path),
            "package_inventory_sha256": digest(canonical(package)),
            "test_inventory_sha256": digest(canonical(tests)),
            "junit_sha256": file_digest(report),
            "result": junit_result(report),
        }


def validate_manifest(
    manifest: dict[str, Any], directory: Path, context: Context, *, historical: bool = False
) -> None:
    require(git("rev-parse", "HEAD") == context.target, "checkout is not the accepted commit")
    require(not git("diff", "HEAD", "--"), "tracked source changed during acceptance")
    version, tag = versions()
    require(
        manifest["schema"] == 1 and manifest["repository"] == REPOSITORY,
        "unknown acceptance format",
    )
    require(manifest["target_sha"] == context.target, "artifact source commit mismatch")
    require(
        manifest["source_tree"] == git("rev-parse", "HEAD^{tree}"), "artifact source tree mismatch"
    )
    require(
        manifest["run_id"] == context.run_id and manifest["run_attempt"] == 1, "replayed acceptance"
    )
    require(manifest["operation"] == context.operation, "wrong acceptance capability")
    require(manifest["release_pr"] == context.release_pr, "wrong accepted release PR")
    require(manifest["version"] == version and manifest["tag"] == tag, "accepted version mismatch")
    require(
        set(manifest["files"]) == distribution_names(version), "distribution inventory mismatch"
    )
    require(
        set(path.name for path in directory.iterdir()) == distribution_names(version) | {MANIFEST},
        "unexpected artifact files",
    )
    require(
        manifest["files"] == snapshot(directory, version), "artifact hash or inventory mismatch"
    )
    if not historical:
        require(
            manifest["ci_run_id"] == context.ci_run_id
            and manifest["ci_run_attempt"] == context.ci_attempt,
            "CI evidence changed",
        )
        require(manifest["recover_from"] == context.recover_from, "recovery identity changed")
        fresh(manifest["accepted_at"], datetime.now(timezone.utc))
    require(
        set(manifest["installed"]) == distribution_names(version), "installed acceptance missing"
    )
    for name, accepted in manifest["installed"].items():
        require(
            accepted["artifact_sha256"] == manifest["files"][name]["sha256"],
            "installed artifact hash mismatch",
        )
        require(accepted["version"] == version, "installed version mismatch")
        require(accepted["python"].startswith("3.10."), "floor installation was not accepted")
        require(
            accepted["package_inventory_sha256"] == digest(canonical(expected_package())),
            "installed package inventory mismatch",
        )
        require(
            accepted["test_inventory_sha256"] == digest(canonical(test_inventory())),
            "installed test inventory mismatch",
        )
        require(SHA256.fullmatch(accepted["junit_sha256"]), "installed report hash missing")
        result = accepted["result"]
        require(
            result["tests"] > 0
            and all(result[key] == 0 for key in ("errors", "failures", "skipped")),
            "installed acceptance incomplete",
        )


def download(
    api: GitHub,
    context: Context,
    artifact_id: int,
    directory: Path,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    metadata = api.repo(f"actions/artifacts/{artifact_id}")
    require(
        metadata["id"] == artifact_id and metadata["expired"] is False,
        "artifact is missing or expired",
    )
    require(metadata["name"] == f"release-acceptance-{context.run_id}-1", "wrong artifact identity")
    provenance = metadata["workflow_run"]
    require(
        provenance["id"] == context.run_id
        and provenance["head_sha"] == context.target
        and provenance["head_branch"] == "main",
        "artifact run mismatch",
    )
    repository_id = api.repo("")["id"]
    require(
        provenance["repository_id"] == repository_id
        and provenance["head_repository_id"] == repository_id,
        "artifact came from another repository",
    )
    archive_digest = metadata["digest"].removeprefix("sha256:")
    require(SHA256.fullmatch(archive_digest), "artifact archive digest missing")
    if expected_digest is not None:
        require(
            archive_digest == expected_digest.removeprefix("sha256:"),
            "artifact output digest differs from GitHub",
        )
    content = api.artifact_bytes(artifact_id)
    require(digest(content) == archive_digest, "artifact archive checksum mismatch")
    version, _ = versions()
    names = distribution_names(version) | {MANIFEST}
    directory.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        require(
            set(archive.namelist()) == names and len(archive.namelist()) == len(names),
            "unexpected artifact archive members",
        )
        require(
            sum(entry.file_size for entry in archive.infolist()) <= MAX_UNPACKED,
            "oversize artifact",
        )
        for entry in archive.infolist():
            require(not stat.S_ISLNK(entry.external_attr >> 16), "artifact contains a link")
            # Fixed single-component allowlist; no extractall/path traversal.
            with (
                archive.open(entry) as source,
                (directory / entry.filename).open("xb") as destination,
            ):
                shutil.copyfileobj(source, destination)
    return cast(dict[str, Any], json.loads((directory / MANIFEST).read_bytes()))


def recover_candidate(api: GitHub, context: Context, directory: Path) -> None:
    require(context.operation == "publish" and context.recover_from, "recovery is publication-only")
    require(context.recover_from != context.run_id, "cannot recover the current invocation")
    old = api.repo(f"actions/runs/{context.recover_from}")
    validate_run(old, context.target, WORKFLOWS["publish"], "workflow_dispatch", 1)
    require(
        old["status"] == "completed" and old["conclusion"] in {"failure", "cancelled", "timed_out"},
        "only failed guarded invocations can be recovered",
    )
    artifacts = api.pages(f"actions/runs/{context.recover_from}/artifacts", "artifacts")
    candidates = [
        artifact
        for artifact in artifacts
        if artifact["name"] == f"release-acceptance-{context.recover_from}-1"
    ]
    require(len(candidates) == 1, "original candidate is absent or ambiguous")
    old_context = replace(context, run_id=positive_id(str(context.recover_from)), recover_from=None)
    manifest = download(api, old_context, candidates[0]["id"], directory)
    validate_manifest(manifest, directory, old_context, historical=True)
    # Only the immutable distributions survive. Every acceptance assertion is
    # regenerated by the current workflow, including installed tests and CI.
    (directory / MANIFEST).unlink()


def build_candidate(api: GitHub, context: Context, directory: Path) -> dict[str, Any]:
    require(git("rev-parse", "HEAD") == context.target, "checkout is not the target commit")
    require(not git("diff", "HEAD", "--"), "candidate source is dirty")
    gate(api, context)
    version, tag = versions()
    if context.recover_from:
        recover_candidate(api, context, directory)
    else:
        require(not directory.exists(), "candidate output must start empty")
        environment = dict(os.environ, SOURCE_DATE_EPOCH=git("show", "-s", "--format=%ct", "HEAD"))
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(directory)],
            cwd=ROOT,
            env=environment,
            check=True,
        )
    files = snapshot(directory, version)
    installed = {name: installed_acceptance(directory / name, version) for name in sorted(files)}
    require(files == snapshot(directory, version), "distributions changed during acceptance")
    evidence = gate(api, context)
    manifest = {
        "schema": 1,
        "repository": REPOSITORY,
        "target_sha": context.target,
        "source_tree": git("rev-parse", "HEAD^{tree}"),
        "run_id": context.run_id,
        "run_attempt": 1,
        "operation": context.operation,
        "release_pr": context.release_pr,
        "recover_from": context.recover_from,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "tag": tag,
        "files": files,
        "installed": installed,
        **evidence,
    }
    (directory / MANIFEST).write_bytes(canonical(manifest))
    validate_manifest(manifest, directory, context)
    return manifest


def verified_candidate(api: GitHub, context: Context, directory: Path) -> dict[str, Any]:
    gate(api, context)
    manifest = download(
        api,
        context,
        positive_id(os.environ["ARTIFACT_ID"]),
        directory,
        os.environ["ARTIFACT_DIGEST"],
    )
    validate_manifest(manifest, directory, context)
    # The artifact must have come from the completed reusable acceptance job in
    # THIS attempt, even on a writer-only rerun that reuses needs outputs.
    jobs = api.pages(f"actions/runs/{context.run_id}/attempts/1/jobs", "jobs")
    accepted = [job for job in jobs if job["name"].endswith(" / Build and accept distributions")]
    require(
        len(accepted) == 1
        and accepted[0]["status"] == "completed"
        and accepted[0]["conclusion"] == "success",
        "acceptance job did not complete successfully",
    )
    require(accepted[0]["head_sha"] == context.target, "acceptance job target mismatch")
    gate(api, context)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    context = Context.from_env()
    api = GitHub(os.environ["GH_TOKEN"])
    manifest = (build_candidate if args.command == "build" else verified_candidate)(
        api, context, args.directory.resolve()
    )
    summary = {
        "target_sha": context.target,
        "run_id": context.run_id,
        "version": manifest["version"],
        "files": {name: data["sha256"] for name, data in manifest["files"].items()},
    }
    print(json.dumps(summary, indent=2))
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(
                "Accepted distribution identity\n\n```json\n"
                + json.dumps(summary, indent=2)
                + "\n```\n"
            )


if __name__ == "__main__":
    main()
