"""awrtifact CLI — the deliberate chunk-and-release loop.

    awrtifact split FILE [--part-size N] [--out DIR]
    awrtifact plan MANIFEST --repo OWNER/REPO --release TAG
    awrtifact upload MANIFEST --repo OWNER/REPO --release TAG [--dir DIR] [--parallel N] [--create]
    awrtifact verify MANIFEST [--dir DIR]
    awrtifact fetch NAME --url BASE --out DIR [--expected N] [--verify-only]
    awrtifact serve-spec SPEC [--emit-dir DIR] [--check]
    awrtifact backup-catalog SPEC [--workflow W] [--dry-run]

Exit codes: 0 ok · 1 operational failure (upload failed, fetch short, drift) ·
2 usage or data error (bad manifest/spec, missing dependency).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, gh
from . import backup as backup_mod
from . import fetch as fetch_mod
from . import manifest as manifest_mod
from . import mirror as mirror_mod
from . import plan as plan_mod
from . import serve_spec as serve_spec_mod
from . import split as split_mod
from . import upload as upload_mod
from . import verify as verify_mod


def _die(message: str, code: int = 2) -> int:
    print(f"awrtifact: {message}", file=sys.stderr)
    return code


def _cmd_split(args: argparse.Namespace) -> int:
    try:
        m = split_mod.split_file(Path(args.file), args.part_size,
                                 Path(args.out) if args.out else None)
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    out = Path(args.out) if args.out else Path(args.file).parent
    manifest_path = out / "manifest.json"
    manifest_mod.write(m, manifest_path)
    print(f"split {m['name']}: {m['total']} bytes -> "
          f"{len(m['parts'])} parts, manifest {manifest_path}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        m = manifest_mod.load(Path(args.manifest))
        planned = plan_mod.plan_parts(m, args.repo, args.release)
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    gh.print_json(planned)
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    try:
        m = manifest_mod.load(Path(args.manifest))
        result = upload_mod.upload_manifest(
            m, args.repo, args.release,
            Path(args.dir) if args.dir else Path(args.manifest).parent,
            parallel=args.parallel, create=args.create,
        )
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    print(f"uploaded {len(result['uploaded'])} part(s); "
          f"{len(result['skipped_present'])} already present")
    if result["failed"]:
        for f in result["failed"]:
            print(f"  FAILED: {f}", file=sys.stderr)
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        m = manifest_mod.load(Path(args.manifest))
        report = verify_mod.verify_manifest(m, Path(args.dir) if args.dir
                                            else Path(args.manifest).parent)
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    if args.json:
        gh.print_json(report)
    else:
        for check in report["checks"]:
            mark = "ok" if check["ok"] else "FAIL " + ", ".join(check["errors"])
            print(f"  {check['part']}: {mark}")
        whole = report["whole"]
        mark = "ok" if whole["ok"] else "FAIL " + ", ".join(whole["errors"])
        print(f"  whole-file: {mark}")
    return 0 if report["ok"] else 1


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        result = fetch_mod.fetch(
            args.name, args.url, Path(args.out),
            expected=args.expected, lockfile=Path(args.lock) if args.lock else None,
            verify_only=args.verify_only,
        )
    except (ValueError, OSError, fetch_mod.FetchError) as exc:
        return _die(str(exc), 1)
    print(f"{result['status']}: {result['path']} ({result['bytes']} bytes, "
          f"sha256 {result['sha256'][:16]}…)")
    return 0 if result["status"] in ("fetched", "verified", "up-to-date") else 1


def _cmd_serve_spec(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    try:
        dirs = serve_spec_mod.emit(spec_mod_load(spec_path), spec_path, check=args.check)
    except ValueError as exc:
        return _die(str(exc))
    if args.check:
        print(f"spec {spec_path}: generated workers are current")
    else:
        for d in dirs:
            print(f"generated worker -> {d}")
    return 0


def spec_mod_load(path: Path) -> dict:
    # Imported lazily so the core commands stay stdlib-only; the spec module
    # raises a clear "pip install awrtifact[spec]" when PyYAML is absent.
    from . import spec as spec_mod  # noqa: PLC0415 — optional dependency

    return spec_mod.load(path)


def _cmd_mirror(args: argparse.Namespace) -> int:
    try:
        if mirror_mod.URL_RE.match(args.source):
            result = mirror_mod.mirror_url(
                args.source, args.name, args.release, args.repo,
                total=args.total, workflow=args.workflow,
            )
        else:
            result = mirror_mod.mirror_file(
                Path(args.source), args.release, args.repo,
                name=args.name, parallel=args.parallel,
            )
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    print(f"mirror {result['lane']}: {result}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    try:
        spec = spec_mod_load(Path(args.spec))
        result = backup_mod.backup_catalog(
            spec, workflow=args.workflow, dry_run=args.dry_run
        )
    except (ValueError, OSError) as exc:
        return _die(str(exc))
    print(f"planned {result['dispatched']} dispatch(es); "
          f"{result['already_present']} artifact(s) already in their releases")
    if result.get("local_only"):
        print("local-only (no cloud source — mirror from the local path):")
        for name in result["local_only"]:
            print(f"  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="awrtifact",
        description="Deliberately chunk artifacts into GitHub release assets "
        "and fetch them back byte-verified.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("split", help="split a file into .partN slices + manifest")
    p.add_argument("file")
    p.add_argument("--part-size", type=int, default=1900000000)
    p.add_argument("--out")
    p.set_defaults(func=_cmd_split)

    p = sub.add_parser("plan", help="which parts are missing from the release")
    p.add_argument("manifest")
    p.add_argument("--repo", required=True)
    p.add_argument("--release", required=True)
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("upload", help="upload missing parts (resumable)")
    p.add_argument("manifest")
    p.add_argument("--repo", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--dir")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--create", action="store_true")
    p.set_defaults(func=_cmd_upload)

    p = sub.add_parser("verify", help="verify local parts against the manifest")
    p.add_argument("manifest")
    p.add_argument("--dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("fetch", help="resumable, verified download")
    p.add_argument("name")
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--expected", type=int)
    p.add_argument("--lock")
    p.add_argument("--verify-only", action="store_true")
    p.set_defaults(func=_cmd_fetch)

    p = sub.add_parser("serve-spec", help="generate the worker from the spec")
    p.add_argument("spec")
    p.add_argument("--emit-dir")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=_cmd_serve_spec)

    p = sub.add_parser(
        "mirror",
        help="feed it a URL or file — it mirrors to GitHub seamlessly",
    )
    p.add_argument("source", help="Range-serving URL or a local file path")
    p.add_argument("--release", required=True, help="release tag to upload into")
    p.add_argument("--repo", default="Aitherium/aitherkvcache")
    p.add_argument("--name", help="served asset name (default: URL basename / filename)")
    p.add_argument("--total", type=int, help="declared size; fail loud on mismatch")
    p.add_argument("--workflow", default=mirror_mod.DEFAULT_WORKFLOW)
    p.add_argument("--parallel", type=int, default=4)
    p.set_defaults(func=_cmd_mirror)

    p = sub.add_parser("backup-catalog", help="dispatch mirror runs for gaps")
    p.add_argument("spec")
    p.add_argument("--workflow", default=backup_mod.DEFAULT_WORKFLOW)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
