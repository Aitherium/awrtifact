"""awrtifact CLI — the deliberate chunk-and-release loop.

    awrtifact split FILE [--part-size N] [--out DIR]
    awrtifact plan MANIFEST --repo OWNER/REPO --release TAG
    awrtifact upload MANIFEST --repo OWNER/REPO --release TAG [--dir DIR] [--parallel N] [--create]
    awrtifact verify MANIFEST [--dir DIR]
    awrtifact fetch NAME --url BASE --out DIR [--expected N] [--verify-only]
    awrtifact serve-spec SPEC [--emit-dir DIR] [--check]
    awrtifact backup-catalog SPEC [--workflow W] [--dry-run]
    awrtifact strata-push MANIFEST [--tenant T] [--tier warm] [--dir DIR]
    awrtifact strata-fetch NAME --out DIR [--tenant T] [--tier warm] [--sha256 HEX]
    awrtifact companion-backup PERSONA --repo OWNER/NAME [--passphrase-env VAR] [--allow-public]
    awrtifact companion-restore PERSONA --repo OWNER/NAME [--tag TAG] [--passphrase-env VAR]
    awrtifact seal-archive FILE --repo OWNER/NAME --tag TAG --recipient PUB|KEYID
    awrtifact unseal-restore --repo OWNER/NAME --tag TAG --out PATH [--key-file F]
                             [--archive-id ID --expect-sha256 HEX]
    awrtifact archive-keygen --private-out F --public-out F [--passphrase-file F]
                             [--github-secret N --secret-repo R]

Exit codes: 0 ok · 1 operational failure (upload failed, fetch short, drift) ·
2 usage or data error (bad manifest/spec, missing dependency).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, gh
from . import archive as archive_mod
from . import backup as backup_mod
from . import companion as companion_mod
from . import envelope as envelope_mod
from . import fetch as fetch_mod
from . import fetchset as fetchset_mod
from . import hf as hf_mod
from . import manifest as manifest_mod
from . import mirror as mirror_mod
from . import plan as plan_mod
from . import provision as provision_mod
from . import sealing as sealing_mod
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
        if hf_mod.is_repo_ref(args.source):
            token = args.hf_token or os.getenv("HF_TOKEN") or None
            result = mirror_mod.mirror_hf_repo(
                args.source, args.repo, args.release or None,
                lane=args.lane, token=token, part_size=args.part_size,
                prefix=args.prefix, max_lanes=args.max_lanes,
                workflow=(args.workflow if args.workflow != mirror_mod.DEFAULT_WORKFLOW
                          else mirror_mod.SET_WORKFLOW),
                work_dir=Path(args.work_dir) if args.work_dir else None,
                seed_workflow=not args.no_seed, dry_run=args.dry_run,
                seal=args.seal or bool(args.seal_key),
                seal_key=Path(args.seal_key) if args.seal_key else None,
                dedupe_from=args.dedupe_from or None,
                create_repo=({"private": False, "public": True}.get(args.create_repo)),
                log=lambda msg: print(f"  {msg}", file=sys.stderr),
            )
            gh.print_json({k: v for k, v in result.items() if k != "items"})
            items = result.get("items", [])
            for it in items[:50]:
                print(f"  {it['asset']}\t{it['size']}\t{it['path']}")
            if len(items) > 50:
                print(f"  ... {len(items) - 50} more")
            return 0
        if not args.release:
            return _die("--release is required for a single URL or file")
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
    except (ValueError, OSError, gh.GhError, hf_mod.HfError) as exc:
        return _die(str(exc))
    print(f"mirror {result['lane']}: {result}")
    return 0


def _cmd_fetch_set(args: argparse.Namespace) -> int:
    try:
        data = fetchset_mod.load_set_from_release(
            args.repo, args.release, args.name, expect_key=args.expect_key or None)
        report = fetchset_mod.fetch_set(data, Path(args.dest), only=args.only or None)
    except (ValueError, OSError, gh.GhError, sealing_mod.SealUnavailableError) as exc:
        return _die(str(exc))
    if "_seal" in data:
        report["seal"] = data["_seal"]
    gh.print_json(report)
    if report["unverified"]:
        print(f"UNVERIFIED (no known sha256): {len(report['unverified'])} file(s)",
              file=sys.stderr)
    return 0 if report["ok"] else 1


def _cmd_provision(args: argparse.Namespace) -> int:
    try:
        result = provision_mod.provision_repo(
            args.repo, public=args.public, pages=not args.no_pages
        )
    except (ValueError, gh.GhError) as exc:
        return _die(str(exc))
    print(f"repo ready: {result['html_url']}")
    for f in result["seeded"]:
        print(f"  seeded {f}")
    if result["pages"]:
        print(f"gate page: {result['gate_url']}  "
              f"(share this + the passphrase)")
    else:
        print("gate page: OFF — private repos need a paid plan for Pages; "
              "the repo and backups still work, only the share-by-page half needs it")
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


def _companion_passphrase(args: argparse.Namespace, confirm: bool) -> str:
    return companion_mod.passphrase_from_env_or_prompt(args.passphrase_env, confirm=confirm)


def _cmd_companion_backup(args: argparse.Namespace) -> int:
    try:
        envelope_mod.require_crypto()
        passphrase = _companion_passphrase(args, confirm=True)
        extra = {}
        if args.card:
            extra[Path(args.card).name] = Path(args.card)
        result = companion_mod.backup(
            args.persona_id, args.repo, passphrase,
            recall_root=Path(args.recall_dir) if args.recall_dir else None,
            extra_files=extra, awm_scope=args.awm_scope or None,
            allow_public=args.allow_public, provision=not args.no_provision,
        )
    except envelope_mod.CryptoUnavailableError as exc:
        return _die(str(exc), 2)
    except (companion_mod.CompanionBackupError, envelope_mod.EnvelopeError) as exc:
        return _die(str(exc), 2)
    except (OSError, gh.GhError) as exc:
        return _die(str(exc), 1)
    print(f"release {result['tag']} in {result['repo']}: {len(result['assets'])} asset(s)")
    try:
        index = companion_mod.record_backup(args.persona_id, result["repo"], result["tag"])
        print(f"recorded in {index} (the next `awsettings --domain companions push` carries it)")
    except (companion_mod.CompanionBackupError, OSError) as exc:
        # The backup itself landed; only the pointer a new machine restores from
        # did not. Say so loudly instead of failing a good backup.
        print(f"warning: backup not recorded in the companions index: {exc}", file=sys.stderr)
    return 0


def _cmd_companion_restore(args: argparse.Namespace) -> int:
    try:
        envelope_mod.require_crypto()
        passphrase = _companion_passphrase(args, confirm=False)
        report = companion_mod.restore(
            args.persona_id, args.repo, passphrase, tag=args.tag or None,
            recall_root=Path(args.recall_dir) if args.recall_dir else None,
        )
    except envelope_mod.CryptoUnavailableError as exc:
        return _die(str(exc), 2)
    except (companion_mod.CompanionBackupError, envelope_mod.EnvelopeError) as exc:
        return _die(str(exc), 2)
    except (OSError, gh.GhError) as exc:
        return _die(str(exc), 1)
    gh.print_json(report)
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
    p.add_argument("source", help="Range-serving URL, a local file path, or a "
                   "Hugging Face repo (hf://org/name[@rev][/sub], "
                   "https://huggingface.co/[datasets/]org/name, org/name)")
    p.add_argument("--release", default="", help="release tag to upload into "
                   "(required for a URL/file; default hf-<org>--<name>-<sha7> for a repo)")
    p.add_argument("--repo", default="Aitherium/aitherkvcache",
                   help="OWNER/NAME target -- any repo you can write to")
    p.add_argument("--name", help="served asset name (default: URL basename / filename)")
    p.add_argument("--total", type=int, help="declared size; fail loud on mismatch")
    p.add_argument("--workflow", default=mirror_mod.DEFAULT_WORKFLOW)
    p.add_argument("--parallel", type=int, default=4)
    # whole-repo lane
    p.add_argument("--lane", choices=("auto", "cloud", "local", "plan"), default="auto",
                   help="repo mirrors: cloud (runners fetch Hub->release), local "
                   "(stream through this box), plan (enumerate only)")
    p.add_argument("--dry-run", action="store_true", help="plan; touch nothing")
    p.add_argument("--hf-token", default="", help="Hub token (else $HF_TOKEN)")
    p.add_argument("--part-size", type=int, default=manifest_mod.DEFAULT_PART_SIZE)
    p.add_argument("--prefix", default="", help="asset-name prefix (several sets, one release)")
    p.add_argument("--max-lanes", type=int, default=20, help="cloud lane parallel runners")
    p.add_argument("--work-dir", default="", help="local lane staging dir (default: temp)")
    p.add_argument("--no-seed", action="store_true",
                   help="never seed the workflow into the target repo")
    p.add_argument("--create-repo", choices=("no", "private", "public"), default="no",
                   help="create the target repo if missing (default: refuse)")
    p.add_argument("--seal", action="store_true",
                   help="sign the set manifest with awseal (default key) and upload the seal")
    p.add_argument("--seal-key", default="", help="awseal private key path (implies --seal)")
    p.add_argument("--dedupe-from", default="",
                   help="earlier release of this set: unchanged files are served from it, "
                        "not uploaded again")
    p.set_defaults(func=_cmd_mirror)

    p = sub.add_parser("fetch-set", help="restore a mirrored HF repo from a release")
    p.add_argument("--repo", required=True, help="OWNER/NAME holding the release")
    p.add_argument("--release", required=True)
    p.add_argument("--name", help="set name when the release holds several")
    p.add_argument("--dest", default=".", help="directory to restore into")
    p.add_argument("--only", action="append", help="restore only this path (repeatable)")
    p.add_argument("--expect-key", default="", help="refuse a set not sealed by this awseal key")
    p.set_defaults(func=_cmd_fetch_set)

    p = sub.add_parser(
        "provision-repo",
        help="create + seed a backup repo (README, mod, gate page) with Pages",
    )
    p.add_argument("--repo", required=True, help="OWNER/NAME — created if missing")
    p.add_argument(
        "--public", action="store_true",
        help="public repo (Pages always works; bytes are encrypted anyway)",
    )
    p.add_argument("--no-pages", action="store_true", help="skip the Pages enable")
    p.set_defaults(func=_cmd_provision)

    for verb, helptext in (
        ("companion-backup", "encrypt a companion's memories into a new private release"),
        ("companion-restore", "restore a companion's memories (append-only merge)"),
    ):
        # allow_abbrev=False: otherwise `--passphrase SECRET` silently matches
        # --passphrase-env and the secret lands in argv AND an error message.
        p = sub.add_parser(verb, help=helptext, allow_abbrev=False)
        p.add_argument("persona_id")
        p.add_argument("--repo", required=True, help="OWNER/NAME (private; created if missing)")
        p.add_argument("--passphrase-env", default=companion_mod.DEFAULT_PASSPHRASE_ENV,
                       help="env var holding the passphrase (else a no-echo prompt; "
                            "never an argument)")
        p.add_argument("--recall-dir", default="", help="recall root (default: resolved)")
        if verb == "companion-backup":
            p.add_argument("--card", default="", help="a card/persona JSON to include")
            p.add_argument("--awm-scope", default="", help="awm scope to export, if awm can")
            p.add_argument("--allow-public", action="store_true",
                           help="accept a PUBLIC repo (persona ids leak in tags/names)")
            p.add_argument("--no-provision", action="store_true",
                           help="fail instead of creating a missing repo")
            p.set_defaults(func=_cmd_companion_backup)
        else:
            p.add_argument("--tag", default="", help="release tag (default: newest)")
            p.set_defaults(func=_cmd_companion_restore)

    p = sub.add_parser("backup-catalog", help="dispatch mirror runs for gaps")
    p.add_argument("spec")
    p.add_argument("--workflow", default=backup_mod.DEFAULT_WORKFLOW)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_backup)

    p = sub.add_parser("strata-push",
                       help="put a split artifact's parts into an AitherStrata pool "
                            "(content addressed; unchanged parts are not re-sent)")
    p.add_argument("manifest")
    p.add_argument("--tenant", default="", help="pool tenant (else $AWSTORAGE_STRATA_TENANT)")
    p.add_argument("--tier", default="warm", choices=("hot", "warm", "cold"))
    p.add_argument("--dir")
    p.set_defaults(func=_cmd_strata_push)

    p = sub.add_parser("strata-fetch",
                       help="stitch an artifact back from an AitherStrata pool, verified")
    p.add_argument("name")
    p.add_argument("--out", required=True)
    p.add_argument("--tenant", default="", help="pool tenant (else $AWSTORAGE_STRATA_TENANT)")
    p.add_argument("--tier", default="warm", choices=("hot", "warm", "cold"))
    p.add_argument("--sha256", default=None,
                   help="pin the whole-file digest (from a lockfile or spec, not the pool)")
    p.set_defaults(func=_cmd_strata_fetch)

    p = sub.add_parser(
        "seal-archive", allow_abbrev=False,
        help="encrypt a file to a recipient public key into a PRIVATE repo release "
             "(the sealer cannot decrypt it)")
    p.add_argument("file")
    p.add_argument("--repo", required=True, help="OWNER/NAME -- must be private (else exit 2)")
    p.add_argument("--tag", required=True, help="release tag (created if missing)")
    p.add_argument("--recipient", required=True,
                   help="the key holder's public key: a .pub path, a 16-hex key id "
                        "(looked up in --recipients-dir), or the base64 key")
    p.add_argument("--recipients-dir", default="",
                   help="where <keyid>.pub files live (default ~/.aither/recipients)")
    p.add_argument("--name", default="",
                   help="file name recorded INSIDE the sealed metadata (default: the "
                        "file's name); asset names are always the opaque archive id")
    p.add_argument("--part-size", type=int, default=archive_mod.DEFAULT_PART_SIZE)
    p.add_argument("--work-dir", default="", help="scratch dir for ONE part (default: temp)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_seal_archive)

    p = sub.add_parser(
        "unseal-restore", allow_abbrev=False,
        help="download (authenticated), verify and decrypt a sealed archive")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--out", default="", help="output file or directory")
    p.add_argument("--name", default="",
                   help="pick the archive whose SEALED name is this, when the release "
                        "holds several")
    p.add_argument("--archive-id", default="",
                   help="pin the archive (32 hex, recorded at seal time)")
    p.add_argument("--expect-sha256", default="",
                   help="refuse unless the archive holds exactly these plaintext bytes "
                        "(the eviction canary passes the original's sha256)")
    p.add_argument("--key-file", default="",
                   help="private key file (raw, or passphrase-protected by archive-keygen)")
    p.add_argument("--key-stdin", action="store_true",
                   help="read the private key from stdin")
    p.add_argument("--key-env", default=archive_mod.DEFAULT_KEY_ENV,
                   help="env var holding the private key when no --key-file/--key-stdin; "
                        "removed from the environment as it is read (never argv)")
    p.add_argument("--passphrase-file", default="",
                   help="passphrase for a protected key file (else "
                        f"${archive_mod.DEFAULT_PASSPHRASE_ENV}, else a prompt)")
    p.add_argument("--work-dir", default="", help="scratch dir for ONE part (default: temp)")
    p.add_argument("--verify-only", action="store_true",
                   help="decrypt and check every byte, write nothing (the canary proof)")
    p.add_argument("--force", action="store_true", help="replace an existing --out file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_unseal_restore)

    p = sub.add_parser(
        "archive-keygen", allow_abbrev=False,
        help="make YOUR recipient keypair (run it on the key holder's machine)")
    p.add_argument("--private-out", required=True,
                   help="private key file: passphrase-protected, owner-only")
    p.add_argument("--public-out", required=True, help="public key file (give this out)")
    p.add_argument("--passphrase-file", default="",
                   help=f"passphrase source (else ${archive_mod.DEFAULT_PASSPHRASE_ENV}, "
                        "else a prompt, asked twice)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="write the private key UNPROTECTED (not allowed with --github-secret)")
    p.add_argument("--github-secret", default="",
                   help="also store the PROTECTED private key as this Actions secret ...")
    p.add_argument("--secret-repo", default="",
                   help="... in this PRIVATE repo under YOUR account or an org you "
                        "administer (value via stdin, never argv)")
    p.add_argument("--force", action="store_true", help="overwrite existing key files")
    p.set_defaults(func=_cmd_archive_keygen)

    return parser


def _archive_errors(fn):
    try:
        return fn()
    except (archive_mod.CryptoUnavailableError, archive_mod.RefusedError) as exc:
        return _die(str(exc), 2)
    except (archive_mod.ArchiveError, gh.GhError, OSError) as exc:
        return _die(str(exc), 1)


def _cmd_seal_archive(args: argparse.Namespace) -> int:
    def run() -> int:
        archive_mod.require_crypto()
        pub, kid = archive_mod.load_recipient(
            args.recipient, Path(args.recipients_dir) if args.recipients_dir else None)
        result = archive_mod.seal_archive(
            Path(args.file), args.repo, args.tag, pub, name=args.name,
            part_size=args.part_size,
            work_dir=Path(args.work_dir) if args.work_dir else None)
        if args.json:
            shown = {k: v for k, v in result.items() if k not in ("wraps", "meta")}
            gh.print_json({**shown, "recipient_key_id": kid})
        else:
            print(f"sealed {result['name']}: {result['plain_size']} bytes -> "
                  f"{len(result['parts'])} part(s) in {args.repo}@{args.tag} "
                  f"for recipient {kid}; archive id {result['archive_id']}, plaintext "
                  f"sha256 {result['plain_sha256']} (record both: an eviction guard "
                  f"passes them to unseal-restore); peak local ciphertext "
                  f"{result['peak_work_bytes']} B")
        return 0
    return _archive_errors(run)


def _cmd_unseal_restore(args: argparse.Namespace) -> int:
    def run() -> int:
        archive_mod.require_crypto()
        key = archive_mod.load_private_key(
            args.key_file, args.key_env, key_stdin=args.key_stdin,
            passphrase_file=args.passphrase_file)
        result = archive_mod.unseal_restore(
            args.repo, args.tag, Path(args.out) if args.out else None, key,
            name=args.name, archive_id=args.archive_id,
            expect_plain_sha256=args.expect_sha256,
            work_dir=Path(args.work_dir) if args.work_dir else None,
            verify_only=args.verify_only, force=args.force)
        del key
        if args.json:
            gh.print_json(result)
        else:
            where = result["path"] or "(nothing written)"
            print(f"{result['status']}: {where} ({result['bytes']} bytes, "
                  f"sha256 {result['plain_sha256'][:16]}..., {result['parts']} part(s))")
        return 0
    return _archive_errors(run)


def _cmd_archive_keygen(args: argparse.Namespace) -> int:
    def run() -> int:
        archive_mod.require_crypto()
        passphrase = None if args.no_passphrase else archive_mod.read_passphrase(
            args.passphrase_file, confirm=True, prompt="new archive key passphrase: ")
        result = archive_mod.keygen(
            Path(args.private_out), Path(args.public_out), passphrase=passphrase,
            force=args.force, github_secret=args.github_secret,
            secret_repo=args.secret_repo)
        del passphrase
        state = "passphrase-protected" if result["protected"] else "UNPROTECTED"
        print(f"key id {result['key_id']}: public {result['public']}, "
              f"private {result['private']} ({state}; keep it and the passphrase -- "
              "nobody else can restore)")
        if not result["owner_only"]:
            print("warning: could not restrict the private key file to your account; "
                  "check its permissions", file=sys.stderr)
        if result["github_secret"]:
            print(f"protected private key also stored as Actions secret "
                  f"{result['github_secret']} (keep the passphrase OUT of GitHub)")
        return 0
    return _archive_errors(run)


def _strata_store(args: argparse.Namespace):
    from .backends import strata as strata_mod  # noqa: PLC0415 -- optional backend

    return strata_mod, strata_mod.from_env(args.tenant or None, args.tier)


def _cmd_strata_push(args: argparse.Namespace) -> int:
    try:
        strata_mod, store = _strata_store(args)
    except Exception as exc:  # noqa: BLE001 - backend unavailable is a usage error
        return _die(str(exc))
    try:
        m = manifest_mod.load(Path(args.manifest))
        result = store.upload(m, Path(args.dir) if args.dir else Path(args.manifest).parent)
    except (ValueError, OSError, strata_mod.StrataBackendError) as exc:
        return _die(str(exc), 1)
    print(f"pooled {len(result['uploaded'])} part(s); "
          f"{len(result['skipped_present'])} already in the pool; manifest {result['manifest']}")
    for f in result["failed"]:
        print(f"  FAILED: {f}", file=sys.stderr)
    return 1 if result["failed"] else 0


def _cmd_strata_fetch(args: argparse.Namespace) -> int:
    try:
        strata_mod, store = _strata_store(args)
    except Exception as exc:  # noqa: BLE001
        return _die(str(exc))
    try:
        m = store.load_manifest(args.name)
        result = store.fetch(m, Path(args.out), expected_sha256=args.sha256)
    except (ValueError, OSError, strata_mod.StrataBackendError) as exc:
        return _die(str(exc), 1)
    print(f"{result['status']}: {result['path']} ({result['bytes']} bytes, "
          f"sha256 {result['sha256'][:16]}...)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
