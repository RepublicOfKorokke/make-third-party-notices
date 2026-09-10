# make-third-party-notices

One-file Python script that generates a deduplicated third-party LICENSE/NOTICE
report (self-contained HTML or plain text) for Rust/Cargo projects. Reads
license texts from vendored crates as the source of truth, recovers missing
files from GitHub with provenance flags, and audits Apache NOTICEs.
Stdlib only; run with uv.

## Why

If you ship a Rust binary, you probably owe your dependencies an attribution
notice listing their licenses. Doing this honestly means bundling the actual
license/NOTICE _texts_ of the exact versions you compiled — not a summary
table, and not whatever the upstream repo shows today. This script automates
that end-to-end and keeps auditors informed about provenance: where every
text came from and how confident you can be in it.

## How it works

1. `cargo vendor --versioned-dirs` materializes the exact crate sources you
   ship (the same bytes your build uses). Their `LICENSE`/`NOTICE` files are
   authoritative and win over anything else.
2. `cargo metadata` enumerates the active, non-workspace packages.
3. Every LICENSE/NOTICE-family file inside each vendored crate is collected.
4. Crates that ship **no** license text at all (usually because
   `package.include` dropped it) are recovered from GitHub with a parallel
   fetch: version tags are tried first; hits found only on a default branch
   are marked `[!]` in the report because they may not match the release.
   Fetches are throttled (default 5 req/s), respect `Retry-After`, pause
   early under sustained pushback, and are pruned using each crate's license
   expression to avoid hopeless 404s.
5. **NOTICE audit**: crates whose license expression mentions Apache-2.0 but
   whose tarball carries no NOTICE get a targeted remote NOTICE probe.
   Some upstreams (e.g. `aws-lc-rs`) keep the NOTICE at the repository root
   and exclude it from the published crate — Apache-2.0 requires you to ship
   it if it exists.
6. Identical texts are deduplicated into a numbered **License Catalog**; each
   package references catalog entries. The report is written atomically
   (temp file + rename) in self-contained HTML or plain text.

## Requirements

- Rust toolchain with cargo-vendor
  (`cargo install cargo-vendor`)
- [uv](https://docs.astral.sh/uv/) (or any Python >= 3.10)

## Usage

```bash
uv run make_third_party_notices.py --cache-dir .notice_cache
```

From outside the target project:

```bash
uv run make_third_party_notices.py --manifest-path /path/to/Cargo.toml
```

### Options

| Flag                   | Meaning                                                           |
| ---------------------- | ----------------------------------------------------------------- |
| `--output PATH`        | Report path (default `THIRD_PARTY_NOTICES.html`)                  |
| `--format {html,txt}`  | Report format (default `html`, self-contained)                    |
| `--project NAME`       | Name shown in the report (default: cargo workspace root dir name) |
| `--manifest-path PATH` | Cargo manifest to inspect (default: nearest `Cargo.toml`)         |
| `--vendor-dir PATH`    | Vendoring target (default `temp_vendor`, always cleaned up)       |
| `--skip-vendor`        | Reuse an existing vendor dir (fast iteration)                     |
| `--offline`            | Never touch the network                                           |
| `--no-notice-audit`    | Skip the Apache-family remote NOTICE probe                        |
| `--fail-on-missing`    | Exit 1 if any crate has no license text (CI gate)                 |
| `--workers N`          | Parallel remote jobs (default 8)                                  |
| `--rate N`             | Global HTTP requests/second, 0 disables (default 5)               |
| `--timeout SECONDS`    | Per-request HTTP timeout (default 10)                             |
| `--cache-dir PATH`     | Cache remote hits _and 404s_ — re-runs become network-free        |
| `--quiet`              | Suppress per-package progress                                     |

### CI example

```yaml
- run: cargo install cargo-vendor
- run: uv run make_third_party_notices.py --cache-dir .notices-cache --fail-on-missing
- uses: actions/upload-artifact@...
  with:
    name: third-party-notices
    path: THIRD_PARTY_NOTICES.html
```

## Reading the report

- **Source** column: `local` (vendored, authoritative), `remote (tag)`
  (exact-version fetch), `remote (branch) [!]` (branch fetch — verify it
  matches the release), `missing`.
- **License Catalog** collapses duplicate boilerplate; `[C<n>]` ids link
  packages to texts.
- The final section lists crates with no license text anywhere plus the
  reason — those are the ones to review by hand (and to fix upstream).

## How this differs from existing tools

`cargo-about`, `cargo-bundle-licenses`, `cargo-license`, `make-notices` and
friends are worth looking at too. Distinctives here: nothing to configure,
vendored sources as the authority (not crates.io metadata), a built-in
Apache NOTICE audit, explicit per-text provenance with branch-drift
warnings, and a polite, cacheable fetch layer. Known limitation: remote
recovery currently speaks only GitHub raw.

## Disclaimer

**This tool is not a lawyer and generates no legal advice.** It collects and
bundles license/NOTICE texts to support attribution obligations, but whether
those obligations are actually met — for your product, your jurisdiction, your
way of distributing — is a legal question for a qualified professional.
Nothing here guarantees a report is complete or correct: crates can exclude
files from their packages, GitHub repositories can differ from published
releases (the report flags what it could not verify), and several licenses
require judgment calls no script can make.

**No warranty, no liability.** The tool and its output are provided "AS IS",
without warranty of any kind, to the maximum extent permitted by law (Apache-2.0,
Section 7). The author assumes no responsibility for compliance failures,
damages, or legal consequences arising from use of this tool or its output.
Have a human — ideally a lawyer — review generated reports before relying on
them.

## License

Apache-2.0 — see [LICENSE](LICENSE).
