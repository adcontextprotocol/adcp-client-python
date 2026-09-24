"""Publish only the accepted bytes; every recovery repeats the entire gate.

PyPI and GitHub have no joint transaction. Publish PyPI first, then the exact
commit tag/release. An interrupted invocation is recoverable only by a fresh
dispatch accepting the original bytes again. Never use --skip-existing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

from scripts.release_artifacts import (
    MANIFEST,
    canonical,
    validate_manifest,
    verified_candidate,
)
from scripts.release_gate import Context, GitHub, NoRedirect, gate, require


def receipt(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "target_sha": manifest["target_sha"],
        "version": manifest["version"],
        "tag": manifest["tag"],
        "release_pr": manifest["release_pr"],
        "files": {name: data["sha256"] for name, data in manifest["files"].items()},
    }


def pypi_version(version: str) -> dict[str, Any] | None:
    # version has already been validated against the tracked release manifest.
    try:
        with urllib.request.build_opener(NoRedirect).open(
            f"https://pypi.org/pypi/adcp/{version}/json", timeout=60
        ) as response:
            return cast(dict[str, Any], json.load(response))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def missing_files(manifest: dict[str, Any], published: dict[str, Any] | None) -> list[str]:
    expected = receipt(manifest)["files"]
    if published is None:
        return sorted(expected)
    require(
        published["info"]["name"] == "adcp" and published["info"]["version"] == manifest["version"],
        "wrong PyPI package/version",
    )
    present: dict[str, str] = {}
    for file in published["urls"]:
        name = file["filename"]
        require(name in expected and name not in present, "unexpected or duplicate PyPI file")
        require(file["yanked"] is False, "a yanked version requires an operator decision")
        require(file["digests"]["sha256"] == expected[name], "PyPI artifact checksum mismatch")
        present[name] = file["digests"]["sha256"]
    return sorted(expected.keys() - present.keys())


def release_state(api: GitHub, manifest: dict[str, Any]) -> tuple[Any, Any]:
    tag = api.optional(f"git/ref/tags/{manifest['tag']}")
    release = api.optional(f"releases/tags/{manifest['tag']}")
    if tag is not None:
        require(
            tag["object"]["type"] == "commit" and tag["object"]["sha"] == manifest["target_sha"],
            "existing tag has a different target or type",
        )
    if release is not None:
        require(tag is not None, "release is missing its exact commit tag")
        # GitHub ignores target_commitish when a tag already exists. Its stored
        # display value is not the release's target: the verified Git ref above
        # is authoritative. Creation still always supplies the literal SHA.
        require(release["tag_name"] == manifest["tag"], "existing release uses another tag")
        require(release["draft"] is False, "unexpected draft release")
        require(release["prerelease"] == ("-" in manifest["tag"]), "release stability mismatch")
        # Only releases created by this guard are recoverable. A tag name or a
        # matching version alone never authorizes adoption of an old release.
        recorded = json.loads(release["body"])
        require(recorded["acceptance"] == receipt(manifest), "existing release receipt mismatch")
    return tag, release


def recheck(api: GitHub, context: Context, manifest: dict[str, Any], directory: Path) -> None:
    require(context.operation == "publish", "proposal capability cannot publish")
    gate(api, context)
    validate_manifest(manifest, directory, context)


def prepare_pypi(
    api: GitHub, context: Context, manifest: dict[str, Any], directory: Path, upload: Path
) -> list[str]:
    recheck(api, context, manifest, directory)
    tag, release = release_state(api, manifest)
    published = pypi_version(manifest["version"])
    if context.recover_from is None:
        require(
            tag is None and release is None and published is None,
            "version already exists; replay is forbidden",
        )
    missing = missing_files(manifest, published)
    upload.mkdir(parents=True, exist_ok=False)
    for name in missing:
        shutil.copyfile(directory / name, upload / name)
    recheck(api, context, manifest, directory)
    return missing


def verify_pypi(api: GitHub, context: Context, manifest: dict[str, Any], directory: Path) -> None:
    recheck(api, context, manifest, directory)
    require(
        not missing_files(manifest, pypi_version(manifest["version"])),
        "PyPI publication is incomplete",
    )


def publish_github(
    api: GitHub, context: Context, manifest: dict[str, Any], directory: Path
) -> None:
    verify_pypi(api, context, manifest, directory)
    tag, release = release_state(api, manifest)
    if context.recover_from is None:
        require(tag is None and release is None, "tag/release already exists; replay is forbidden")
    if tag is None:
        recheck(api, context, manifest, directory)
        api.repo("git/refs", "POST", {"ref": f"refs/tags/{manifest['tag']}", "sha": context.target})
    # Even after an uncertain create, only an exact existing tag may be used.
    release_state(api, manifest)
    if release is None:
        recheck(api, context, manifest, directory)
        api.repo(
            "releases",
            "POST",
            {
                "tag_name": manifest["tag"],
                "target_commitish": context.target,
                "name": manifest["tag"],
                "draft": False,
                "prerelease": "-" in manifest["tag"],
                "body": canonical(
                    {
                        "acceptance": receipt(manifest),
                        "run_id": context.run_id,
                        "artifact_id": int(os.environ["ARTIFACT_ID"]),
                        "artifact_digest": os.environ["ARTIFACT_DIGEST"],
                    }
                ).decode(),
            },
        )
    _, created = release_state(api, manifest)
    require(created is not None, "GitHub release was not created")
    # Complete the normal Release Please lifecycle, without another invocation
    # of its release-capable action or broader issues:write permissions.
    labels = {label["name"] for label in api.repo(f"pulls/{context.release_pr}")["labels"]}
    if "autorelease:tagged" not in labels:
        recheck(api, context, manifest, directory)
        api.repo(f"issues/{context.release_pr}/labels", "POST", {"labels": ["autorelease:tagged"]})
    if "autorelease:pending" in labels:
        recheck(api, context, manifest, directory)
        api.repo(f"issues/{context.release_pr}/labels/autorelease%3Apending", "DELETE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-pypi", "verify-pypi", "github"))
    args = parser.parse_args()
    context = Context.from_env()
    require(context.operation == "publish", "proposal capability cannot publish")
    api = GitHub(os.environ["GH_TOKEN"])
    directory = Path("accepted-candidate").resolve()
    if args.command == "verify-pypi":
        manifest = json.loads((directory / MANIFEST).read_bytes())
        verify_pypi(api, context, manifest, directory)
    else:
        manifest = verified_candidate(api, context, directory)
        if args.command == "github":
            publish_github(api, context, manifest, directory)
        else:
            missing = prepare_pypi(api, context, manifest, directory, Path("publish-dist"))
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(f"upload={'true' if missing else 'false'}\n")


if __name__ == "__main__":
    main()
