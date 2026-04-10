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
