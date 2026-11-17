# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.53.x | Yes |
| < 0.53 | No |

This is the first public release candidate. Security fixes are applied to the current minor line.

## Reporting a vulnerability

Please **do not** open a public issue for a suspected vulnerability.

Use the repository's **Security** tab and submit a private security advisory, or contact the
maintainers through a private channel listed in the repository profile. Include:

- AGL version (`python -m compiler.cli --version`) and Python version;
- operating system and architecture;
- the smallest `.agl` source, `agl.toml` or command that reproduces the issue;
- the effect declarations involved (`IO`, `Net`, `model`, `Time`, `External`);
- expected impact: code execution, file access, network access, path traversal, denial of service,
  diagnostic information leak, or another category;
- whether the default self-hosted compiler, `--compiler seed` or `--backend py` was used.

Do not include real credentials, private keys or personal data in a report. Use a minimal
reproduction instead.

## What counts as a security issue

- A way to escape the declared effect model at **compile time** (for example, calling an `IO`
  built-in from a function that does not declare `IO`);
- a way to make the VM perform file, network or external calls outside `--allow-effects`;
- path traversal or arbitrary file write/read through an example, CLI option or build artifact;
- code execution that does not require an explicitly declared effect;
- a diagnostic or log that exposes secrets or private file content;
- a denial of service that cannot be bounded by `--max-steps`, `--timeout-ms` or `--max-calls`;
- a supply-chain issue in the release archive or install path.

## What is not a vulnerability by itself

AGL stage 1 is a **compile-time effect declaration checker**, not an OS-level sandbox. The following
are known limitations, documented in `docs/SPEC.zh.md` / `docs/SPEC.en.md` and
`docs/标准与契约.md` §8.3:

- the VM and Python fallback run in the same OS account as the user;
- a program that declares `IO` may access files the OS account can access;
- `--allow-effects` is an execution policy, not a kernel sandbox;
- there is no network egress filtering, memory quota or per-process isolation;
- the tool-call runtime (`tool` / `工具` blocks) is not implemented;
- repeat-call circuit breaker T19 is design-only.

Reports that demonstrate a concrete impact beyond these documented boundaries are still welcome.

## Disclosure

We aim to acknowledge a report within a few business days, agree on a fix and disclosure timeline
with the reporter, and credit the reporter unless they prefer to remain anonymous. Please allow
time for a fix and a release before public disclosure.
