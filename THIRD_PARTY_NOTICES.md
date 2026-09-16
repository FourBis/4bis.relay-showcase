# Third-party notices

Relay's own code uses [MIT](LICENSE). Third-party code retains its original
licenses. Full copyright and license notices for the distributed JavaScript
components and Tailwind CSS are in
[npm-LICENSES.txt](third_party_licenses/npm-LICENSES.txt).

[npm-components.json](third_party_licenses/npm-components.json) records all 82
package/version combinations, official npm tarball URLs, SHA-512 integrity and
license-file hashes. Mermaid's bundled components were identified from the
33 nested source maps and two esbuild metafiles, including scoped packages and multiple
versions of the same dependency. Fastdom's license is reproduced from its
official package README; Khroma's MIT license is in its `license` file even
though its npm metadata omits the license field.

| Distributed component | Version | License |
| --- | --- | --- |
| marked | 18.0.7 | MIT |
| Mermaid | 11.17.2, Relay rebuild 1 | MIT, plus bundled dependency notices |
| DOMPurify, standalone and inside Mermaid | 3.4.15 | MPL-2.0 OR Apache-2.0 |
| Tailwind CSS, generated stylesheet | 3.4.17 | MIT |

The embedded path-browserify source was matched exactly to version 1.0.1.

Python dependencies are installed separately from PyPI rather than vendored.
Their own distributions provide their respective license notices.

## Mermaid security rebuild

The stock Mermaid 11.17.2 bundle includes older DOMPurify and js-yaml versions.
The Relay artifact is built from the official
[mermaid@11.17.2 tag](https://github.com/mermaid-js/mermaid/tree/mermaid%4011.17.2),
commit `dcb694ddb58dc5ad3502e7e903cac05fd812eac3`, with DOMPurify 3.4.15 and
js-yaml 4.3.2 and lodash-es 4.18.1. The exact package/lockfile changes are preserved in
[mermaid-security.patch](third_party_licenses/mermaid-security.patch).
No Mermaid source files were modified. Rendering uses `securityLevel: strict`.

Build provenance: Windows, Node 22.12.0, pnpm 10.30.3 and the upstream esbuild
script. To reproduce, clone the tag into a separate directory, verify the commit
above, apply the patch, and run:

```powershell
# Inside the separate Mermaid checkout, with pnpm 10.30.3 available:
git apply C:/path/to/relay/third_party_licenses/mermaid-security.patch
pnpm install --ignore-scripts --frozen-lockfile
pnpm exec tsx .esbuild/build.ts --mermaid
```

The upstream script also builds other workspace packages. Only
`packages/mermaid/dist/mermaid.min.js` is copied into Relay as
`vendor-mermaid-11.17.2.relay1.min.js`; build dependencies, source maps and
`node_modules` are not distributed. The source map was retained locally for
the component inventory. This optional maintenance build is not required to
install or run Relay.

SHA-256 of the committed vendor files:

| File | SHA-256 |
| --- | --- |
| `vendor-marked-18.0.7.esm.js` | `fd52a4d9a8eb652477ec1ddea2c48677bf46fcfc82e3801e46ca54359c57164f` |
| `vendor-mermaid-11.17.2.relay1.min.js` | `25c5bb39b8995cb5dc6837a5eb25ed69072491b55fb218dea73598741b04700f` |
| `vendor-purify-3.4.15.es.js` | `e7d8182ea0aae9daa46c3294a486067b3f4461bd18f8ca76e499c623e9bda6e3` |

## Advisory review, 2026-09-16

The 81 JavaScript package/version combinations in the distributed artifacts
returned no known advisories from [OSV](https://osv.dev/) on the review date.
This is a component scan, not a proof that the application has no vulnerabilities.
The upstream build workspace still reports advisories in other tooling/package
branches; that workspace is not shipped with Relay.

The rebuild addresses, among others, upstream advisories for
[Mermaid](https://github.com/mermaid-js/mermaid/security/advisories/GHSA-6x64-9x62-f2gx),
[DOMPurify](https://github.com/cure53/DOMPurify/security/advisories/GHSA-55q2-fjhq-7xh7)
and [js-yaml](https://github.com/nodeca/js-yaml/security/advisories/GHSA-2883-xcg3-v3hh).
