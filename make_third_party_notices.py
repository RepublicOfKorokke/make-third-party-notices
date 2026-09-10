# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Generate a third-party NOTICE/LICENSE report for a Cargo project.

Works from any location inside (or, with --manifest-path, pointed at) a
Cargo workspace. No dependencies beyond the Python standard library and a
Rust toolchain; designed to run standalone via:

    uv run make_third_party_notices.py [options]

Pipeline:
  1. Run `cargo vendor` to obtain the exact crate sources being shipped.
  2. Read `cargo metadata` to enumerate active, non-workspace packages.
  3. Collect every LICENSE/NOTICE-family file found in each vendored crate
     (local files are authoritative: they match the exact released version).
  4. For crates that ship NO license/notice text at all, fall back to a
     parallel GitHub fetch (version tags first, branches last) and mark
     branch-sourced hits as unverified.
  5. NOTICE audit: crates whose license expression includes Apache-2.0 but
     whose tarball ships no NOTICE file get a targeted remote NOTICE probe
     (many upstreams keep NOTICE at the repo root, excluded from the crate
     package by Cargo.toml `include` -- e.g. aws-lc-rs). Disable with
     --no-notice-audit.
  6. Deduplicate identical texts into a "License Catalog" and write the
     report atomically (temp file + rename), so a failed run never leaves
     a truncated report behind. Output format: HTML (self-contained) or
     plain text via --format txt.

Server politeness: all network requests pass a global token-bucket
throttle (default 5 req/s; --workers parallel jobs share it), candidate
filenames are pruned from the crate's license expression to avoid hopeless
404s, 403/429 responses honor Retry-After, and sustained pushback pauses
remote fetching entirely instead of hammering. --cache-dir makes repeat
runs network-free.

Usage:
    uv run make_third_party_notices.py [options]

Options:
    --output PATH        Report path (default: THIRD_PARTY_NOTICES.html).
    --format {html,txt}  Report format (default: html).
    --project NAME       Project name shown in the report
                         (default: cargo workspace root directory name).
    --manifest-path PATH Cargo manifest to inspect (default: nearest Cargo.toml).
    --vendor-dir PATH    Vendoring target (default: temp_vendor).
    --skip-vendor        Reuse an existing vendor dir (fast iteration).
    --offline            Never touch the network (skip remote fallback).
    --no-notice-audit    Skip the remote NOTICE probe for Apache-family crates.
    --fail-on-missing    Exit 1 if any crate has no license/notice text (CI gate).
    --workers N          Parallel remote jobs (default: 8).
    --rate N             Global HTTP requests per second, 0 disables (default: 5).
    --timeout SECONDS    Per-request HTTP timeout (default: 10).
    --cache-dir PATH     Cache remote hits and 404s for reuse (optional).
    --quiet              Suppress per-package progress output.

NOT LEGAL ADVICE / NO WARRANTY: this tool is not a lawyer and its reports
are attribution support, not a compliance guarantee. See README.md and the
Apache-2.0 LICENSE (Sections 6 and 7).

License of this script: Apache-2.0 (see LICENSE).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VENDOR_DIR = "temp_vendor"
DEFAULT_OUTPUT_FILE = "THIRD_PARTY_NOTICES.html"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_WORKERS = 8
DEFAULT_RATE_PER_SECOND = 5
THROTTLE_ABORT_HITS = 3  # 403/429 responses after which remote work pauses

LINE = "-" * 72
HEAVY = "=" * 72

IGNORE_DIRS = {".git", "tests", "test", "examples", "benches", "target"}
LICENSE_FILE_RE = re.compile(
    r"^(LICENSE|LICENCE|NOTICE|COPYING|COPYRIGHT)([._-].*)?$", re.IGNORECASE
)

# Order matters: generic license files first, NOTICE-style last. Extension
# variants cover monorepos that only ship e.g. LICENSE.md (wezterm, objc2).
REMOTE_CANDIDATES = (
    "LICENSE",
    "LICENCE",
    "LICENSE.md",
    "LICENCE.md",
    "LICENSE.txt",
    "LICENSE-MIT",
    "LICENSE-MIT.md",
    "LICENSE-MIT.txt",
    "LICENSE-APACHE",
    "LICENSE-APACHE.md",
    "LICENSE-APACHE.txt",
    "COPYING",
    "COPYING.md",
    "UNLICENSE",
    "NOTICE",
    "NOTICE.txt",
    "NOTICE.md",
)
GENERIC_LICENSE_NAMES = {
    "LICENSE",
    "LICENCE",
    "LICENSE.md",
    "LICENCE.md",
    "LICENSE.txt",
    "COPYING",
    "COPYRIGHT",
    "UNLICENSE",
}
BRANCH_REFS = frozenset({"main", "master"})
# Targets for the Apache-family NOTICE audit (see module docstring, step 5).
NOTICE_CANDIDATES = ("NOTICE", "NOTICE.txt", "NOTICE.md")
USER_AGENT = "make-third-party-notices/1.0"
TOOL_URL = "https://github.com/RepublicOfKorokke/make-third-party-notices"
DISCLAIMER_TEXT = (
    "Generated by make-third-party-notices -- attribution support, NOT legal advice.\n"
    "Provided \"AS IS\" with NO WARRANTY of any kind; completeness is not guaranteed.\n"
    f"{TOOL_URL}"
)
DISCLAIMER_HTML = (
    "<hr>\n<p class=\"meta\">Generated by "
    f"<a href=\"{TOOL_URL}\">make-third-party-notices</a> &mdash; attribution "
    "support, <strong>not legal advice</strong>; provided &quot;AS IS&quot; "
    "with <strong>no warranty</strong> of any kind. Completeness is not "
    "guaranteed: have a human review before relying on it.</p>\n"
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    license_expr: str
    license_file: str
    repo_url: str
    source: str | None  # cargo source string (None => path dependency)
    vendor_dir: Path | None


@dataclass(frozen=True)
class RemoteHit:
    filename: str
    content: str
    ref: str
    url: str

    @property
    def from_branch(self) -> bool:
        return self.ref in BRANCH_REFS


@dataclass
class FileRef:
    display: str  # path inside the crate, or fetched filename
    catalog_id: str
    remote_url: str | None = None
    remote_ref: str | None = None


@dataclass
class PackageEntry:
    package: Package
    origin: str  # local | remote-tag | remote-branch | missing
    files: list[FileRef] = field(default_factory=list)
    missing_reason: str = ""
    notice_origin: str = ""  # "" | remote-tag | remote-branch (NOTICE audit result)


@dataclass(frozen=True)
class CatalogEntry:
    cid: str
    title: str
    text: str


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Normalize line endings and surrounding whitespace for dedup hashing."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def title_of(text: str) -> str:
    """First non-empty, non-decoration line of a license/notice text."""
    for line in text.splitlines():
        stripped = line.strip("= #-*\t ")
        if stripped:
            return stripped[:72]
    return "(untitled)"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# License catalog (dedup store)
# --------------------------------------------------------------------------


class LicenseCatalog:
    """Maps each unique license/notice text to a stable catalog id [C<n>]."""

    def __init__(self) -> None:
        self._ids_by_digest: dict[str, str] = {}
        self.entries: list[CatalogEntry] = []
        self.file_counts: dict[str, int] = {}
        self.package_keys: dict[str, set[str]] = {}

    def add(self, text: str, package: Package) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cid = self._ids_by_digest.get(digest)
        if cid is None:
            cid = f"C{len(self.entries) + 1}"
            self._ids_by_digest[digest] = cid
            self.entries.append(CatalogEntry(cid=cid, title=title_of(text), text=text))
            self.file_counts[cid] = 0
            self.package_keys[cid] = set()
        self.file_counts[cid] += 1
        self.package_keys[cid].add(f"{package.name} {package.version}")
        return cid


# --------------------------------------------------------------------------
# Cargo / metadata
# --------------------------------------------------------------------------


def require_cargo(skip_vendor: bool) -> None:
    if shutil.which("cargo") is None:
        sys.exit("error: `cargo` was not found on PATH.")
    if not skip_vendor and subprocess.run(
        ["cargo", "vendor", "--help"], capture_output=True
    ).returncode != 0:
        sys.exit("error: `cargo vendor` is unavailable. Install it: cargo install cargo-vendor")


def run_cargo_vendor(vendor_dir: Path, quiet: bool, manifest_path: str | None) -> None:
    if vendor_dir.exists():
        shutil.rmtree(vendor_dir)
    cmd = ["cargo", "vendor", "--versioned-dirs"]
    if manifest_path:
        cmd += ["--manifest-path", manifest_path]
    cmd.append(str(vendor_dir))
    if quiet:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(cmd, check=True)


def load_packages(
    vendor_dir: Path, quiet: bool, manifest_path: str | None
) -> tuple[list[Package], str]:
    cmd = ["cargo", "metadata", "--format-version", "1"]
    if manifest_path:
        cmd += ["--manifest-path", manifest_path]
    metadata = json.loads(subprocess.check_output(cmd))
    active_ids = {node["id"] for node in metadata.get("resolve", {}).get("nodes", [])}
    workspace_ids = set(metadata.get("workspace_members", []))

    packages: list[Package] = []
    for raw in metadata.get("packages", []):
        if raw["id"] in workspace_ids:
            continue
        if active_ids and raw["id"] not in active_ids:
            continue
        name, version = raw["name"], raw["version"]
        vendor_path = vendor_dir / f"{name}-{version}"
        packages.append(
            Package(
                name=name,
                version=version,
                license_expr=raw.get("license") or "",
                license_file=raw.get("license_file") or "",
                repo_url=raw.get("repository") or "",
                source=raw.get("source"),
                vendor_dir=vendor_path if vendor_path.is_dir() else None,
            )
        )
    packages.sort(key=lambda p: p.name)

    missing_dir = [p for p in packages if p.vendor_dir is None]
    if missing_dir and not quiet:
        for p in missing_dir:
            kind = "path dependency" if p.source is None else "non-registry dependency"
            print(f"  [skip] {p.name} v{p.version}: {kind}, not present in vendor dir")
    default_project = Path(metadata.get("workspace_root") or ".").name
    return packages, default_project


# --------------------------------------------------------------------------
# Local file collection
# --------------------------------------------------------------------------


def find_local_files(pkg_dir: Path) -> list[tuple[str, str]]:
    """Return sorted (relative_path, normalized_text) license/notice pairs."""
    found: dict[str, str] = {}
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d.lower() not in IGNORE_DIRS]
        for filename in files:
            if not LICENSE_FILE_RE.match(filename):
                continue
            path = Path(root) / filename
            rel = str(path.relative_to(pkg_dir))
            try:
                found[rel] = normalize_text(read_text(path))
            except OSError as exc:
                print(f"  [warn] {pkg_dir.name}/{rel}: unreadable ({exc})", file=sys.stderr)
    return sorted(found.items())


# --------------------------------------------------------------------------
# Remote (GitHub) fetching: throttle + backoff + candidate pruning
# --------------------------------------------------------------------------


class Throttler:
    """Global minimum-interval limiter shared by all fetch threads."""

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._next_allowed = 0.0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._cond:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait > 0:
            time.sleep(wait)


def github_repo_path(repo_url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, dropping /tree/, /blob/ subpaths."""
    url = repo_url.rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            path = url[len(prefix) :]
            segments = [s for s in path.split("/") if s]
            if len(segments) < 2:
                return None
            owner, repo = segments[0], segments[1]
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{owner}/{repo}"
    return None


def refs_for(name: str, version: str) -> list[str]:
    candidates = [f"v{version}", version, f"{name}-v{version}", f"{name}-{version}", "main", "master"]
    return list(dict.fromkeys(candidates))


def expr_has(expr: str, token: str) -> bool:
    return re.search(rf"\b{token}\b", expr, re.IGNORECASE) is not None


def prune_candidates(license_expr: str) -> tuple[str, ...]:
    """Drop specific license files the expression cannot refer to, cutting 404 noise."""
    if not license_expr:
        return REMOTE_CANDIDATES
    kept = []
    for name in REMOTE_CANDIDATES:
        upper = name.upper()
        if upper.startswith("LICENSE-MIT") and not expr_has(license_expr, "MIT"):
            continue
        if upper.startswith("LICENSE-APACHE") and not expr_has(license_expr, "APACHE"):
            continue
        if upper == "UNLICENSE" and not (
            expr_has(license_expr, "UNLICENSE") or expr_has(license_expr, "ZLIB")
        ):
            continue
        kept.append(name)
    return tuple(kept) or REMOTE_CANDIDATES


class HttpFetcher:
    """GET raw.githubusercontent files with cache, throttling and backoff."""

    def __init__(
        self, cache_dir: Path | None, timeout: int, quiet: bool, rate: float
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.quiet = quiet
        self.throttler = Throttler(rate)
        self.aborted = False
        self._cooldown_until = 0.0
        self._pushback_hits = 0
        self._state_lock = threading.Lock()
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_file(self, url: str, suffix: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.{suffix}"

    def _wait_or_skip(self) -> bool:
        """False if remote work must stop (aborted); True after honoring cooldown."""
        with self._state_lock:
            if self.aborted:
                return False
            cooldown = self._cooldown_until
        if cooldown > time.monotonic():
            time.sleep(cooldown - time.monotonic())
        return True

    def _register_pushback(self, response: urllib.error.HTTPError) -> None:
        retry_after = 60
        try:
            retry_after = max(1, min(300, int(response.headers.get("Retry-After", 60))))
        except (TypeError, ValueError):
            pass
        with self._state_lock:
            self._pushback_hits += 1
            self._cooldown_until = time.monotonic() + retry_after
            abort = self._pushback_hits >= THROTTLE_ABORT_HITS
            if abort:
                self.aborted = True
        if not self.quiet:
            reason = "pausing all remote fetches" if abort else f"cooling down {retry_after}s"
            print(f"  [remote] rate limited by GitHub (HTTP {response.code}): {reason}", file=sys.stderr)

    def fetch(self, repo_path: str, ref: str, filename: str) -> RemoteHit | None:
        url = f"https://raw.githubusercontent.com/{repo_path}/{ref}/{filename}"
        hit_file = self._cache_file(url, "hit")
        if hit_file is not None and hit_file.exists():
            return RemoteHit(filename, hit_file.read_text(encoding="utf-8", errors="replace"), ref, url)
        miss_file = self._cache_file(url, "miss")
        if miss_file is not None and miss_file.exists():
            return None
        if not self._wait_or_skip():
            return None
        self.throttler.acquire()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                self._register_pushback(exc)
            elif exc.code == 404:
                _atomic_store(miss_file, b"")
            else:
                print(f"  [remote] HTTP {exc.code} for {url}", file=sys.stderr)
            return None
        except (urllib.error.URLError, OSError) as exc:
            print(f"  [remote] connection error for {url}: {exc}", file=sys.stderr)
            return None
        content = normalize_text(content)
        _atomic_store(hit_file, content.encode("utf-8"))
        return RemoteHit(filename, content, ref, url)

    def default_branch(self, repo_path: str) -> str | None:
        """Resolve a repo's default branch via the GitHub API (cached, token-optional)."""
        cache_key = f"api:default-branch:{repo_path}"
        hit_file = self._cache_file(cache_key, "hit")
        if hit_file is not None and hit_file.exists():
            return hit_file.read_text(encoding="utf-8").strip() or None
        miss_file = self._cache_file(cache_key, "miss")
        if miss_file is not None and miss_file.exists():
            return None
        if not self._wait_or_skip():
            return None
        self.throttler.acquire()
        headers = {"User-Agent": USER_AGENT}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            request = urllib.request.Request(f"https://api.github.com/repos/{repo_path}", headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                branch = json.loads(response.read().decode("utf-8")).get("default_branch")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if not self.quiet:
                print(f"  [remote] default-branch lookup failed for {repo_path}: {exc}", file=sys.stderr)
            _atomic_store(miss_file, b"")
            return None
        _atomic_store(hit_file, (branch or "").encode("utf-8"))
        return branch or None

    def fetch_package(self, pkg: Package, mode: str = "full") -> list[RemoteHit]:
        """Fetch remote license/notice files. mode: "full" or "notice" (audit probe)."""
        repo_path = github_repo_path(pkg.repo_url)
        if repo_path is None:
            return []
        refs = refs_for(pkg.name, pkg.version)
        candidates = NOTICE_CANDIDATES if mode == "notice" else prune_candidates(pkg.license_expr)
        hits = self._collect_hits(repo_path, refs, candidates)
        if not hits and mode == "full":
            # Branch-only NOTICE audits must not chase arbitrary default branches
            # (version-drift risk); full recovery of license-less crates may.
            default = self.default_branch(repo_path)
            if default and default not in refs:
                hits = self._collect_hits(repo_path, [default], candidates)
        return hits

    def _collect_hits(self, repo_path: str, refs: list[str], candidates: tuple[str, ...]) -> list[RemoteHit]:
        hits: list[RemoteHit] = []
        generic_found = False
        for filename in candidates:
            is_generic = filename in GENERIC_LICENSE_NAMES
            if is_generic and generic_found:
                continue
            for ref in refs:
                hit = self.fetch(repo_path, ref, filename)
                if hit is None:
                    continue
                hits.append(hit)
                if is_generic:
                    generic_found = True
                if candidates is NOTICE_CANDIDATES:
                    return hits  # audit probe: one NOTICE is enough
                break
        return hits


def _atomic_store(path: Path | None, payload: bytes) -> None:
    if path is None:
        return
    handle = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    try:
        handle.write(payload)
        handle.close()
        os.replace(handle.name, path)
    except OSError:
        handle.close()


def collect_remote_entries(
    jobs: list[tuple[Package, str]], fetcher: HttpFetcher, workers: int
) -> dict[str, list[RemoteHit]]:
    """Run (package, mode) fetch jobs in parallel; results keyed by 'name version'."""
    results: dict[str, list[RemoteHit]] = {}
    if not jobs:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetcher.fetch_package, pkg, mode): f"{pkg.name} {pkg.version}"
            for pkg, mode in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
    return results


# --------------------------------------------------------------------------
# Report construction
# --------------------------------------------------------------------------


def build_entries(
    packages: list[Package],
    local_files: dict[str, list[tuple[str, str]]],
    remote_hits: dict[str, list[RemoteHit]],
    catalog: LicenseCatalog,
    remote_paused: bool = False,
) -> list[PackageEntry]:
    entries: list[PackageEntry] = []
    for pkg in packages:
        key = f"{pkg.name} {pkg.version}"
        entry = PackageEntry(package=pkg, origin="missing")
        if pkg.vendor_dir is None:
            entry.missing_reason = "not present in vendor dir (path or git dependency)"
            entries.append(entry)
            continue

        for rel, text in local_files.get(key, []):
            entry.files.append(FileRef(display=rel, catalog_id=catalog.add(text, pkg)))
        if entry.files:
            entry.origin = "local"
            for hit in remote_hits.get(key, []):  # NOTICE audit additions
                entry.files.append(
                    FileRef(
                        display=hit.filename,
                        catalog_id=catalog.add(hit.content, pkg),
                        remote_url=hit.url,
                        remote_ref=hit.ref,
                    )
                )
                if not hit.from_branch:
                    entry.notice_origin = "remote-tag"
                elif not entry.notice_origin:
                    entry.notice_origin = "remote-branch"
            entries.append(entry)
            continue

        hits = remote_hits.get(key, [])
        for hit in hits:
            entry.files.append(
                FileRef(
                    display=hit.filename,
                    catalog_id=catalog.add(hit.content, pkg),
                    remote_url=hit.url,
                    remote_ref=hit.ref,
                )
            )
        if entry.files:
            entry.origin = "remote-tag" if any(not h.from_branch for h in hits) else "remote-branch"
            entries.append(entry)
            continue

        if github_repo_path(pkg.repo_url) is None and not pkg.repo_url:
            entry.missing_reason = "no repository URL in Cargo.toml metadata"
        elif github_repo_path(pkg.repo_url) is None:
            entry.missing_reason = f"repository is not hosted on GitHub ({pkg.repo_url})"
        else:
            entry.missing_reason = "no license/notice file found locally or on GitHub"
            if remote_paused:
                entry.missing_reason = "remote fetch paused: rate limited by GitHub (rerun to retry)"
        entries.append(entry)
    return entries


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------


def render_report(
    entries: list[PackageEntry], catalog: LicenseCatalog, project: str, fmt: str
) -> str:
    if fmt == "html":
        return _render_html_report(entries, catalog, project)
    return _render_text_report(entries, catalog, project)


ANCHOR_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _anchor_for(pkg: Package) -> str:
    return "pkg-" + ANCHOR_INVALID_RE.sub("-", f"{pkg.name}-{pkg.version}")


def _source_label(entry: PackageEntry) -> str:
    labels = {
        "local": "local",
        "remote-tag": "remote (tag)",
        "remote-branch": "remote (branch) [!]",
        "missing": "missing",
    }
    label = labels[entry.origin]
    if entry.notice_origin:
        audit = "NOTICE: remote (tag)" if entry.notice_origin == "remote-tag" else "NOTICE: remote (branch) [!]"
        label += f" + {audit}"
    return label


def _render_html_report(entries: list[PackageEntry], catalog: LicenseCatalog, project: str) -> str:
    esc = html.escape
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    local_n = sum(1 for e in entries if e.origin == "local")
    remote_n = sum(1 for e in entries if e.origin in ("remote-tag", "remote-branch"))
    audit_n = sum(1 for e in entries if e.notice_origin)
    missing = [e for e in entries if e.origin == "missing"]

    out = io.StringIO()
    out.write("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
    out.write(f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n")
    out.write(f"<title>Third-Party Notices &amp; Licenses — {esc(project)}</title>\n")
    out.write(f"<style>{_HTML_CSS}</style>\n</head>\n<body>\n")
    out.write(f"<h1>Third-Party Notices and Licenses</h1>\n<p class=\"meta\">Project: <strong>{esc(project)}</strong>"
              f" &middot; Generated: {esc(generated)}</p>\n")
    out.write(f"<p class=\"meta\">{len(entries)} packages (license text: {local_n} local, {remote_n} fetched from remote; "
              f"NOTICE audit recovered {audit_n} crate-package-missing NOTICE file(s); {len(missing)} without any text) &middot; "
              f"{len(catalog.entries)} unique license texts, deduplicated in the <a href=\"#catalog\">License Catalog</a></p>\n")

    toc = [
        "<nav class=\"toc\">\n<strong>Contents</strong>\n<ul>",
        "<li><a href=\"#summary\">Summary</a></li>",
        "<li><a href=\"#packages\">Packages</a></li>",
        "<li><a href=\"#catalog\">License Catalog</a></li>",
    ]
    if missing:
        toc.append("<li><a href=\"#missing\">Packages without any license/notice text</a></li>")
    toc.append("</ul>\n</nav>\n")
    out.write("\n".join(toc) + "\n")

    out.write("<h2 id=\"summary\">Summary</h2>\n<table>\n<thead><tr>"
              "<th>Package</th><th>Version</th><th>License (Cargo.toml)</th><th>Source</th></tr></thead>\n<tbody>\n")
    for entry in entries:
        pkg = entry.package
        label = _source_label(entry)
        cls = ""
        if "[!]" in label:
            cls = ' class="warn"'
        elif entry.origin == "missing":
            cls = ' class="err"'
        out.write(
            f"<tr><td><a href=\"#{_anchor_for(pkg)}\">{esc(pkg.name)}</a></td>"
            f"<td>{esc(pkg.version)}</td><td>{esc(pkg.license_expr or pkg.license_file or 'unspecified')}</td>"
            f"<td{cls}>{esc(label)}</td></tr>\n"
        )
    out.write("</tbody>\n</table>\n")
    out.write("<p class=\"meta\"><span class=\"warn\">[!]</span> = fetched from a branch ref: "
              "verify it matches the released version.</p>\n")

    out.write("<h2 id=\"packages\">Packages</h2>\n")
    for entry in entries:
        pkg = entry.package
        out.write(f"<article class=\"pkg\" id=\"{_anchor_for(pkg)}\">\n<h3>{esc(pkg.name)} v{esc(pkg.version)} "
                  f"<a class=\"up\" href=\"#summary\">&#8679; summary</a></h3>\n")
        metas = []
        if pkg.license_expr:
            metas.append(f"License (Cargo.toml): <code>{esc(pkg.license_expr)}</code>")
        if pkg.license_file:
            metas.append(f"License file (Cargo.toml): <code>{esc(pkg.license_file)}</code>")
        if pkg.repo_url:
            metas.append(f"Repository: <a href=\"{esc(pkg.repo_url)}\">{esc(pkg.repo_url)}</a>")
        if metas:
            out.write("<p class=\"meta\">" + " &middot; ".join(metas) + "</p>\n")
        if entry.files:
            out.write("<ul class=\"files\">\n")
            for ref in entry.files:
                line = f"<li><code>{esc(ref.display)}</code> &rarr; <a href=\"#{ref.catalog_id}\">[{ref.catalog_id}]</a>"
                if ref.remote_url:
                    line += f" <span class=\"meta\">(fetched: <a href=\"{esc(ref.remote_url)}\">{esc(ref.remote_url)}</a>"
                    if ref.remote_ref in BRANCH_REFS:
                        line += " <span class=\"warn\">[!] branch ref: verify against released version</span>"
                    line += ")</span>"
                out.write(line + "</li>\n")
            out.write("</ul>\n")
        else:
            out.write(f"<p class=\"err\">No license/notice text available: {esc(entry.missing_reason)}</p>\n")
        out.write("</article>\n")

    out.write("<h2 id=\"catalog\">License Catalog</h2>\n<p class=\"meta\">Each unique license/notice text appears once.</p>\n")
    for cat_entry in catalog.entries:
        out.write(f"<section class=\"license\" id=\"{cat_entry.cid}\">")
        out.write(f"<h3>[{cat_entry.cid}] {esc(cat_entry.title)}</h3>")
        out.write(f"<p class=\"meta\">Referenced by {len(catalog.package_keys[cat_entry.cid])} package(s), "
                  f"{catalog.file_counts[cat_entry.cid]} file(s)</p>")
        out.write(f"<pre>{esc(cat_entry.text)}</pre></section>\n")

    if missing:
        out.write(f"<h2 id=\"missing\">Packages without any license/notice text ({len(missing)})</h2>\n<ul>\n")
        for entry in missing:
            pkg = entry.package
            out.write(f"<li class=\"err\"><a href=\"#{_anchor_for(pkg)}\">{esc(pkg.name)} v{esc(pkg.version)}</a> "
                      f"({esc(pkg.license_expr or 'license unspecified')}) — {esc(entry.missing_reason)}"
                      + (f" — repo: <a href=\"{esc(pkg.repo_url)}\">{esc(pkg.repo_url)}</a>" if pkg.repo_url else "")
                      + "</li>\n")
        out.write("</ul>\n")

    out.write(DISCLAIMER_HTML)
    out.write("</body>\n</html>\n")
    return out.getvalue()


_HTML_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 1100px;
       margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { border-bottom: 3px solid currentColor; padding-bottom: .3em; }
h2 { margin-top: 2.5em; border-bottom: 1px solid currentColor; padding-bottom: .2em; }
h3 { margin: 1.2em 0 .3em; }
.meta { color: color-mix(in srgb, currentColor 65%, transparent); font-size: .92em; }
nav.toc { border: 1px solid color-mix(in srgb, currentColor 25%, transparent); border-radius: 8px;
          padding: .6em 1em; margin: 1.2em 0; }
nav.toc ul { display: flex; flex-wrap: wrap; gap: .4em 2em; list-style: none; margin: .4em 0 0; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: .93em; }
th, td { text-align: left; padding: .25em .6em; border-bottom: 1px solid color-mix(in srgb, currentColor 20%, transparent); }
th { border-bottom: 2px solid currentColor; }
pre { background: color-mix(in srgb, currentColor 7%, transparent); padding: 1em; overflow: auto;
      border-radius: 6px; white-space: pre-wrap; font-size: .85em; }
section.license { border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
                  border-radius: 8px; padding: 0 1em; margin: 1em 0; }
article.pkg { margin: 1.6em 0; }
ul.files { margin: .2em 0; }
code { background: color-mix(in srgb, currentColor 7%, transparent); padding: 0 .25em; border-radius: 4px; }
.warn { color: #b45309; font-weight: 600; }
.err  { color: #b91c1c; font-weight: 600; }
.up { font-size: .7em; font-weight: normal; vertical-align: middle; }
@media print { .up { display: none; } body { max-width: none; } }
"""


def _render_text_report(entries: list[PackageEntry], catalog: LicenseCatalog, project: str) -> str:
    out = io.StringIO()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    remote_count = sum(1 for e in entries if e.origin in ("remote-tag", "remote-branch"))
    missing = [e for e in entries if e.origin == "missing"]

    out.write(f"{HEAVY}\nTHIRD-PARTY NOTICES AND LICENSES\n{HEAVY}\n\n")
    out.write(f"Project:   {project}\n")
    out.write(f"Generated: {generated}\n")
    out.write(f"Packages:  {len(entries)} (local: {sum(1 for e in entries if e.origin == 'local')}, ")
    out.write(f"fetched from remote: {remote_count}, missing: {len(missing)})\n")
    out.write(f"License texts: {len(catalog.entries)} unique (deduplicated in the catalog below)\n\n")

    _render_summary_table(out, entries)
    _render_catalog(out, catalog)
    _render_packages(out, entries)
    _render_missing(out, missing)
    out.write(f"{LINE}\n{DISCLAIMER_TEXT}\n")
    return out.getvalue()


def _render_summary_table(out: io.StringIO, entries: list[PackageEntry]) -> None:
    headers = ("Package", "Version", "License (Cargo.toml)", "Source")
    rows = [
        (
            e.package.name,
            e.package.version,
            e.package.license_expr or e.package.license_file or "unspecified",
            _source_label(e),
        )
        for e in entries
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    out.write(f"{LINE}\nSUMMARY\n{LINE}\n\n")
    out.write("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip() + "\n")
    for row in rows:
        out.write("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() + "\n")
    out.write("\n  [!] fetched from a branch ref: verify it matches the released version\n\n")


def _render_catalog(out: io.StringIO, catalog: LicenseCatalog) -> None:
    out.write(f"{LINE}\nLICENSE CATALOG (each unique text appears once)\n{LINE}\n\n")
    for entry in catalog.entries:
        packages = catalog.package_keys[entry.cid]
        out.write(f"[{entry.cid}] {entry.title}\n")
        out.write(f"Referenced by {len(packages)} package(s), {catalog.file_counts[entry.cid]} file(s)\n\n")
        out.write(entry.text)
        out.write("\n")


def _render_packages(out: io.StringIO, entries: list[PackageEntry]) -> None:
    out.write(f"{LINE}\nPACKAGES\n{LINE}\n\n")
    for entry in entries:
        pkg = entry.package
        out.write(f"Package: {pkg.name} v{pkg.version}\n")
        if pkg.license_expr:
            out.write(f"License (Cargo.toml): {pkg.license_expr}\n")
        if pkg.license_file:
            out.write(f"License file (Cargo.toml): {pkg.license_file}\n")
        if pkg.repo_url:
            out.write(f"Repository: {pkg.repo_url}\n")
        if entry.files:
            for ref in entry.files:
                line = f"  {ref.display} -> [{ref.catalog_id}]"
                if ref.remote_url:
                    line += f"  (fetched: {ref.remote_url})"
                    if ref.remote_ref in BRANCH_REFS:
                        line += "  [!] branch ref: verify against released version"
                out.write(line + "\n")
        else:
            out.write(f"  [NO LICENSE/NOTICE TEXT AVAILABLE: {entry.missing_reason}]\n")
        out.write("\n")


def _render_missing(out: io.StringIO, missing: list[PackageEntry]) -> None:
    if not missing:
        return
    out.write(f"{LINE}\nPACKAGES WITHOUT ANY LICENSE/NOTICE TEXT ({len(missing)})\n{LINE}\n\n")
    for entry in missing:
        pkg = entry.package
        out.write(f"  {pkg.name} v{pkg.version} ({pkg.license_expr or 'license unspecified'})\n")
        out.write(f"    reason: {entry.missing_reason}\n")
        if pkg.repo_url:
            out.write(f"    repo: {pkg.repo_url}\n")
    out.write("\n")


def write_atomically(output_path: Path, report: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent, suffix=".tmp", delete=False
    )
    try:
        handle.write(report)
        handle.close()
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, output_path)
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a deduplicated third-party NOTICE/LICENSE report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="report file to write")
    parser.add_argument("--format", choices=("html", "txt"), default="html", help="report format")
    parser.add_argument("--project", default=None, help="project name shown in the report")
    parser.add_argument("--manifest-path", default=None, help="path to Cargo.toml to inspect")
    parser.add_argument("--vendor-dir", default=DEFAULT_VENDOR_DIR, help="cargo vendor target dir")
    parser.add_argument("--skip-vendor", action="store_true", help="reuse an existing vendor dir")
    parser.add_argument("--offline", action="store_true", help="disable the remote fetch fallback")
    parser.add_argument(
        "--no-notice-audit",
        action="store_true",
        help="skip the remote NOTICE probe for Apache-family crates lacking a local NOTICE",
    )
    parser.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="exit with status 1 if any crate has no license/notice text",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel remote jobs")
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE_PER_SECOND,
        help="global HTTP requests per second (0 disables throttling)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout per request")
    parser.add_argument("--cache-dir", default=None, help="directory to cache remote hits and 404s")
    parser.add_argument("--quiet", action="store_true", help="suppress per-package progress output")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.rate < 0:
        parser.error("--rate must be >= 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    vendor_dir = Path(args.vendor_dir)
    output_path = Path(args.output)
    quiet = args.quiet

    def log(message: str) -> None:
        if not quiet:
            print(message)

    require_cargo(args.skip_vendor)
    if args.skip_vendor:
        if not vendor_dir.is_dir():
            sys.exit(f"error: --skip-vendor given but vendor dir '{vendor_dir}' does not exist.")
        vendored_here = False
    else:
        log(f"{HEAVY}\n1. Running 'cargo vendor' into {vendor_dir}/\n{HEAVY}")
        run_cargo_vendor(vendor_dir, quiet, args.manifest_path)
        vendored_here = True

    try:
        log(f"\n{HEAVY}\n2. Reading cargo metadata\n{HEAVY}")
        packages, default_project = load_packages(vendor_dir, quiet, args.manifest_path)
        log(f"Third-party packages to process: {len(packages)}")

        log(f"\n{HEAVY}\n3. Collecting license/notice files from vendored crates\n{HEAVY}")
        local_files: dict[str, list[tuple[str, str]]] = {}
        need_license: list[Package] = []
        need_notice: list[Package] = []
        for idx, pkg in enumerate(packages, 1):
            key = f"{pkg.name} {pkg.version}"
            if pkg.vendor_dir is None:
                continue
            files = find_local_files(pkg.vendor_dir)
            local_files[key] = files
            has_notice = any("NOTICE" in rel.upper() for rel, _ in files)
            if github_repo_path(pkg.repo_url):
                if not files:
                    need_license.append(pkg)
                elif not has_notice and "apache" in pkg.license_expr.lower() and not args.no_notice_audit:
                    need_notice.append(pkg)
            if not quiet and idx % 50 == 0:
                log(f"  scanned {idx}/{len(packages)}")
        log(f"Crates without any local license text needing remote fetch: {len(need_license)}")
        log(f"Apache-family crates without a local NOTICE (audit target): {len(need_notice)}")

        remote_hits: dict[str, list[RemoteHit]] = {}
        remote_paused = False
        jobs: list[tuple[Package, str]] = [(p, "full") for p in need_license]
        jobs += [(p, "notice") for p in need_notice]
        if jobs and not args.offline:
            rate_text = f"{args.rate:g} req/s" if args.rate > 0 else "unthrottled"
            log(f"\n{HEAVY}\n4. Parallel remote fetch ({args.workers} workers, {rate_text}, {len(jobs)} job(s))\n{HEAVY}")
            fetcher = HttpFetcher(
                Path(args.cache_dir) if args.cache_dir else None, args.timeout, quiet, args.rate
            )
            remote_hits = collect_remote_entries(jobs, fetcher, args.workers)
            remote_paused = fetcher.aborted
            license_recovered = sum(1 for p in need_license if remote_hits.get(f"{p.name} {p.version}"))
            notice_recovered = sum(1 for p in need_notice if remote_hits.get(f"{p.name} {p.version}"))
            log(
                f"Recovered license text for {license_recovered}/{len(need_license)} crate(s); "
                f"NOTICE for {notice_recovered}/{len(need_notice)} audited crate(s)"
            )
            if remote_paused:
                log("warning: GitHub rate-limited this run; remote fetching was paused early.")
        elif jobs:
            log("\n[offline] skipping remote fallback and NOTICE audit")

        catalog = LicenseCatalog()
        entries = build_entries(packages, local_files, remote_hits, catalog, remote_paused)

        log(f"\n{HEAVY}\n5. Writing report -> {output_path}\n{HEAVY}")
        project = args.project or default_project
        write_atomically(output_path, render_report(entries, catalog, project, args.format))
    finally:
        if vendored_here:
            shutil.rmtree(vendor_dir, ignore_errors=True)

    total = len(entries)
    missing_count = sum(1 for e in entries if e.origin == "missing")
    log(f"\nDone: {total} packages, {len(catalog.entries)} unique license texts, {missing_count} missing.")
    log(f"Report written to '{output_path}'.")
    if missing_count and args.fail_on_missing:
        log(f"error: {missing_count} package(s) without any license/notice text (--fail-on-missing)")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("interrupted")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: command failed with exit code {exc.returncode}: {exc.cmd}")
