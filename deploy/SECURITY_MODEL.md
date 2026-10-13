# DEK security model

## Protected goals

- Only DingTalk-authorized employees can read the site or use the bot.
- Only configured reviewers can approve, reject or reclassify knowledge.
- A review decision is bound to the exact source snapshot and candidate content.
- One unprivileged service cannot perform the complete publish chain by itself.
- Releases are immutable, auditable and automatically rolled back when live proofs fail.

## Trust boundary

The host administrator (`root`) and the deployed application code are trusted. A hostile
`root` can replace code, units, state and local keys, so this single-host design does not
claim to resist host-administrator compromise. Protecting against that threat requires an
external signer, KMS or independently administered host.

## Current signature boundaries

1. The publisher signs `approval.json`; the credentialless, networkless builder verifies it.
2. After validating the build and successfully pushing Git, the publisher signs
   `activation-ready.json`; the activator verifies it and the bound `release.json` digest.

There is no separate signature on `release.json` for current releases. It used the same key
as the activation gate and added no independent authorization boundary. The activator still
accepts the legacy field and file for existing candidates during rollout and rollback.

Service accounts, filesystem permissions, the review queue lock, artifact digests, immutable
release directories, live Web/QA proofs and rollback remain unchanged.
