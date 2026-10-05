# HOLDOUT LOG

The sealed holdout starts at **2025-10-01T00:00:00Z** (fixed date, decision D-008 in docs/SPEC.md).
It may be read **once**, at gate G4, after Parham's explicit written approval.
Unsealing requires `allow_holdout=True` **and** the environment variable `TBOT_UNSEAL_HOLDOUT=G4`;
every attempt — successful or refused — is appended below.

| timestamp (UTC) | caller | reason | approved by | outcome |
|---|---|---|---|---|
| — | — | — | — | holdout still sealed |
