# Agent Guidelines for Maintaining a Fork of `vanwild11/viralcut`

This repository is a personal fork (`origin`: `hiez1337/viralcut`) tracking the upstream repository (`upstream`: `vanwild11/viralcut`).

## Branch Rules

- **`main`**: Clean mirror of `upstream/main`. **Never commit custom changes directly to `main`.**
- **`custom`**: Primary working branch containing our personal modifications on top of `main`. Pushes to `origin/custom`.

## How to Implement New Changes (Minimal-Patch Rule)

1. **Isolate custom code**: Prefer putting new features, helpers, scripts, or UI extensions in separate new files/modules instead of heavily rewriting upstream files.
2. **Keep upstream edits tiny**: In existing upstream files, add only minimal hooks, imports, or wiring needed to invoke the isolated modules. This minimizes merge/rebase conflicts when upstream updates.
3. **Document every change**: After completing a modification, add or update an entry in `CUSTOM_CHANGES.md` describing:
   - What was changed and why (behavior / requirements)
   - Which files were added vs. modified
   - How to verify it still works after a rebase
4. **Local-only files**: If creating machine-specific notes or scratch configs that should not be committed even to `custom`, add them to `.git/info/exclude` instead of editing `.gitignore`.

## How to Sync with Upstream Updates

When asked to pull updates from the original developer:

1. Fetch and fast-forward `main` from `upstream` (**never push upstream `v*` tags to `origin`** — upstream's `.github/workflows/build.yml` triggers an automatic upstream release on `v*` tags):
   ```powershell
   git fetch upstream
   git checkout main
   git merge --ff-only upstream/main
   git push origin main
   ```
2. Switch to `custom` and rebase onto the updated `main`:
   ```powershell
   git checkout custom
   git rebase main
   ```
3. **If conflicts occur**:
   - Inspect both our intent (`CUSTOM_CHANGES.md` and the conflicting commit) and upstream's changes (`git diff HEAD..REBASE_HEAD` or `git log -p main -- <file>`).
   - Resolve conflicts semantically (preserving both upstream improvements and our custom behavior), then `git rebase --continue`.
   - If upstream heavily refactored a file and rebasing is messy, check out a fresh branch from `main` and re-apply the spec from `CUSTOM_CHANGES.md` onto the new architecture.
4. Run relevant tests/checks, then force-push `custom` to the personal fork:
   ```powershell
   git push --force-with-lease origin custom
   ```
