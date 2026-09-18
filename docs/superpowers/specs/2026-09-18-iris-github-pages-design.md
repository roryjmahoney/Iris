<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris GitHub Pages website design

## Purpose

Create the public front door for Iris: explain the product in seconds, show the
real authentication motion, set honest security expectations, and guide Ubuntu
users to the reviewed installation documentation.

## Visual direction

The site extends Iris's existing interface instead of inventing a separate web
brand. It uses the shipping 48-tick dial, blue scanning and green success
states, quiet neutral surfaces, generous spacing, crisp typography, and soft
depth. It supports light and dark system themes and removes nonessential motion
when `prefers-reduced-motion` is enabled.

## Information architecture

1. Sticky navigation with Features, Security, Install, Documentation, Release,
   and GitHub links.
2. Hero with the product promise, real dial animation, release CTA, and an
   immediate statement that processing stays local.
3. Compatibility strip for Ubuntu 26.04, GNOME 50, Wayland, and AGPL-3.0.
4. Authentication surfaces: GDM, ScreenShield, sudo, and polkit.
5. Architecture diagram showing the narrow PAM client and root-owned daemon.
6. Security section that explains encrypted embeddings, password fallback, and
   liveness limitations without overstating guarantees.
7. Safe installation sequence that never pipes remote content into a root shell.
8. Documentation links, FAQ, and a compact open-source footer.

## Behavior and accessibility

- Semantic landmarks, logical headings, visible focus, skip link, and keyboard
  operability throughout.
- Responsive layouts from small phones through wide desktop displays.
- Theme preference follows the operating system by default and can be toggled.
- Copy buttons give textual feedback through an ARIA live region.
- Reveal effects are progressive enhancement; content remains visible without
  JavaScript, and reduced-motion users receive no sweeping or entrance motion.

## Technical design

The source lives in `site/` as plain HTML, CSS, and small vanilla JavaScript.
`tools/build_site.py` assembles a disposable `_site/` output and copies the two
canonical dial animations from `docs/assets/`. A GitHub Actions workflow runs
the website tests, builds the same output, and deploys it with GitHub's official
Pages actions. There are no web fonts, trackers, cookies, CDNs, or runtime
dependencies.

## Acceptance criteria

- Local desktop and mobile previews are visually coherent and have no browser
  console errors or broken local resources.
- Contract tests validate links, metadata, security copy, accessibility hooks,
  deploy permissions, and the reproducible build.
- The GitHub Pages workflow succeeds and the public URL serves the expected
  release, canonical URL, stylesheet, script, and dial asset.

