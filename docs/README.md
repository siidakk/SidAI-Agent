# docs/

The architecture walkthrough, published with GitHub Pages at
**https://siidakk.github.io/SidAI-Agent/**

`index.html` is a single self-contained page: all CSS inline, fonts from
Google Fonts, and diagrams rendered client-side by Mermaid from a CDN.
Nothing to build — edit the file, push, and Pages serves it.

`.nojekyll` stops GitHub running the page through Jekyll, which would
otherwise mangle anything that looks like Liquid template syntax.

`404.html` exists because of one confusing thing about this setup: Pages
serves `docs/` **as the site root**, so this file appears at
`/SidAI-Agent/` — and `/SidAI-Agent/docs/index.html`, which is the URL you
would guess from browsing the repo, is a 404. Rather than explain that to
anyone who trips over it, the 404 page just takes them to the right place.

To publish: repository **Settings → Pages → Source: Deploy from a branch →
main / docs**.
