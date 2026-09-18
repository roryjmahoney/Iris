<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris website implementation plan

1. Add failing website contract tests for structure, accessibility, local-only
   assets, build output, and Pages workflow safety.
2. Add a deterministic static-site builder and implement the responsive site in
   `site/`, reusing the canonical dial assets.
3. Run focused tests, the full Iris suite, syntax checks, and link/resource
   validation.
4. Build `_site/`, serve it locally, and inspect desktop and mobile layouts in a
   real browser, correcting visual or console issues.
5. Resolve and pin official GitHub Pages actions, commit the finished website,
   and push the same commit to Gitea and GitHub.
6. Enable workflow-based Pages publishing, wait for the deployment, then verify
   both the GitHub deployment record and the public page in a real browser.

