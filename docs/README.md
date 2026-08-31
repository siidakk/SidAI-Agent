# docs/

The architecture walkthrough, published with GitHub Pages at
**https://siidakk.github.io/SidAI-Agent/**

`index.html` is a single self-contained page: all CSS inline, fonts from
Google Fonts, and diagrams rendered client-side by Mermaid from a CDN.
Nothing to build — edit the file, push, and Pages serves it.

`.nojekyll` stops GitHub running the page through Jekyll, which would
otherwise mangle anything that looks like Liquid template syntax.

To publish: repository **Settings → Pages → Source: Deploy from a branch →
main / docs**.
