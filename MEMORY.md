# Project context

- Created on 2026-11-17 by extracting the proven structured API workflow into an independent Python CLI package. Runtime imports must not depend on historical workspace scripts, media, credentials, or modified SDK files.
- Directory co-location identifies candidates only; filenames and numbering must not determine narrative relevance. Downloads require explicit file/group selection and plan-specific human confirmation. Approval is reusable only for an unchanged exact scope.
- The repaired provider contracts use account sync's `no` field, preserve the leading `1` in share IDs, and percent-encode authorization JSON before signing. The pinned public protocol reference is parsed as data in memory and never executed or saved.
- Scoped provider sessions enforce HTTPS, allowed provider domains, and TLS verification across redirects. Credentials and signed URLs must stay out of reports, logs, archives, and command arguments.
- Receipts bind plan identity, destination, source identity, byte counts, and media evidence. Atomic no-overwrite publication uses hard links; require a supporting filesystem. An interrupted publication can recover its verified receipt.
- Job mutation uses POSIX kernel locks and atomic JSON replacement. Concurrency is bounded to four isolated sessions. Current-run transferred bytes exclude already-present partial bytes and reused complete files.
- The historical real download succeeded before packaging. Package acceptance uses offline fixtures plus real local media verification; do not describe it as a new live-provider download.

## Repository publication

- The user authorized publishing this standalone project to https://github.com/sys98/baidu-download after improving the README. The repository was empty, public, and used main as its initial branch.
- README is the concise project homepage; docs/usage.md retains the complete task-oriented guide. The static workflow in assets/readme/workflow.svg explains the real scan, plan, and confirmation boundary without generated art or fabricated metrics.
- Only portable source, tests, documentation, and the third-party notice belong in this repository. Historical share lists, extraction codes, media, receipts, account credentials, and signed URLs must not be imported.
- Source distributions include docs and README assets so local documentation links remain valid after extraction. Project URLs point to the user-authorized repository.
- No AGENTS.md existed in the empty repository and no matching local governance template was available; user-provided rules govern this task.
