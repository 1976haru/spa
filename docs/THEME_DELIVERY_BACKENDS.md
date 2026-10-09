# Theme delivery backends

ShopSource keeps preview construction, semantic checks, raw-byte preservation,
backup creation, verification, and rollback policy above the transport layer.
Backends move exact theme file contents and report what they observed.

## ADMIN_GRAPHQL

Directly updates `templates/index.json` through Shopify Admin GraphQL. It is
available only when `write_themes` and Shopify's required exemption are active.
This remains the primary transport.

## THEME_ACCESS_CLI

An owner-controlled operational option for stores where the merchant has
installed Theme Access, generated a password with Themes permission, and
configured that password in the local operating system credential store. The
CLI backend is not a way around Shopify security or app approval. Phase A
supports only the exact `templates/index.json` file and does not connect to UI
actions.

## THEME_APP_EXTENSION

The longer-term scalable delivery lane. A Theme App Extension exposes blocks
in Theme Editor; the merchant adds and activates the app block. It avoids direct
theme code editing and can componentize Featured Products and Category
Shortcuts for many stores. This lane is documented for future work and is not
implemented in Phase A.
