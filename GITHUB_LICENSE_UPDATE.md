# GitHub license update checklist

This overlay changes the EIRVEN-owned source-code license from the previously used MIT policy to **GNU AGPL v3 or later (`AGPL-3.0-or-later`)** for the current/future licensed branch.

After applying this overlay to the repository:

1. Commit the root `LICENSE`, `LICENSING.md`, `COPYRIGHT.md`, `BRANDING.md`, updated `THIRD_PARTY_NOTICES.md`, and `release_assets/*LICENSE*` / data notices.
2. Remove or replace any old README badge/text that still says `MIT` for EIRVEN source code.
3. In package metadata, if present, replace EIRVEN's own license identifier with `AGPL-3.0-or-later`.
4. Do **not** replace third-party license identifiers with AGPL.
5. Keep the Qwen3 Apache 2.0 license file and Education provenance notices in redistributable archives that contain those assets.
6. If there are outside contributors whose code was not owned by or relicensable by the project, verify permission/compatibility before representing their contributions as relicensed.

GitHub normally recognizes a standard license when the canonical license text is stored in the repository root as `LICENSE`.
