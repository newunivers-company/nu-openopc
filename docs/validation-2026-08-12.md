# Validation Report — 2026-08-12

## Result

The recommended hardening sequence was implemented from P0 bootstrap and
repair paths through P2 operational visibility. The shared CLI, slash-command,
WebSocket, service, Office UI, operations, and packaging paths are regression
tested. Destructive retention and fabricated provider evidence remain
explicitly outside the automated flow.

## Delivered in priority order

1. **Custom organization bootstrap** — `opc org saved create` accepts repeated
   compact/JSON members or a YAML/JSON member file, persists and activates the
   organization, and allows a market preset to be applied without replacing
   its identity. A later CLI process restores both the organization and its
   active-list marker.
2. **Initialization repair** — initialization distinguishes uninitialized,
   partial, and initialized homes. `opc init --repair` fills only missing
   required templates, preserves existing bytes, and refuses invalid YAML.
3. **Cross-surface contracts** — CLI, interactive slash commands, WebSocket
   handlers, and Office services are pinned to shared organization and market
   service calls.
4. **Office load isolation** — Phaser and the Office component are loaded only
   after the first Office visit. Revisiting wakes the retained scene without
   downloading either chunk again.
5. **Provider evidence operations** — campaign scaffold/run/record commands
   bind samples to a verified plan digest, force status-only calls, disable
   automatic failure injection, and reject an incomplete drill pack before
   writing any drill evidence.
6. **Environment diagnostics** — `opc doctor` reports initialization,
   filesystem, read-only SQLite integrity, external-agent availability, and
   all channel providers without returning credential values. Command probes
   remain opt-in.
7. **Storage visibility** — Mission Control inventories SQLite databases,
   generated backups, logs, total footprint, large files, and safe retention
   candidates in a worker thread. It emits capacity/retention alerts and an
   explicit command, but never performs automatic cleanup.

## Use-case evidence

- Fresh isolated home: `opc init --yes --no-external-agent-preflight` completed;
  unavailable default Cursor/OpenCode adapters were saved as disabled, and
  `opc doctor --strict --json` reported initialized config plus healthy
  filesystem, SQLite, external-agent, and channel checks.
- Custom organization: a two-member `Research Lab` was created, the 21-role VC
  preset was applied, `org_index.yaml` remained `research_lab`, no preset-named
  organization file was created, and a later `saved list` returned
  `active_name=research_lab`.
- Provider campaign: one genuine status-only sample was persisted with the
  campaign ID and plan digest. The isolated provider was unavailable, so
  readiness correctly remained pending. Untouched drill templates were
  rejected and the database retained zero drill rows.
- Storage: Mission Control returned `dry_run=true`,
  `automatic_cleanup=false`, and `apply_requires_explicit_flag=true`. Retention
  tests verified that changed files, symlinks, manual backups, and paths outside
  the inspected root cannot be deleted.
- Browser: the initial Workspace request set contained no Phaser or PhaserGame
  asset. The first Office visit loaded one of each and displayed the canvas;
  the second visit issued no additional chunk request. The active organization
  ID and its hint render as separate, wrapping lines with a measured 5 px gap.
- Preset response semantics: a four-role preset with no persisted hires
  reported `employees=0`, `persisted_employees=0`, and
  `runtime_default_employees=4`; the saved YAML retained an empty employee list.
- Sandbox: the real bubblewrap FIFO round trip passed both from the normal
  source tree and a release checkout below `/tmp`. The private tmpfs no longer
  hides a temporary workspace, interpreter, or source path.

## Regression evidence

- Remote-pinned release workspace: 2,318 tests passed, 17 skipped, 38 subtests
  passed. After the SIGTERM lifecycle regression test was added, the current
  worktree passed 2,320 tests with the same 17 skips and 38 subtests.
- Ruff: pass. MyPy operations/runtime boundary: pass (36 source files).
- Critical policy coverage gate: 84.74%, above the required 80%.
- Frontend: 28 unit scripts and 25 Vitest tests passed; TypeScript typecheck and
  production build passed.
- Browser E2E: message-list behaviors passed and all 20 execution-panel scroll
  assertions passed. The fixture retries only one observed Vite dependency
  optimizer `ERR_NETWORK_CHANGED`; ordinary page, console, and fixture errors
  still fail.
- Office committed-bundle verification: pass. The built wheel contains a valid
  current bundle. Initial entry is about 370 KB; Phaser remains a separate
  roughly 1.21 MB chunk.
- Outcome benchmark contract: all 12 v1 cases execution-ready.
- Operations regression gate: pass with no violations.
- Golden operations loop: 20 goals, contracts, measured-usage rows,
  scorecards, canaries, and outbox deliveries; every invariant passed.
- Source distribution and wheel: built successfully.

## Periodic isolated review — cycle 2

- A second fresh `OPC_HOME` bootstrapped project `periodic-live`; strict Doctor
  returned `ok=true` with no issues and disabled only unavailable default
  Cursor/OpenCode adapters.
- Four separate CLI processes created `Periodic Review Lab`, loaded it, applied
  the `research-lab` preset, and loaded it again. The active organization ID
  remained `periodic_review_lab`, four roles persisted, no preset-named
  organization was created, and persisted/runtime employee counts remained
  unambiguous at 0/4.
- Headless Chromium observed HTTP 200, two WebSocket connections and two
  Mission Control responses. Project and organization state survived reload;
  Office loaded Phaser only after entry; browser console, page, and failed
  request lists were empty. Screenshots were also reviewed visually.
- The cycle exposed a P1 shutdown defect: Ctrl+C performed full cleanup, while
  SIGTERM terminated before `aiohttp` shutdown hooks. The server now converts
  SIGTERM into its async stop event. A clean follow-up home exited with code 0,
  logged the ordered engine/Office cleanup sequence, released the listener and
  DB handles, left no WAL/SHM sidecars, and returned `quick_check=ok` for every
  database.
- No paid content generation, synthetic failure injection, or automatic file
  deletion was performed.

## Periodic installed-wheel review — cycle 3

- A wheel-only Python 3.13 environment under
  `/tmp/nu-openopc-periodic-3-20260812-alLj9t` installed `opc==0.1.0` together
  with the remote-pinned `nu-llm-routing-lib==0.4.0` and
  `nu-resource-gen-lib==0.2.4`. Imports resolved from the isolated
  `site-packages`, not the source checkout.
- Fresh project `wheel-live` passed strict Doctor with no issues. Separate CLI
  processes created `Wheel Release Lab`, listed it, applied `devops-pipeline`,
  and listed it again. The active organization remained
  `wheel_release_lab`, its four-role runtime default survived, persisted hires
  remained zero, and no preset-named organization file appeared.
- The first installed-wheel browser run exposed an intermittent Chromium
  `ResizeObserver loop completed with undelivered notifications` console
  error. Phaser, message-viewport, and virtualized-session size reactions now
  coalesce layout work onto the next animation frame; the session virtualizer
  is disabled while ordinary rows are rendered. Cleanup also cancels every
  pending frame.
- The rebuilt production bundle and wheel passed source/wheel bundle
  verification. Ten independent Chromium runs then each returned HTTP 200,
  preserved the project and organization across reload, lazy-loaded Phaser
  only after Office entry, rendered Mission Control, opened two WebSockets,
  and received two Mission Control frames. Across all ten runs there were zero
  console errors, page errors, or failed requests.
- The final installed-wheel Office server received SIGTERM, completed both
  engine shutdown paths and the Office shutdown hook, released its listener,
  and exited with code 0. `quick_check` returned `ok` and
  `foreign_key_check` returned no rows for both project databases and the
  Office UI database. Follow-up integrity readers can create zero-byte WAL and
  32 KiB SHM sidecars for WAL-mode databases; these contained no pending data
  and were not produced by the server shutdown itself.
- Focused Office/UI regression passed 72 tests. The final full Python suite
  passed 2,320 tests with 17 skips and 38 subtests; frontend typecheck, 28 unit
  scripts, 25 Vitest tests, and production build all passed.
- The installed runtime logs an informational fail-open fallback when NU LLM
  routing is enabled without an explicit `NU_LLM_ROUTER_CONFIG` or
  `llm.nu_routing.config_path`. Doctor now surfaces the same state before
  runtime without treating an intentional fail-open fallback as a failure.
- No paid content generation, synthetic failure injection, or automatic file
  deletion was performed. All isolated homes, wheels, JSON evidence, and
  screenshots were retained.

## Periodic installed-wheel review — cycle 4

- A third independent Python 3.13 wheel environment under
  `/tmp/nu-openopc-periodic-4-20260812-CSZAl5` installed `opc==0.1.0`,
  `nu-llm-routing-lib==0.4.0`, and `nu-resource-gen-lib==0.2.4`. The OPC import
  and Office assets resolved exclusively from `site-packages`; after the cycle's
  two fixes, the final wheel digest was
  `55a92bf47f643baaf10ce691a548726bd80ff18f14db94cb1b7725d183ae69c3`.
- Fresh project `cycle4-live` passed strict Doctor with no required issues.
  Doctor now reports the installed-wheel NU state explicitly as
  `state=fallback`, `fallback_active=true`, `package_available=true`, plus the
  `NU_LLM_ROUTER_CONFIG` / `llm.nu_routing.config_path` setup notice. This
  expected fail-open state remains strict-ready; fail-closed unavailability is
  separately covered as `blocked` and fails strict diagnostics.
- Four later CLI processes created `Cycle Four Lab`, read it, applied
  `devops-pipeline`, and read it again. The active ID stayed
  `cycle_four_lab`, four roles persisted, employee counts remained 0 persisted
  and 4 runtime-default, and no preset-named organization file was created.
- Five independent Chromium 151 runs, followed by one more run from the final
  rebuilt wheel, each returned HTTP 200 and restored the
  project and organization after reload, deferred Phaser until Office entry,
  opened two WebSockets, and received two Mission Control responses. All six
  runs had zero console errors, page errors, and failed requests. The final
  Office and Mission Control captures were reviewed visually.
- The exact listening Office process received SIGTERM and exited with code 0
  after both engine shutdown sequences and the Office shutdown hook. The port
  was released and no WAL/SHM sidecar remained. Immutable read-only checks of
  both project databases and `ui_state.db` returned `quick_check=ok` and zero
  foreign-key violations without creating new sidecars.
- The cycle also exposed an existing import-order regression: an earlier
  patched `get_opc_home` could leave `OfficeServiceFactory` bound to the stale
  function and make later commands write UI state to the wrong home. The
  factory now resolves and injects the home when each context is constructed;
  both test orders pass, with a dedicated late-bound-home regression test.
- Diagnostics/CLI/factory coverage passed 114 tests in both file orders, and a
  focused routing/Doctor/Office lifecycle group passed 42 tests. Ruff and
  MyPy for the newly changed diagnostic/factory boundaries passed. The final
  full Python suite passed 2,325 tests with 17 skips and 38 subtests.
- No paid content generation, synthetic failure injection, or automatic file
  deletion was performed. The cycle home, wheel, JSON evidence, and screenshots
  were retained.

## Periodic installed-wheel review — cycle 5

- A new Python 3.13.12 environment under
  `/tmp/nu-openopc-periodic-5-20260812-40is8k` installed the OpenOPC wheel
  exclusively from `site-packages`. Its SHA-256 remained
  `55a92bf47f643baaf10ce691a548726bd80ff18f14db94cb1b7725d183ae69c3`,
  and both the source and wheel Office bundles passed the bundle verifier.
- The cycle made the private-package installation boundary explicit:
  `opc[nu]` cannot resolve the two pinned NU packages from the default public
  index. Clean remote checkouts at
  `f0f27187f8e88f0977449be63b670b5c0070f5cc` and
  `1f2b0025c3314e1410fca3db4628a881ab67750b` produced the exact 0.4.0 and
  0.2.4 wheels, their stable-facade compatibility check passed, and a second
  empty venv successfully installed all three wheels in one resolver
  transaction. The README now documents this consumer path and the private
  index alternative.
- Fresh project `cycle5-live` passed strict Doctor with no issues and the
  expected configured-package `fallback` routing state. Separate CLI
  processes created `Cycle Five Lab`, listed it, applied
  `devops-pipeline`, and listed it again. The active ID remained
  `cycle_five_lab`, four roles persisted, the persisted employee count stayed
  zero, four runtime-default employees were reported, and no preset-named
  organization was created.
- Six Chromium 151.0.7922.34 sessions were exercised across the initial
  helper-managed server and an exact-PID server. Every run returned HTTP 200,
  deferred Phaser until Office entry, rendered the Office canvas, restored the
  project and organization after reload, and received two Mission Control
  WebSocket responses. Console errors, page errors, and failed requests were
  all zero; the final Office and Mission Control screenshots were also reviewed
  visually.
- The generic browser helper terminates its shell wrapper rather than the child
  listener, so that helper-owned stop left the default project's WAL/SHM files.
  This was isolated as a harness boundary, not a product shutdown result. The
  browser-exercised listener was then resolved from the socket table and sent
  SIGTERM directly. Both root and delegated engines shut down, the process
  exited with code 0, the port was released, and every WAL/SHM file disappeared
  without manual deletion.
- Immutable read-only checks of `cycle5-live/tasks.db`,
  `default/tasks.db`, and `ui_state.db` returned `quick_check=ok` and zero
  foreign-key violations. Ruff, the 37-file CI MyPy boundary, 28 frontend unit
  scripts, 25 Vitest tests, and the complete Python suite all passed; the latter
  reported 2,325 passed, 17 skipped, and 38 passing subtests.
- No provider/content generation call, synthetic failure injection, or
  automatic file deletion was performed. The cycle home, exact dependency
  checkouts, wheels, JSON evidence, databases, and screenshots were retained.

## Periodic installed-wheel review — cycle 6

- A fourth independent Python 3.13.12 release environment under
  `/tmp/nu-openopc-periodic-6-20260812-Li7umF` rebuilt OpenOPC plus the two NU
  dependencies from the current source and the manifest's exact remote
  commits. The three-wheel install and stable NU facade verification passed,
  and all imports resolved from the isolated `site-packages`.
- Fresh projects `cycle6-live` and `cycle6-alt` both passed strict Doctor
  with no issues and the expected package-present `fallback` routing state.
  Separate CLI processes created `Cycle Six Lab`, created the alternate
  project, listed both projects, applied `devops-pipeline`, and read the
  organization from the alternate project. The active organization remained
  `cycle_six_lab`, four roles and zero persisted employees survived, four
  runtime-default employees were reported, and no preset-named organization
  appeared.
- The final Chromium 151.0.7922.34 run kept two independent browser contexts
  and WebSockets connected at once. Client A stayed scoped to `cycle6-live`
  while client B stayed scoped to `cycle6-alt`; alternating both selectors
  did not change the peer's selection. Their Mission Control response project
  IDs followed each client's switch sequence, both Office canvases and role
  lists rendered, Phaser remained deferred until Office entry, and client A
  continued refreshing after client B closed. HTTP, console, page, and request
  error counts were all zero. The final Office and alternate-project Mission
  Control captures were reviewed visually. One earlier attempt was excluded
  because the harness used an overly strict accessible-name selector; the
  established selector and stronger project-ID assertions passed twice.
- The exact listener PID received SIGTERM after all three project engines
  (`cycle6-live`, `cycle6-alt`, and the initially visited `default`) had
  been exercised. All three engine shutdown sequences completed, the process
  exited with code 0, port 18810 was released, and every WAL/SHM file was
  removed by normal connection closure. Immutable checks of those three
  project databases plus `ui_state.db` returned `quick_check=ok` and zero
  foreign-key violations.
- Cross-cycle comparison exposed a packaging reproducibility gap: identical NU
  wheel members had identical bytes but 175 and 417 ZIP timestamps differed,
  producing different whole-wheel SHA-256 values from the same immutable
  commits. Setting `SOURCE_DATE_EPOCH` to each source commit timestamp made
  two successive builds byte-for-byte identical. CI now rebuilds and compares
  OpenOPC and both NU wheels before accepting the package gates. The final
  deterministic hashes are
  `d08f755b63928338872412698768fd1e5edb4b349e8ee9031520e2238be1c5a5`,
  `578f594007b6ef170d2e645624c1266b2342dcb2510e19750e47fad4b49ac477`,
  and
  `8df8ef5b9020211fc208e2f194a41d6718ece901e32d206a48a25a2289732434`.
  A fresh venv installed those deterministic wheels and re-passed strict Doctor
  and saved-organization retrieval.
- A final post-Doctor hygiene check then exposed a second issue: Doctor's
  nominally read-only `mode=ro` connection recreated an empty WAL plus SHM
  after a clean server shutdown. The connection now uses an URI-encoded
  `mode=ro&immutable=1` URI, closes reliably on both success and error, and
  reports `access=read_only_immutable`. A dedicated WAL-mode regression test
  and the rebuilt installed wheel both returned `quick_check=ok` without
  creating either sidecar.
- The concurrency/shutdown/diagnostic Python group passed 319 tests and three
  subtests; the release-package group passed nine tests. The final complete
  Python suite passed 2,327 tests with 17 skips and 38 passing subtests. Ruff,
  the 37-file CI MyPy boundary, 28 frontend unit scripts, and 25 Vitest tests
  also passed.
- No provider/content generation call, synthetic failure injection, automatic
  file deletion, or retention apply was performed. The cycle home, checkouts,
  original and deterministic wheels, databases, browser JSON, and screenshots
  were retained.

## Periodic installed-wheel review — cycle 7

- A fifth independent Python 3.13.12 environment under
  `/tmp/nu-openopc-periodic-7-20260812-haCihM` rebuilt OpenOPC twice plus the
  two pinned NU dependencies from clean sources. The OpenOPC builds were
  byte-identical, all three wheels installed in a fresh venv, the committed
  Office bundle and NU stable facades passed, and their SHA-256 values remained
  `d08f755b63928338872412698768fd1e5edb4b349e8ee9031520e2238be1c5a5`,
  `578f594007b6ef170d2e645624c1266b2342dcb2510e19750e47fad4b49ac477`,
  and
  `8df8ef5b9020211fc208e2f194a41d6718ece901e32d206a48a25a2289732434`.
- Fresh CLI processes bootstrapped `cycle7-live`, created `cycle7-offline`,
  created and recovered `Cycle Seven Lab`, and applied `devops-pipeline`.
  Four roles, zero persisted employees, four runtime-default employees, and
  the active `cycle_seven_lab` ID survived cross-project reads. Applying the
  preset did not create a preset-named saved organization. Strict Doctor
  repeatedly passed with package-present fallback routing and no issues.
- Doctor was also run against the offline project before, during, and after the
  live UI session. Its database checks consistently reported
  `access=read_only_immutable`; database bytes and mtime remained unchanged,
  and no WAL/SHM sidecar was created.
- Chromium 151.0.7922.34 kept a real Office WebSocket connected while the exact
  port-18820 listener PID received SIGTERM. The client observed one WebSocket
  close, the already-loaded page remained renderable, Mission Control had
  delivered two frames, Phaser stayed deferred until Office entry, and the
  browser reported no console, page, or request errors. Both initialized
  engines shut down, the process exited with code 0, and the port was released.
- An immediate replacement process then bound the same port and a new browser
  recovered `cycle7-live`, `Cycle Seven Lab`, all four roles, the Office
  canvas, and Mission Control. The project selection survived a full browser
  reload, both WebSocket connections completed cleanly, and the second SIGTERM
  shutdown also exited with code 0 and released the port.
- Immutable post-shutdown checks covered `cycle7-live`, `cycle7-offline`,
  `default`, and `ui_state.db`; all four returned `quick_check=ok`, zero
  foreign-key violations, and no remaining WAL/SHM sidecars. The separate
  20-iteration local operations golden-loop database also passed the same
  integrity checks after normal closure.
- The complete Python suite passed 2,327 tests with 17 skips and 38 passing
  subtests. Ruff, the 35-file CI MyPy boundary, 28 frontend unit scripts, 25
  Vitest tests, and `git diff --check` passed. The critical-policy group passed
  32 tests at 84.74% coverage; the 12-case benchmark contract, 20-iteration
  zero-cost local golden loop, scorecard regression gate, and Office bundle
  gate also passed.
- No new blocking regression was found, so no product source was changed in
  this cycle. No paid provider/content generation call, synthetic failure
  injection, automatic file deletion, or retention apply was performed. The
  cycle home, wheels, databases, JSON evidence, and screenshots were retained.

## Periodic installed-wheel review — cycle 8

- A new Python 3.13.12 environment under
  `/tmp/nu-openopc-periodic-8-20260812-f5wFOx` rebuilt the current OpenOPC
  source and installed it with the two pinned NU wheels. The new OpenOPC wheel
  was byte-identical to cycle 7 with SHA-256
  `d08f755b63928338872412698768fd1e5edb4b349e8ee9031520e2238be1c5a5`;
  NU facade and packaged Office bundle verification passed again.
- The fresh home bootstrapped `cycle8-blue`, created `cycle8-green`, and passed
  strict Doctor for both. Independent CLI processes created `Cycle Eight Lab`,
  applied `devops-pipeline`, and recovered it from the other project. Four
  preset roles, zero persisted employees, four runtime-default employees, and
  the `cycle_eight_lab` identity survived. No preset-named saved organization
  appeared.
- Doctor reported `read_only_immutable` for every database check. The three
  existing database files had identical SHA-256, size, and mtime before and
  after parallel Doctor runs, and no WAL/SHM sidecar was introduced.
- Two independent Chromium 151.0.7922.34 contexts connected concurrently and
  stayed scoped to `cycle8-blue` and `cycle8-green`. Both rendered the saved
  organization, four-role graph, deferred Phaser until Office entry, opened
  Office canvases, and received Mission Control responses with the correct
  per-client project ID. HTTP statuses were 200 and there were no initial
  console, page, or request errors.
- With both WebSockets active, the exact server PID received the normal SIGINT
  path. Three initialized project engines closed, the process exited with code
  0, both pages remained renderable, and the port was released. A replacement
  process immediately reused port 18830; both unchanged pages reconnected
  automatically without reload, retained separate project selections, and
  again received project-correct Mission Control data. The second normal
  SIGINT shutdown also exited with code 0.
- Each browser recorded three native `ERR_CONNECTION_REFUSED` messages during
  the operator-side gap after the first server exited and before the
  replacement process was started and listening. These matched the client's
  bounded 2/4/8-second exponential retry path; both clients recovered on the
  next connection, with no page error, failed HTTP request, state loss, or
  retry storm. This is classified as expected restart-window telemetry rather
  than a blocking regression. Cycle 9 separately measures an automated
  immediate replacement without that orchestration gap.
- Immutable final checks covered `cycle8-blue`, `cycle8-green`, `default`, and
  `ui_state.db`; every DB returned `quick_check=ok`, zero foreign-key
  violations, and no remaining WAL/SHM file. The focused shutdown, Doctor,
  protocol, bundle, and session group passed 219 tests and three subtests. All
  28 frontend unit scripts, 25 Vitest tests, Ruff, the Office bundle verifier,
  and `git diff --check` passed.
- No product source fix was warranted in this cycle. No paid provider/content
  generation call, synthetic failure injection, automatic file deletion, or
  retention apply was performed. All cycle evidence remains retained.

## External and governed follow-up

- The sibling development checkouts remain ahead of their published branches
  and are therefore not release-equivalent. They were left untouched rather
  than silently treating unpublished local commits as immutable release inputs.
- Follow-up on 2026-08-12: immutable release `openopc-nu-2026-08-12` now pins
  the revisions advertised by the two remote `main` refs
  (`nu-llm-routing-lib==0.4.0`, `nu-resource-gen-lib==0.2.4`). The manifest,
  project pins, clean remote checkouts, installed facade, and wheel-only
  compatibility gates target that same reproducible release.
- Production provider readiness still requires the real observation window,
  dense time buckets, fresh samples, and independently evidenced failure drills.
  The automation deliberately does not manufacture those elapsed-time or
  human/independent-authority facts.
- No retention candidate was deleted during validation. Any future cleanup
  requires reviewing the exact dry-run result and separately adding `--apply`.
