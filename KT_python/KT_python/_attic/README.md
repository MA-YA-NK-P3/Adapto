# _attic — superseded iterations from the multi-agent build (not part of the build)

The HTTP layer was built twice in competing shapes during the parallel build:
a single-module `knowledge_tracer/api.py` and a `knowledge_tracer/api/` package.
A Python package shadows a same-named module, so the two could never coexist.
After three rounds (see `PROJECT.md` §11 for the full story), the **package won**:
its final iteration converged to full compatibility with both HTTP test suites
(263 tests green) and ships as `knowledge_tracer/api/`.

What is preserved here, byte-intact:

| Path | What it is |
|---|---|
| `api_package/` | Round-1 version of the package (per-request DI, `create_app(settings)`; missing `/actuator/health` and the correlation-id filter) |
| `test_api_contract.py` | Round-1 contract suite (39 tests, were green against `api_package/`) |
| `api_package_v2/` | Round-2 version (recreated mid-build; incomplete when attic'd) |
| `test_api_contract_v2.py` | Round-2 contract suite |

The single-module `api.py` (the round-1 winner, by the FastAPI-layer agent) was
removed from `knowledge_tracer/` by the api-layer agent's final pass and is **not**
archived here; it is recoverable from that agent's transcript if ever wanted.

Nothing here is imported by the live build. Restoring any of it into
`knowledge_tracer/` will collide with the shipped `api/` package.
