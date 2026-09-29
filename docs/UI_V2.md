# Operator UI V2

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
