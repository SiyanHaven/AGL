# Support

## Where to ask

- **Bug or unexpected compiler behavior**: open an issue with the bug report template.
- **Feature or language-change proposal**: open an issue with the feature request template. For a
  large change, discuss the design before writing code.
- **Security issue**: follow [`SECURITY.md`](SECURITY.md); do not open a public issue.
- **Usage question**: check [`README.md`](README.md), [`docs/入门教程.md`](docs/入门教程.md),
  [`examples/README.md`](examples/README.md) and [`docs/SPEC.zh.md`](docs/SPEC.zh.md) /
  [`docs/SPEC.en.md`](docs/SPEC.en.md) first.

## Before asking

Run these and include their output:

```powershell
python -m compiler.cli --version
python -m compiler.cli doctor
```

For a code problem, reduce it to the smallest `.agl` file that still shows the issue. Include the
source, the exact command, the expected result and the actual result. Remove private paths, tokens
and personal data.

## Scope

Maintainers support the current release line (0.53.x) and the documented language surface. The
project does not provide commercial support, SLA or private consulting. Known limitations are
listed in the specification; a request that assumes an unimplemented sandbox, tool runtime or
package manager is a feature request, not a support case.

## Response expectations

This is a community project. Reports are triaged as time allows; security reports take priority.
A lack of an immediate reply is not a rejection. Please keep issue threads on topic and follow
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
