# Shopify Production App CLI bootstrap

This directory is reserved for linking the already-approved Shopify Production App. ShopSource never runs Shopify CLI commands automatically.

## Link an existing app

From the repository root, run:

```powershell
pwsh -File tools/shopify_app/safe_shopify_cli.ps1 -Action Link
```

In the Shopify CLI prompt, select the existing Production App. Do not create a new app. The CLI command is `shopify app config link`. After linking, compare the generated `client_id` and declared scopes with the Shopify `app.apiKey` readback and `config/shopify_scope_contract.json`.

Linked `shopify.app*.toml` files in this directory are ignored by Git. Never copy a Client Secret into TOML or source control.

## Validate and deploy

```powershell
pwsh -File tools/shopify_app/safe_shopify_cli.ps1 -Action Validate
pwsh -File tools/shopify_app/safe_shopify_cli.ps1 -Action Deploy -ConfirmProductionDeploy
```

Deploy changes the real Shopify app configuration/version. It requires an explicit switch and the operator must type `DEPLOY`. This implementation did not run link, deploy, app install, or any Shopify mutation.

Official references: https://shopify.dev/docs/apps/build/cli-for-apps/manage-app-config-files and https://shopify.dev/docs/apps/build/cli-for-apps/app-configuration.
