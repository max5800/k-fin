## [1.35.1](https://github.com/max5800/k-fin/compare/v1.35.0...v1.35.1) (2026-05-10)


### Bug Fixes

* wave-2 stream-z infra bugs (api dockerfile, py 3.14 drift, smoke imports) ([#48](https://github.com/max5800/k-fin/issues/48)) ([2e9f2d5](https://github.com/max5800/k-fin/commit/2e9f2d558ec85b478649390c644a5733de6095e9)), closes [PR#34](https://github.com/PR/issues/34)

# [1.35.0](https://github.com/max5800/k-fin/compare/v1.34.1...v1.35.0) (2026-05-09)


### Features

* **notifier:** Discord webhook for failed sync/agent runs (M11 Stream D) ([#44](https://github.com/max5800/k-fin/issues/44)) ([fab18ee](https://github.com/max5800/k-fin/commit/fab18ee61d6c5b8d208be9806f19fc28e7257ba3))

## [1.34.1](https://github.com/max5800/k-fin/compare/v1.34.0...v1.34.1) (2026-05-09)


### Bug Fixes

* trigger semantic-release for wave-0 critical fixes ([#42](https://github.com/max5800/k-fin/issues/42)) ([b5aa0cf](https://github.com/max5800/k-fin/commit/b5aa0cf7bf0d6a32777f7323be46c289b97bbe2c))

# [1.34.0](https://github.com/max5800/k-fin/compare/v1.33.0...v1.34.0) (2026-05-09)


### Bug Fixes

* **api:** production-clamp dev tools + cross-cutting v1.33 review fixes ([d40e605](https://github.com/max5800/k-fin/commit/d40e605e2b1cbf76c8e4e3288e0128bc6be983f2))
* **tests:** mock get_recent_reports + run_in_fresh_loop in anomaly period tests ([#39](https://github.com/max5800/k-fin/issues/39)) ([9ac7d2d](https://github.com/max5800/k-fin/commit/9ac7d2def7849373048cf0ee912a7f441e626ff5))


### Features

* **agents:** anomaly-agent ohne floor + depot-paging-loop ([#36](https://github.com/max5800/k-fin/issues/36)) ([cc2fa70](https://github.com/max5800/k-fin/commit/cc2fa7075dab40ea4e3b8d9a71c85a88cda12267))
* **api:** tag_ids filter, period param, user page_size setting ([#38](https://github.com/max5800/k-fin/issues/38)) ([7bd1827](https://github.com/max5800/k-fin/commit/7bd1827ae1cee83b9f43101885109ed5ba1acf30))
* **portfolio:** instrument price-history backfill via yfinance (M11) ([#37](https://github.com/max5800/k-fin/issues/37)) ([e183fdd](https://github.com/max5800/k-fin/commit/e183fdd3a83b3fc5572e840206fde2fed4e1b984)), closes [PR#38](https://github.com/PR/issues/38)

# [1.33.0](https://github.com/max5800/k-fin/compare/v1.32.0...v1.33.0) (2026-05-09)


### Features

* **api:** dev-only DB tools — wipe + seed mock dataset ([28d462f](https://github.com/max5800/k-fin/commit/28d462f7eb56ee61a3faeec8820abd9469b87d4f))
* **api:** refund-aware accounting + audit + auto-apply ([930305e](https://github.com/max5800/k-fin/commit/930305e8b541d86233ba5fc84b3d72fd1c18ef62))

# [1.32.0](https://github.com/max5800/k-fin/compare/v1.31.0...v1.32.0) (2026-05-06)


### Features

* **mcp:** allow budget upsert via explicit write allowlist ([#32](https://github.com/max5800/k-fin/issues/32)) ([666b779](https://github.com/max5800/k-fin/commit/666b7794e81373cc1ff50ec7f9d0c2c5d1487587))

# [1.31.0](https://github.com/max5800/k-fin/compare/v1.30.0...v1.31.0) (2026-05-05)


### Features

* **api:** add GET /transactions/export for CSV/JSON download ([8a73ef5](https://github.com/max5800/k-fin/commit/8a73ef5a1ac1ac8e076bccd3bcc6f73c88b2521a))
* **api:** add POST /runs/{id}/rerun for failed agent runs ([0a62cbd](https://github.com/max5800/k-fin/commit/0a62cbd7f70a4688bf4c9d6d8a5c2ae391d2e02f))

# [1.30.0](https://github.com/max5800/k-fin/compare/v1.29.3...v1.30.0) (2026-05-05)


### Features

* **api:** expose GET /sync/runs for the run-history UI ([c12c4bf](https://github.com/max5800/k-fin/commit/c12c4bf42b74678fecbb46849d119cb1567da08e))

## [1.29.3](https://github.com/max5800/k-fin/compare/v1.29.2...v1.29.3) (2026-05-05)


### Bug Fixes

* **security:** equalise login timing for unknown emails ([4be5be6](https://github.com/max5800/k-fin/commit/4be5be69c3e6751c47e52144b104e8742d47a727))

## [1.29.2](https://github.com/max5800/k-fin/compare/v1.29.1...v1.29.2) (2026-05-05)


### Bug Fixes

* **mcp:** fallback to curl for homelab API access ([#31](https://github.com/max5800/k-fin/issues/31)) ([3d86ad7](https://github.com/max5800/k-fin/commit/3d86ad72259b5b3338e28f4f6545000c85d59165))

## [1.29.1](https://github.com/max5800/k-fin/compare/v1.29.0...v1.29.1) (2026-05-03)


### Bug Fixes

* **security:** stop gitleaks false positives on shell interpolation and empty env keys ([5065846](https://github.com/max5800/k-fin/commit/5065846fd485526c805014e2145eba1ab319e85a))

# [1.29.0](https://github.com/max5800/k-fin/compare/v1.28.2...v1.29.0) (2026-05-03)


### Features

* **deploy:** add docker-compose stack for self-hosted use ([f089b0b](https://github.com/max5800/k-fin/commit/f089b0b7a3eca05f27f0fbe565fa418eef1a2033))

## [1.28.2](https://github.com/max5800/k-fin/compare/v1.28.1...v1.28.2) (2026-05-02)


### Bug Fixes

* **aggregates:** honor ?months= param in cashflow-over-time ([3ae1f1d](https://github.com/max5800/k-fin/commit/3ae1f1d804574d2805db36cf6a8c4431306a0f6b))
* **aggregates:** return savings_rate as fraction, not percent ([5e4af3a](https://github.com/max5800/k-fin/commit/5e4af3af9742a74eb2c918f7f4cabee03a491208))

## [1.28.1](https://github.com/max5800/k-fin/compare/v1.28.0...v1.28.1) (2026-05-01)


### Bug Fixes

* **normalize:** COALESCE category_id on upsert to preserve agent/user assignments ([b786af4](https://github.com/max5800/k-fin/commit/b786af46f6afdcda8f63082c3f95a8d0f2d63cd9))

# [1.28.0](https://github.com/max5800/k-fin/compare/v1.27.0...v1.28.0) (2026-05-01)


### Features

* **backfill:** historical transaction backfill (24 months, TAN-in-the-loop) ([41eb235](https://github.com/max5800/k-fin/commit/41eb235985b4d28f3d2263ccfcca27cab8ac90e2))

# [1.27.0](https://github.com/max5800/k-fin/compare/v1.26.1...v1.27.0) (2026-05-01)


### Features

* **reports:** expose agent JSON content to UI + fix synthesis solo run ([013becb](https://github.com/max5800/k-fin/commit/013becbd3075b26645ee76536660cd9a437e99c6))

## [1.26.1](https://github.com/max5800/k-fin/compare/v1.26.0...v1.26.1) (2026-05-01)


### Bug Fixes

* **agents:** surface real cause on batch failure, stop retrying 4xx ([0b1cbe9](https://github.com/max5800/k-fin/commit/0b1cbe97ef6c168afbf9249fa7d860a64ec411c1))

# [1.26.0](https://github.com/max5800/k-fin/compare/v1.25.0...v1.26.0) (2026-05-01)


### Bug Fixes

* **deploy:** bump worker memory to 1Gi + add startupProbe ([405e8d0](https://github.com/max5800/k-fin/commit/405e8d0679a876fb3da2d91fd0f86cde80677e5f))


### Features

* **reliability:** move agent runs to worker, add cancel + heartbeat reaper ([0ea658c](https://github.com/max5800/k-fin/commit/0ea658ca6d08208947b74972a9bea1ea3e154df2))

# [1.25.0](https://github.com/max5800/k-fin/compare/v1.24.2...v1.25.0) (2026-04-30)


### Bug Fixes

* **deploy:** wire JWT_SECRET and bootstrap-user envs into api/migrate ([3175eda](https://github.com/max5800/k-fin/commit/3175eda14e273b757e61fa1fa46cc8eb3f579e36))
* **dev:** make Tilt live_update reliable for the api image ([526084f](https://github.com/max5800/k-fin/commit/526084f82c40def8fb62601495470210c811c808))


### Features

* **auth:** add JWT-based user auth (M10a) ([06e56dc](https://github.com/max5800/k-fin/commit/06e56dc312f19700c5ccd69cd0f313eac3dfb171))

## [1.24.2](https://github.com/max5800/k-fin/compare/v1.24.1...v1.24.2) (2026-04-26)


### Bug Fixes

* **deploy:** keep SEARXNG_URL on public ingress ([591f09e](https://github.com/max5800/k-fin/commit/591f09edfb4941c02af50e52edc3a499a0be278c))

## [1.24.1](https://github.com/max5800/k-fin/compare/v1.24.0...v1.24.1) (2026-04-26)


### Bug Fixes

* **deploy:** point SEARXNG_URL at in-cluster service DNS ([7b03c61](https://github.com/max5800/k-fin/commit/7b03c61b37bb75eaa77db10bfee86d7206ef17ab))

# [1.24.0](https://github.com/max5800/k-fin/compare/v1.23.1...v1.24.0) (2026-04-26)


### Features

* **agents:** wire SearXNG search_web tool into categorization ([0418064](https://github.com/max5800/k-fin/commit/04180646e28cfe8855c35d51940b11314a78b774))

## [1.23.1](https://github.com/max5800/k-fin/compare/v1.23.0...v1.23.1) (2026-04-23)


### Bug Fixes

* **deploy:** bump UI image tag to v1.0.2 ([d0b88c0](https://github.com/max5800/k-fin/commit/d0b88c0a411c20e2d166a96e8f0ef294288a6182))

# [1.23.0](https://github.com/max5800/k-fin/compare/v1.22.0...v1.23.0) (2026-04-23)


### Bug Fixes

* **connector:** cap depot paging at Comdirect's 500 limit ([be87d3a](https://github.com/max5800/k-fin/commit/be87d3add66a63371a3fed2edbe14c1562fb2f29))
* **deploy:** pin UI image tag to v1.0.0 ([8d8db3b](https://github.com/max5800/k-fin/commit/8d8db3b94274618b1468ba38a5d3c73e251fe608))


### Features

* **agents:** few-shot memory for categorization (M7a Phase 1a) ([e7a70a0](https://github.com/max5800/k-fin/commit/e7a70a0de667576bff852c159faa99efd262749a))

# [1.22.0](https://github.com/max5800/k-fin/compare/v1.21.0...v1.22.0) (2026-04-18)


### Features

* **portfolio:** depot persistence, KPI API and UI backend ([55009cc](https://github.com/max5800/k-fin/commit/55009cc376171f6825dd44c6823e5a7b5bb42c3a))

# [1.21.0](https://github.com/max5800/k-fin/compare/v1.20.0...v1.21.0) (2026-04-18)


### Features

* **m7:** agent pipeline with live progress, token cost tracking, Sonnet 4.6 categorization ([721a30c](https://github.com/max5800/k-fin/commit/721a30c972fe7d6d7f477b2d76ec96bba75d506c))

# [1.20.0](https://github.com/max5800/k-fin/compare/v1.19.2...v1.20.0) (2026-04-17)


### Features

* **identity:** anchor k-fin product identity across backend ([e24f5a5](https://github.com/max5800/k-fin/commit/e24f5a5a7290fc55c8fa7dbde77f745acf1011b5))

## [1.19.2](https://github.com/max5800/k-fin/compare/v1.19.1...v1.19.2) (2026-04-17)


### Bug Fixes

* **normalization:** exclude internal transfers from outlier detection ([eaaff0b](https://github.com/max5800/k-fin/commit/eaaff0b7ae8bdb815b2fdf78ab84587b98adfd3d))

## [1.19.1](https://github.com/max5800/k-fin/compare/v1.19.0...v1.19.1) (2026-04-17)


### Bug Fixes

* **chart:** set VITE_ALLOWED_HOSTS for the UI deployment ([0c23869](https://github.com/max5800/k-fin/commit/0c238693bb6f1a9dfb2974ee8c83c0460dae44df))

# [1.19.0](https://github.com/max5800/k-fin/compare/v1.18.3...v1.19.0) (2026-04-17)


### Features

* **normalization:** return run_id from process_and_normalize ([004478a](https://github.com/max5800/k-fin/commit/004478a16573106ed20597cf4c8054007e03543b))

## [1.18.3](https://github.com/max5800/k-fin/compare/v1.18.2...v1.18.3) (2026-04-17)


### Bug Fixes

* **normalization:** switch outlier detection from z-score to MAD ([2b24da0](https://github.com/max5800/k-fin/commit/2b24da0fbe41d4c6fba226c95508b13a3e1f4d8c))

## [1.18.2](https://github.com/max5800/k-fin/compare/v1.18.1...v1.18.2) (2026-04-17)


### Bug Fixes

* **api:** return 400/409 for invalid or duplicate category creation ([c15cfa6](https://github.com/max5800/k-fin/commit/c15cfa6f2e5448e4266cea40fda3ebc2186652d4))

## [1.18.1](https://github.com/max5800/k-fin/compare/v1.18.0...v1.18.1) (2026-04-17)


### Bug Fixes

* **normalization:** insert new raw row before updating superseded_by FK ([1ffb77f](https://github.com/max5800/k-fin/commit/1ffb77ff560a103a2a7267fe3d31433dfa311eff))

# [1.18.0](https://github.com/max5800/k-fin/compare/v1.17.0...v1.18.0) (2026-04-17)


### Bug Fixes

* **api:** add create_app factory so tests can patch env before building the app ([9962b60](https://github.com/max5800/k-fin/commit/9962b60cbe01d0a4775342c761d41e0e3a742bcc))


### Features

* **chart:** integrate k-fin-ui into Tilt dev environment ([b0b6f23](https://github.com/max5800/k-fin/commit/b0b6f231857fbfd600d39d6ce473c9e8ccd26737))

# [1.17.0](https://github.com/max5800/k-fin/compare/v1.16.1...v1.17.0) (2026-04-17)


### Features

* **m8:** add stdio MCP server driven by OpenAPI ([9ba546e](https://github.com/max5800/k-fin/commit/9ba546e23e066faafe5d9fa3b9a605ac5cdc3eb1))

## [1.16.1](https://github.com/max5800/k-fin/compare/v1.16.0...v1.16.1) (2026-04-17)


### Bug Fixes

* **tests:** update currentPrice structure in depot position mapping test ([d7b1bd9](https://github.com/max5800/k-fin/commit/d7b1bd961ad8ad7bc8ed0e62f5a4a801b938a7f6))

# [1.16.0](https://github.com/max5800/k-fin/compare/v1.15.1...v1.16.0) (2026-04-17)


### Features

* **agents:** auto-apply high-confidence categorizations to transactions ([87f9eb1](https://github.com/max5800/k-fin/commit/87f9eb1fe6e3b814b27e061140b94361aa0b2cee))
* **m7:** wire full sync pipeline, fix ESO secret split, seed categories ([2a57696](https://github.com/max5800/k-fin/commit/2a576960b3150951d8d84c6953f94240a07097d1))

## [1.15.1](https://github.com/max5800/k-fin/compare/v1.15.0...v1.15.1) (2026-04-16)


### Bug Fixes

* **connector:** resolve nested currentPrice structure from Comdirect API ([16b6daf](https://github.com/max5800/k-fin/commit/16b6daf8777ff606e4e1e980ad01be8b12fe7a43))

# [1.15.0](https://github.com/max5800/k-fin/compare/v1.14.1...v1.15.0) (2026-04-16)


### Bug Fixes

* **ci:** align image and chart names with k-fin branding ([05d56d1](https://github.com/max5800/k-fin/commit/05d56d1dc9969d60f7586ab70af958213b1f6e39))
* **ci:** disable husky hooks in semantic-release step ([cb47ceb](https://github.com/max5800/k-fin/commit/cb47cebe9e9a367c2f40e9f4fabf2666d9ed3180))
* **dev:** remote Tilt dev environment for k3s-app cluster ([9b24a84](https://github.com/max5800/k-fin/commit/9b24a846fa0d336c0077de519d235af5d3244fb1))
* **m7:** resolve crash bugs — Report model, agent names, tags router ([8224c42](https://github.com/max5800/k-fin/commit/8224c4256047d46a3cdf1cc3fd2fddd37cef9355))
* **normalization:** handle SWIFT fields starting with digits in description ([8499cd5](https://github.com/max5800/k-fin/commit/8499cd518f9d5ee62df66ba5ea30aaf4bfc98cae))
* **normalization:** use fixed-width 37-char SWIFT blocks for description parsing ([26d4103](https://github.com/max5800/k-fin/commit/26d41034317e5c269ff3975c24d59bada4f7fd57))
* resolve 9 API bugs found during dev endpoint testing ([1a3e00e](https://github.com/max5800/k-fin/commit/1a3e00e7b20a1526a023e30e3e10448b7e82d593))


### Features

* **api:** add POST /sync/normalize endpoint for re-normalization ([2bf9005](https://github.com/max5800/k-fin/commit/2bf900503193cd75522858b611054e85253a1f1e))
* **api:** add sync proxy router to public API with input validation ([5813f27](https://github.com/max5800/k-fin/commit/5813f277808770dfe9a73d3dcf02dff7d68b3077))
* **dev:** prepare helm and ingress for k-fin dev stage ([958f218](https://github.com/max5800/k-fin/commit/958f218348f49def61066b0eaff8e82c4dab5b24))
* **dev:** target remote k-fin dev stage with Tilt ([#16](https://github.com/max5800/k-fin/issues/16)) ([460e110](https://github.com/max5800/k-fin/commit/460e110643d0322b6eb2021670a79f2547012950))
* **m6:** add agent runs API with DB migration ([#12](https://github.com/max5800/k-fin/issues/12)) ([25892f7](https://github.com/max5800/k-fin/commit/25892f7e12532a40a8bbdd59237aaf1e77569253))
* **m6:** add aggregates API for financial dashboards ([#13](https://github.com/max5800/k-fin/issues/13)) ([7f869e8](https://github.com/max5800/k-fin/commit/7f869e8bd16d1e419c0bb534cf9b2e7ae11ee5e6))
* **m6:** add categories and budgets API ([#11](https://github.com/max5800/k-fin/issues/11)) ([a57d382](https://github.com/max5800/k-fin/commit/a57d382ca80fb5924de1cf679cb3fe75b7eaa6a6))
* **m6:** add reports API endpoints ([#15](https://github.com/max5800/k-fin/issues/15)) ([7dae698](https://github.com/max5800/k-fin/commit/7dae698c5c0d852a119a3033efef5775aaa5e065))
* **m7:** complete agent pipeline — worker integration + tests ([70a5c88](https://github.com/max5800/k-fin/commit/70a5c88447430df8b1b84b4a244f63cc7115bc4e))
* **m7:** wire runs API to agent orchestrator, persist reports ([38d5bdb](https://github.com/max5800/k-fin/commit/38d5bdb950848465c484f3ab0ad03259d0856b10))

## [1.14.1](https://github.com/max5800/k-fin/compare/v1.14.0...v1.14.1) (2026-04-16)


### Bug Fixes

* **ci:** move dev deps to dependency-groups and auto-format codebase ([f7ab07e](https://github.com/max5800/k-fin/commit/f7ab07e38477905e01670a5b6cb33ee0e59f8f86))

# [1.14.0](https://github.com/max5800/k-fin/compare/v1.13.0...v1.14.0) (2026-04-16)


### Features

* **m6:** add Finance API foundation with transactions endpoints ([9d7e9a5](https://github.com/max5800/k-fin/commit/9d7e9a555ed537443d3fa49bc0a4d706f14470a3))

# [1.13.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.12.2...v1.13.0) (2026-04-13)


### Features

* **m5:** postgres via CNPG + alembic baseline + normalization pipeline ([d5b90a3](https://github.com/max5800/comdirect-firefly-sync/commit/d5b90a3d1e36d4b92461785178ecdf19c9b63c2b))

## [1.12.2](https://github.com/max5800/comdirect-firefly-sync/compare/v1.12.1...v1.12.2) (2026-04-11)


### Bug Fixes

* pass transaction fetch params from settings to all callers ([7729c80](https://github.com/max5800/comdirect-firefly-sync/commit/7729c80d1b60d10bbbf416def6fd03edd5b2b059))

## [1.12.1](https://github.com/max5800/comdirect-firefly-sync/compare/v1.12.0...v1.12.1) (2026-04-10)


### Bug Fixes

* default chart images to versioned tags ([5040830](https://github.com/max5800/comdirect-firefly-sync/commit/504083010449ff2d148d9b59a27ebf42315f1add))

# [1.12.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.11.0...v1.12.0) (2026-04-10)


### Features

* allow per-sync request overrides ([e4abd02](https://github.com/max5800/comdirect-firefly-sync/commit/e4abd02970b4d1b296b8ab73c054a689e6f0a0d9))

# [1.11.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.10.0...v1.11.0) (2026-04-10)


### Features

* make worker fetch window configurable ([b65f967](https://github.com/max5800/comdirect-firefly-sync/commit/b65f9676554a34a603cc04343e1e63dd9c146df0))

# [1.10.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.9.2...v1.10.0) (2026-04-10)


### Features

* add optional ingress tls support ([968c4dd](https://github.com/max5800/comdirect-firefly-sync/commit/968c4ddf39f829447f40b25c61b29bd9b201a9b4))

## [1.9.2](https://github.com/max5800/comdirect-firefly-sync/compare/v1.9.1...v1.9.2) (2026-04-10)


### Bug Fixes

* use uppercase comdirect vault keys ([638f6dc](https://github.com/max5800/comdirect-firefly-sync/commit/638f6dcbb560d7b1955eda1b530f71453e32e9d4))

## [1.9.1](https://github.com/max5800/comdirect-firefly-sync/compare/v1.9.0...v1.9.1) (2026-04-10)


### Bug Fixes

* split vault paths for comdirect secrets and api token ([14804d7](https://github.com/max5800/comdirect-firefly-sync/commit/14804d749d28a0b93bbbabdda774151c37b69623))

# [1.9.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.5...v1.9.0) (2026-04-10)


### Features

* add imagePullSecrets support to API and worker deployments ([567366c](https://github.com/max5800/comdirect-firefly-sync/commit/567366cd088f6a33b936902fe65a1c88d16df6a1))

## [1.8.5](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.4...v1.8.5) (2026-04-10)


### Bug Fixes

* document CORS_ORIGINS in env example ([acd656a](https://github.com/max5800/comdirect-firefly-sync/commit/acd656abe382225f72efd2417258f6e5e81f9e86))

## [1.8.4](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.3...v1.8.4) (2026-04-10)


### Bug Fixes

* add CORS middleware + correct vault path to k8s/finance-api ([70246f7](https://github.com/max5800/comdirect-firefly-sync/commit/70246f7439ee3a2bee8e14338a73f78aaf50ba41))

## [1.8.3](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.2...v1.8.3) (2026-04-10)


### Bug Fixes

* inject API_TOKEN from vault secret into api container ([5009743](https://github.com/max5800/comdirect-firefly-sync/commit/5009743dc5d32b5a5998705cb31f358621497f1e))

## [1.8.2](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.1...v1.8.2) (2026-04-10)


### Bug Fixes

* make API_TOKEN required to prevent unauthenticated access ([f4e86e2](https://github.com/max5800/comdirect-firefly-sync/commit/f4e86e2b6947f2990cb5f264c13924b9b2c787c9))

## [1.8.1](https://github.com/max5800/comdirect-firefly-sync/compare/v1.8.0...v1.8.1) (2026-04-10)


### Bug Fixes

* serve json files alongside csv in api ([b4bc022](https://github.com/max5800/comdirect-firefly-sync/commit/b4bc022677ecf772b8e411365eb97ecce7969057))

# [1.8.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.7.0...v1.8.0) (2026-04-10)


### Features

* replace polling auth with deterministic two-step flow ([5155d81](https://github.com/max5800/comdirect-firefly-sync/commit/5155d816e5adba7c2e6cc08c3ec39111b74207ee))

# [1.7.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.6.2...v1.7.0) (2026-04-10)


### Features

* replace blocking input() TAN wait with async polling ([46a051f](https://github.com/max5800/comdirect-firefly-sync/commit/46a051fd058356e085c80945712aa8fd56e2f107))

## [1.6.2](https://github.com/max5800/comdirect-firefly-sync/compare/v1.6.1...v1.6.2) (2026-04-10)


### Bug Fixes

* add httpx dependency to Dockerfile.api ([3d21b45](https://github.com/max5800/comdirect-firefly-sync/commit/3d21b45ff9a542de4e717f9bd797f0e871ed23f2))

## [1.6.1](https://github.com/max5800/comdirect-firefly-sync/compare/v1.6.0...v1.6.1) (2026-04-10)


### Bug Fixes

* update Tiltfile and Helm charts for two-microservice architecture ([c2066f3](https://github.com/max5800/comdirect-firefly-sync/commit/c2066f3e573078f1c210c83840ae05ef5f921c10))

# [1.6.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.5.0...v1.6.0) (2026-04-10)


### Features

* split into api and worker microservices ([612b62d](https://github.com/max5800/comdirect-firefly-sync/commit/612b62dc41c61fa5d228e8dd628671a3e43e5649))

# [1.5.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.4.0...v1.5.0) (2026-04-10)


### Features

* enhance data mapping and validation in models and finance agent exporter ([1128218](https://github.com/max5800/comdirect-firefly-sync/commit/1128218aa12c92d97d28dc75fc35af7a16f8b6f5))

# [1.4.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.3.0...v1.4.0) (2026-04-10)


### Features

* add model-based JSON export path ([5d6e318](https://github.com/max5800/comdirect-firefly-sync/commit/5d6e318037c56647f6c5ede49cd609d76bd1e4ac))
* harden report generator for multi-format input and missing data ([1c43609](https://github.com/max5800/comdirect-firefly-sync/commit/1c43609b00b2ffb7a1ce7f65605614084f5b6b3a))

# [1.3.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.2.0...v1.3.0) (2026-04-10)


### Features

* add markdown financial report generator ([f25d985](https://github.com/max5800/comdirect-firefly-sync/commit/f25d98585cf55be7e3d2f424543cf139e2fe18c5))

# [1.2.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.1.1...v1.2.0) (2026-04-10)


### Bug Fixes

* **security:** remove session/TAN IDs from logs, mask balances in test output ([07b7e59](https://github.com/max5800/comdirect-firefly-sync/commit/07b7e597b293b29bf346be56f968b8f033f4cf99))
* update workspace configuration to include additional folders ([bafc7e6](https://github.com/max5800/comdirect-firefly-sync/commit/bafc7e681d47421efbc626ad8792829f1e32eb06))


### Features

* add domain models, CI pipeline, tests, and fix remaining issues ([5fdc2bd](https://github.com/max5800/comdirect-firefly-sync/commit/5fdc2bd38f653323fec1d0e6838cf221449dca7e))
* add Helm chart and Tiltfile for K8s deployment ([2f61ace](https://github.com/max5800/comdirect-firefly-sync/commit/2f61ace7b70792d18d864269655fe2426bfa04f9))
* harden security and add agent review team ([d0705b4](https://github.com/max5800/comdirect-firefly-sync/commit/d0705b4ee278363d68fd254b6972f168e9b61ae0))

## [1.1.1](https://github.com/max5800/comdirect-firefly-sync/compare/v1.1.0...v1.1.1) (2026-04-10)


### Bug Fixes

* align env example with config and document TAN security design ([384ab6b](https://github.com/max5800/comdirect-firefly-sync/commit/384ab6bbb1f79ac749722e1b021ebf8bf8e7a030))

# [1.1.0](https://github.com/max5800/comdirect-firefly-sync/compare/v1.0.0...v1.1.0) (2026-03-16)


### Features

* add API reference for comdirect-finance-api skill ([c678bca](https://github.com/max5800/comdirect-firefly-sync/commit/c678bcaa8e9039d65cb381695c0332b9bb166a58))
* add comdirect-finance-api OpenClaw skill ([575457b](https://github.com/max5800/comdirect-firefly-sync/commit/575457b29ee7989383dd5c66f1fd6a34db6098d1))


### Reverts

* remove AI skill section from main (should be on feature branch) ([3581e96](https://github.com/max5800/comdirect-firefly-sync/commit/3581e966caea020404d010ca4ea4e29e5d833e99))

# 1.0.0 (2026-03-15)


### Bug Fixes

* **auth:** fix 6-step Comdirect auth flow ([7c60dbb](https://github.com/max5800/comdirect-firefly-sync/commit/7c60dbb6cb200ff1e6bf8ec21937c39abbad1389))


### Features

* add Docker containerization ([48126a6](https://github.com/max5800/comdirect-firefly-sync/commit/48126a60f80ed461cb5eae2a8c73d17b854e4cae))
* add full financial CSV export with date filtering ([8e96b11](https://github.com/max5800/comdirect-firefly-sync/commit/8e96b11f554d6f780342e7c8273d90f4e74b47c0))
* add read-only REST API to serve CSV exports ([7087276](https://github.com/max5800/comdirect-firefly-sync/commit/70872768f117d1a42fdde43492b831acf51fc875))
* **connector:** add depot, positions and pagination endpoints ([3415198](https://github.com/max5800/comdirect-firefly-sync/commit/34151986d1098de1a51dd2097fa977e92b6b96b8))
* **connector:** implement full Comdirect OAuth flow with TAN confirmation ([774679b](https://github.com/max5800/comdirect-firefly-sync/commit/774679bbebd5812caa2fb69cccde7fc1ea3d8260))
* **exporter:** add finance agent output mapper ([989ff30](https://github.com/max5800/comdirect-firefly-sync/commit/989ff30cd253b9705ee0e8ddaf8cde3d1723165a))
* initial project scaffold — connector, importer, scheduler, FastAPI ([a414039](https://github.com/max5800/comdirect-firefly-sync/commit/a41403917e7042323c2fb79c377c2be3350b1386))
* **scripts:** add finance data export CLI ([35a174e](https://github.com/max5800/comdirect-firefly-sync/commit/35a174e09a0da1fce88ee37f2799d95c47ab8caf))
* **scripts:** add standalone Comdirect test (no Firefly needed) ([a8d0942](https://github.com/max5800/comdirect-firefly-sync/commit/a8d0942607c375fcf463b508b7e7e669dbe52acc))
