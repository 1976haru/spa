# Operator UI V2

The API-free Sourcing page includes a candidate-count batch panel with target, automatic MASTER import toggle, current run/progress, and pause/resume/cancel/retry controls. `NEEDS_DETAIL` candidates can be queued together. Batch controls call `BatchSourcingService`; UI callbacks do not issue batch SQL. Product detail tabs are opened by the paired extension one at a time.

Sourcing now presents API-free Browser Capture before optional Keepa automation. Settings creates local pairing codes for the unpacked ShopSource Capture extension. The capture candidate list shows detail-completeness and status; operators can open candidate product pages, import detail-complete records, and use the regular Products/Packages pages. The extension is available for Edge and Chrome at `browser_extension/shopsource_capture`.

Install the optional local UI dependency with `pip install -e ".[ui]"`, then run `run_ui_v2.bat` or
`shopsource-ui-v2`. It binds to `127.0.0.1:8081`. The existing `shopsource-gui` Tkinter app remains
available as the fallback.

Sidebar pages: Dashboard, Stores, Sourcing, Products, Packages, History, Settings.
Product queries use SQLite filtering/sorting and fetch 25–200 rows per page. AG Grid is explicitly
initialized with `modules="community"`; the UI does not request Enterprise modules. Row details use a
drawer-like dialog and bulk status changes call the same Store decision table semantics as CLI overrides.

Store Workbench includes a six-step profile wizard and Keyword Studio. Keepa validation runs only after
operator confirmation, shows an estimated token/request range, and accepts at most ten terms in one batch.
Operator preferences (default target/token budget, Light/Dark theme, log level) are saved in SQLite.
Dashboard and sourcing pages show
actionable empty states and run/token/package summaries.

For Windows Credential Manager integration, install `pip install -e ".[credentials]"`. Without keyring,
use `KEEPA_API_KEY` in the process environment; the application does not write the value to a file.
