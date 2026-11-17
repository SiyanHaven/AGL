## What changed

<!-- 一两句话说明用户可见的变化，不要只贴文件名。 -->

## Why

<!-- 关联 issue：Fixes #... / Closes #... -->

## Verification

- [ ] `python -m compiler.cli doctor` — 27/27
- [ ] `python examples/run_examples.py` — 6/6
- [ ] `python scripts/verify_spec_snippets.py` — 12/12
- [ ] If the self-hosted compiler changed: `python -m compiler.cli bootstrap` passed
- [ ] If a language keyword/type/effect/built-in changed: `spec/keywords.toml`, both specs, and both branch snippets updated in this PR
- [ ] No generated `.agb`, `.agl.ir`, `build/`, `dist/` or cache files committed

## Behavior and compatibility

<!-- 是否改变关键字、诊断码、产物格式、MCP 工具或版本号？不兼容点是什么？ -->

## Notes for reviewers

<!-- 已知限制、后续工作、需要特别检查的边界。 -->
