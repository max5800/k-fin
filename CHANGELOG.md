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
