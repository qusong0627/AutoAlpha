# AGENTS.md

## What this repo is

**A read-only mirror of the factor-mining slice of `D:\git\quantmindoss` @ branch `next`.**
Nothing here is an original codebase — it was extracted so the module can run standalone
with its own Postgres / data dirs / frontend.

* The upstream repo is the source of truth. **Never write to `D:\git\quantmindoss`.**
* Allowed read pattern: `git -C D:\git\quantmindoss show next:<path>` or
  `git -C D:\git\quantmindoss ls-tree -r --name-only next -- <paths>`.
* Re-sync/add files with `git archive` (not checkout):
  `git -C D:\git\quantmindoss archive next -o <tmp>.tar -- <paths>` then `tar -xf <tmp>.tar -C D:\git\autoalpha`.
* Status: `git -C D:\git\quantmindoss status --porcelain` must stay at the 5 pre-existing
  master-dirty files. If you added files there, you broke the "don't touch upstream" rule.

`FACTOR_MINING_MIGRATION.md` is the long-form log (why PG, what broke, full verification
transcripts). Read it when something here is unclear.

## Layout

| Path | What it owns |
|---|---|
| `web/` | The only frontend. A **slim** Vite app: 155 files, 14 runtime deps. Load-bearing: `features/alpha-research` (mine/pool/backtest) + `features/factor-research` (research). The full upstream `electron/` (1343 files) was deleted on purpose. Only `src/workspace/*` is routed — see **Frontend visual design** for the two coexisting style systems. |
| `backend/services/engine/alpha_agent/` | Mining orchestration: `launcher.py`, `task_store.py`, two engines (`statistical/`, `rd_agent/`), `factorminer/`, `doc_*`, `experience_memory.py`. |
| `backend/services/engine/app_factor_mining.py` | **The entrypoint.** Mounts only the mining routers. `engine/main.py` is upstream and is *not* what runs here. |
| `backend/services/engine/factor_{backtest,report,research,library}/` | Downstream consumers. |
| `backend/services/api/routers/admin/` | Mining→training-feature graduation + rd_mined console. |
| `scripts/alpha_agent/` | Mining runners. Resolved **repo-root-relative** (`scripts/alpha_agent/run_*.py` per `algorithms/*.yaml`) — do not move. |
| `scripts/wsl/` | Deploy/ops scripts. `scripts/dev-web.cmd` starts the frontend. |
| `config/` (repo root) | Required: `backend/shared/auth.py` does a bare `import config.settings`. |

## Run it

Order matters: Postgres → backend → frontend.

```powershell
# PREREQUISITE — secrets are no longer baked into any script (fail-closed):
#   wsl bash /mnt/d/git/autoalpha/scripts/wsl/init-runtime-env.sh
#   or copy config/runtime.env.example to ~/.config/autoalpha/runtime.env and fill it
#   (if using DeepSeek, revoke the previously leaked key before setting a replacement)

# backend (WSL), port 8001 — dev flags baked in: AUTH_DISABLED=1, ENABLE_STARTUP_DDL=1
wsl bash /mnt/d/git/autoalpha/scripts/wsl/start-dev.sh

# frontend, port 3000
D:\git\autoalpha\scripts\dev-web.cmd
```

* `run-wsl.sh` (repo root) starts with **auth ON** and startup DDL OFF, but this mirror has no
  login route or configured trusted auth gateway; it is not a usable production entrypoint.
* Only the mining pages work. `/api/v1/simulation/*`, `/strategies`, `/notifications`, …
  are **404** because those routers are not mounted. Expected, not a bug.

## Verification

```powershell
# one-shot: listeners + backend health + PG counts + frontend/proxy (run this first)
powershell -File D:\git\autoalpha\scripts\verify-all.ps1

wsl bash /mnt/d/git/autoalpha/scripts/wsl/status.sh          # venv/PG/tables/service overview
wsl bash /mnt/d/git/autoalpha/scripts/wsl/run-verify.sh      # route mounts (expect 9 groups / 111 routes)
wsl bash /mnt/d/git/autoalpha/scripts/wsl/db-smoke.sh        # PG read/write + /evolve round-trip
wsl bash /mnt/d/git/autoalpha/scripts/wsl/pg-check.sh        # tables + partial unique index
wsl bash /mnt/d/git/autoalpha/scripts/wsl/quantdb-check.sh   # QuantDB actually readable
powershell -File D:\git\autoalpha\scripts\web-smoke.ps1      # frontend modules + /api proxy
powershell -File D:\git\autoalpha\scripts\no-auth-smoke.ps1  # AUTH_DISABLED path
```

* `GET /health` on 8001 is the single debugging surface: it returns `mounted[]`, `failed{}`,
  `startup{}` per ensure-tables step, `auth_disabled`, `startup_ddl`.
* Frontend gates: `cd web; npm.cmd run typecheck` (currently 0 errors) and
  `npm.cmd run build` (full graph resolution — the fastest way to catch a missing file).
  There is **no test suite**; `web/package.json` only has dev/build/preview/typecheck.

## Frontend visual design

The original design system has been **ported onto the live app** (details below); the original
files themselves remain unimported reference copies. The complete CSS import graph is two
lines — `antd/dist/reset.css` in `main.tsx` and `./workspace/workspace.css` in `App.tsx`.
Nothing else in the tree imports a stylesheet:

| Path | Holds | Status |
|---|---|---|
| `src/workspace/` — the **6 routed pages** (`HomePage` `MiningPage` `TasksPage` `FactorsPage` `BacktestPage` `HelpPage`) + `ui.tsx` / `Chart.tsx` / `WorkspaceContext.tsx` / `MiningForm.tsx` / `MiningInsights.tsx` + `workspace.css` | shell, **the live token layer**, every routed page | **Live** |
| `src/workspace/{Pool,Combos,Data,Research,Settings}Page.tsx` | five extra pages from the fuller upstream shell | **Orphan** — no route, no import (same folder as the live pages: grep by route, not by directory) |
| `src/pages/*` (`MinePage` 582 L, `FactorsPage`, `BacktestPage`, `MePage`, `ui.tsx`) | the original page bodies | **Orphan** — no import points at `./pages/*` |
| `src/styles/global.css` (1083 L) | `--bg-main`, `.app-root`, `.dock-*`, `.glass-effect`, `.text-gradient`, window radius | **Orphan** |
| `src/styles/ai-strategy-theme.css` (437 L) | `--ai-primary-*` gradient theme, shimmer/spin | **Orphan** |
| `src/styles/{mac-theme,research-next-theme,scrollbar}.css` (2 / 52 / 39 L) | extra theme + scrollbar CSS from the fuller shell | **Orphan** |
| `src/pages/pages.css` (125 L) | flat `--qm-*` palette used by `pages/*` | **Orphan** |
| `src/features/alpha-research/styles-v2/index.css` (260 L) | **`.qa-glass`**, `.gradient-mesh`, `.particles`, all `qa-*` keyframes | **Orphan** |

So the "original look" is **on screen**, but it is a re-implementation on the live class names —
those files stay unimported. What shipped:

* `workspace.css` `:root` is the live token layer: `--brand-gradient` (`135deg #4468ff ->
  #813ce8`), `--bg-card` glass, `--line-hairline`, `--shadow-premium` / `--shadow-float`,
  `--radius-card: 12px`, `--accent`, `--up`/`--down`.
* Shell = one floating capsule at the top: a brand row (gradient mark + name + subtitle, with a
  connection chip pinned right) over a **text-only tab rail carrying six entries** —
  首页 / 参数配置 / 任务中心 / 因子库 / 回测中心 / 帮助中心. No divider, no bottom dock, no
  per-tab icon. (The ten-entry 5|5 rail — 因子挖掘 / 因子池 / 组合实验 / 因子研究 / 数据与特征 /
  设置 — belongs to the original design, not to this app.)
* Every page opens with the same hero heading: eyebrow pill + 30px `h1` + description + actions.
* Two signature blocks carry the rest of the rhythm: `SpecPanel` (labelled spec columns) and
  `CardGrid` (icon + coloured tag + blurb + link). Both live in `ui.tsx`.
* Backdrop is a six-stop pastel mesh on `.workspace-app`; glass = translucent white +
  `blur(12px)` + hairline + two-layer soft shadow.

Tailwind is configured (`tailwind.config.cjs` + the postcss plugin) but the only `@tailwind`
directives live in the unimported `global.css`, so **utility class names in JSX emit
nothing** — style through `workspace.css`.

### Tokens

```
global.css (original shell)
  font      Outfit, Inter, "Noto Sans SC"   mono: JetBrains Mono, Fira Code
  brand     --primary-blue #2563eb · --electric-indigo #6366f1 · --neon-purple #a855f7
  gradient  primary 135deg #667eea -> #764ba2 · success #10b981 -> #059669
            warning #f59e0b -> #d97706 · brand mark 135deg #4468ff -> #813ce8
  semantic  up/profit #ef4444 (red = up, A-share convention) · down/loss #10b981
  slate     50 #f8fafc · 100 #f1f5f9 · 200 #e2e8f0 · 400 #94a3b8
            600 #475569 · 800 #1e293b · 900 #0f172a
  surfaces  --bg-card rgba(255,255,255,.7) · --bg-glass rgba(255,255,255,.4)
  borders   1px rgba(255,255,255,.5) glass · 1px rgba(226,232,250,.8) subtle
            1px rgba(99,102,241,.2) glow
  shadows   premium 0 4px 6px -1px rgba(0,0,0,.02), 0 10px 15px -3px rgba(0,0,0,.03)
            float   0 20px 25px -5px rgba(0,0,0,.05), 0 10px 10px -5px rgba(0,0,0,.02)
  radii     card 12 · pill 9999 · window 16 · dock item 12 · dock height 64

pages.css (original flat page bodies)
  --qm-bg #f5f7fb · --qm-card #fff · --qm-border #e8ecf3
  --qm-text-1 #1f2733 · -2 #5b6675 · -3 #98a2b3 · --qm-primary #2f6bff
  --qm-up #e2483f · --qm-down #17a673   radii 12 / 10 / 8 / 6

main.tsx antd ConfigProvider (live — change tokens here, not per component)
  colorPrimary #4f63f3 · borderRadius 10 · fontSize 13 · controlHeight 38
  Table headerBg #f7f9fe / headerColor #8591a8 · Modal radius 20
  Button primaryShadow 0 4px 12px rgba(79,99,243,.15)
```

### Layout + motion

Single column (`min(100%, 1240px)`, padding `30px 32px 35px`), one sticky capsule at the
top holding a 38x38 gradient brand mark (radius 12, `0 6px 15px #5960d520`) plus the six-entry
tab rail, then the page hero, an optional card group on a 16px grid and a full-width spec strip.
Type scale 30 / 25 / 19 / 15 / 14 / 13 / 12 / 11.

Glass = `rgba(255,255,255,.7)` + `backdrop-filter: blur(12px)` (+`saturate(160%)` on the shell)
+ a 1px `rgba(0,0,0,.05)` hairline + the two-layer soft shadow above. Layering comes from
translucency and blur, not from borders or heavy shadows.

Motion: `qa-fade-in-up/down/left/right` .6s · `qa-scale-in` .4s · `qa-float` 3s ±10px ·
`qa-shimmer` 2s sweep · `qa-pulse-glow` 2s · `qa-typing` 1.4s; dock hover
`translateY(-3px) scale(1.08)` on `cubic-bezier(.34,1.56,.64,1)`.

### Rules

* Put visual changes in a stylesheet that is **imported**. A stylesheet nothing imports is
  exactly how this repo lost its design.
* Build pages from the shared primitives in `src/workspace/ui.tsx` (`Page` for the hero heading,
  `Panel`/`Surface` for glass sections, `Stat` for KPIs, `SpecPanel`, `CardGrid`) rather than
  hand-rolling markup — that is what keeps the six pages on one rhythm.
* There is **no `navigation` array**: `App.tsx` hardcodes the rail as six `NavLink`s inside one
  `<nav className="app-tabs">` (text-only; order = reading order). Adding an entry is three edits
  in that one file — `lazy(() => import('./workspace/XPage'))`, a `<NavLink to="/x">` in the nav,
  and `<Route path="/x" element={<XPage />} />` in the `<Routes>`. Anything without a `<Route>`
  falls through the `path="*"` catch-all and redirects to `/`.
* Reviving `pages/*` is three jobs, not a routing change: mount the route, import
  `styles/global.css` **and** `features/alpha-research/styles-v2/index.css`, and wrap the mount
  in `data-theme="alpha-research"` (the shadcn tokens there are scoped under it).
* `pages/MinePage.tsx` gets its glass and hover behaviour from `className="qa-glass"`, which
  lives in the orphan styles-v2 file — without it the page renders as bare inline styles.
* Brand is **AutoAlpha** (renamed from `QuantaAlpha`). Do not reintroduce the old name in UI
  copy. `docs/` still says `QuantaAlpha` on purpose: there it is a research paper / agent name
  in a comparison table, not this product.

## Host quirks that will waste your time

* **Python must be 3.12.** `pyqlib` publishes only cp38–cp312 wheels and **no sdist**, so
  the mining engine cannot run on the system 3.14. The real venv is
  `/home/qusong/autoalpha-venv312`; `/home/qusong/autoalpha-venv` (3.14) exists but has no qlib.
  Start scripts default to 312 (`VENV=` overrides).
* Create venvs with `python3 -m venv --copies`. With the default `--symlinks`, `bin/*`
  intermittently fails with `No such file or directory` even though `cat` reads the file.
* Use `python -m pip`, not `bin/pip`. Use **Tsinghua mirror** (`-i https://pypi.tuna.tsinghua.edu.cn/simple`);
  official PyPI runs ~30 KB/s here and drops mid-download.
* Prefer running WSL work from a **script file**. `wsl bash -c "<abs-path> ..."` with the
  path as the first word intermittently reports ENOENT.
* `npm.cmd`, never `npm.ps1` (blocked by execution policy). Registry: npmmirror (`.npmrc`).
* `web/postcss.config.cjs` must keep the **`.cjs`** extension — `package.json` has
  `"type": "module"`, and a `.js` CJS config makes every page 500.
* `scripts/*.ps1` must be **ASCII-only**; PowerShell 5.1 reads them as ANSI and a
  multi-byte character can break parsing.
* **WSL cannot reach Windows-host ports.** `curl 127.0.0.1:3000` from WSL gets `000`
  (only Windows→WSL localhost forwarding exists). Frontend smoke tests run on Windows.
* `/data/*` dirs are created by root-run `scripts/wsl/link-quantdb.sh`; if you recreate them,
  preview `scripts/wsl/fix-data-owner.sh`, then run `scripts/wsl/fix-data-owner.sh --apply`
  or the service user gets `PermissionError`.

## Data

* `/data/quantdb` → symlink to `/mnt/e/quantdb` (111 GB QuantDB). Symlink rather than env-only
  because upstream code hard-falls-back to `/data/quantdb` in several places.
* `/data/quantcustom` (CUSTOM market, `6_ml_datasets/rd_mined`) and `/data/qlib/cn_data`
  (qlib bin, built via `scripts/wsl/build-qlib-cache.sh`, ~9 min) are **real dirs on ext4** —
  deliberately not on 9p, since materialization rewrites 1600+ partitions per factor.
* `QLIB_PROVIDER_URI=/data/qlib/cn_data` is pinned in the start scripts: it is the highest
  priority in `backend/shared/qlib_paths.resolve_qlib_provider_uri`, so cache writes land on
  ext4 instead of falling back to the repo's `db/qlib_data`.

## Auth model

Deliberately **no login** in this deployment:

* backend `AUTH_DISABLED=1` → the middleware injects an admin identity and skips the 401 gate;
* frontend `VITE_DISABLE_AUTH=true` in `web/.env.development.local` → `authService` seeds a
  dev admin and `LoginPage` auto-logins (both already exist upstream, no local code changes).

Three traps:

1. A secret equal to anything in `backend/shared/auth.py::_PUBLIC_INTERNAL_DEFAULTS`
   (`dev-internal-call-secret`, `changeme-*`, `autoalpha-dev-internal-secret-9f3a`, …) is treated
   as *unconfigured* → fail-closed. **Read that the right way round: being listed there means the
   value is dead, not that it is safe to use.** A secret merely *absent* from the list is accepted
   as real — which is exactly how the old hardcoded dev secret became a working admin credential.
2. `INTERNAL_CALL_SECRET` + `X-User-Id` is a full authentication bypass *by design* (see
   `get_current_user`): it mints an admin identity with no password. Treat the secret as a
   **credential** and never commit it. `scripts/wsl/start-dev.sh` and the Vite proxy no longer
   invent one — no env var, no identity headers injected.
3. The admin routers are `APIRouter(dependencies=[Depends(require_admin)])`, and
   `require_admin` only trusts `current_user["is_admin"]`; otherwise it queries an RBAC table
   that does not exist here. `app_factor_mining.py` therefore installs
   `app.dependency_overrides` for `require_admin` / `get_current_user` rather than editing
   upstream auth code. Keep that pattern.

Required secrets come from the environment only (fail-closed) — either `export` them or put them
in `config/runtime.env` (template: `config/runtime.env.example`; `start-dev.sh` sources it):

| Var | Why |
|---|---|
| `INTERNAL_CALL_SECRET` | Service-to-service trust secret, and a de-facto admin credential. |
| At least one of `DEEPSEEK_API_KEY`, `AI_IDE_LLM_API_KEY`, `AI_IDE_API_KEY`, `OPENAI_API_KEY` | LLM entrypoint for mining (`llm_client.py`). No defaults; rotate the previously leaked DeepSeek key before reuse. |
| `SECRET_KEY` (recommended) | JWT signing key. Unset → regenerated every boot → all tokens die. |

Before exposing port 8001 beyond localhost: set `AUTH_DISABLED=0`, drop `VITE_DISABLE_AUTH`,
**and make sure `INTERNAL_CALL_SECRET` is a real random value** — turning off `AUTH_DISABLED`
alone is not enough while a frontend proxy is willing to inject identity headers for you.

## Postgres, not SQLite

The storage layer is written against PG dialect and cannot be swapped: `pg_advisory_xact_lock`,
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `JSONB`, `CREATE TYPE ... AS ENUM`, `FOR UPDATE`,
and the `(user_id, fingerprint) WHERE ...` partial unique index used for mining-task idempotency.
Concurrency is genuinely multi-process (engine + celery + materialize/promote/validate scripts),
so SQLite would collapse to a whole-database write lock. Setup: `scripts/wsl/pg-setup.sh`
(role/db `quantmind`/`quantmind`, PG 18, autostarts via `/etc/wsl.conf` `systemd=true`).

## Locally modified vs upstream

These files intentionally differ from upstream. A blind re-sync from `next` will regress them:

| File | Local change |
|---|---|
| `backend/services/engine/app_factor_mining.py` | New — the standalone entrypoint + auth bypass. |
| `backend/services/engine/qlib_app/services/__init__.py` | Made lazy (PEP 562 `__getattr__`); upstream eagerly imports the whole backtest stack. |
| `backend/services/api/routers/admin/__init__.py` | Emptied; upstream aggregates ~30 admin routers we do not ship. |
| `web/**` | New slim app (own package.json / vite / tailwind / tsconfig). |
| `scripts/wsl/**`, `scripts/dev-web.cmd`, `scripts/web-smoke.ps1`, `scripts/no-auth-smoke.ps1`, `run-wsl.sh` | Ops scripts written for this deployment. |
| `backend/shared/auth.py` | Added the retired dev internal secret to `_PUBLIC_INTERNAL_DEFAULTS` so any leftover copy fails closed. |
| `backend/services/api/routers/admin/alpha_factor_pipeline.py` | `promote*` heavy compute moved off the event loop (thread pool + timeout + concurrency cap); qlib `kernels` capped. |
| `backend/services/engine/alpha_agent/doc_mining_settings.py` | Legacy MinerU Redis credentials can be deleted but never read or converted into runtime credentials. |
| `backend/services/engine/alpha_agent/doc_parse_service.py` | MinerU runtime uses service environment credentials; old rows marked as user-key-backed fail explicitly. |
| `backend/services/engine/alpha_agent/profile_gateway.py` | Removed; mining no longer fetches raw user Profile data or credentials. |

Everything else under `backend/`, `config/`, `docs/`, `alphaagent/`, `requirements/`, and
`scripts/` was *intended* to be a byte-identical upstream copy, but that assumption is false.
The 2026-10-18 blob comparison in `MIRROR_DIFFS.md` found **74 byte-different common files**
(23 differ only in CRLF/LF line endings; 51 still differ after newline normalization) and
**72 mirror-only files**; 1,797 upstream files in the compared scope are intentionally omitted
from this slim slice. The inventory excludes the purpose-built `web/` tree and generated files.
Do **not** blind-re-sync from `next`: the difference list still needs intent-by-intent review.
See `PRELAUNCH_REVIEW.md` (P1-14) and `MIRROR_DIFFS.md`.
`scripts/wsl/probe-*.sh` / `install-*.sh` are one-off diagnostics from bring-up; the canonical
entrypoints are the ones listed under "Run it" and "Verification".

## Known gaps

* Three lazy (function-level) imports have no target here and only break their own endpoints:
  `backend.services.api.market_analysis_hk`, `backend.services.api.routers.model_training`,
  `backend.services.engine.inference`.
* `research.py` / `research_service.py` belong to the investment-research aggregate, not
  factor mining; they are present for the admin tier but their routes are not mounted.
* `backend/scripts/` holds the offline pipeline (materialize → promote → validate). It has
  never been executed end-to-end here because it needs `rd_mined` data that does not exist yet.
* `web/src/pages/*` and `web/src/styles/*` are upstream design leftovers: no route, no CSS
  import, so they render unstyled if reactivated. See **Frontend visual design**.
