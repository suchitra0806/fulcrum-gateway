# Store Your User PAT in an Encrypted Secret Store

Keep your bootstrap user PAT (`axp_u_…`) off the filesystem in plaintext by
routing `axctl login` through an encrypted secret store. The CLI itself never
needs to know which backend you use — the integration point is the `AX_TOKEN`
environment variable, which sits at the top of the config-resolution cascade.

Background: [`docs/credential-security.md`](../credential-security.md) and
[`ADR-005`](../adr/ADR-005-credentials-never-in-workspace.md) define the
"credentials never in workspace" trust boundary that motivates this workflow.

## Goal

Verify your PAT through `axctl login`, capture the verified token into an
encrypted store (dotenvx, sops, or pass), and stop writing the plaintext copy
to `~/.ax/user.toml`. From then on, run `axctl` commands through a wrapper
that decrypts the secret into the child process environment.

## Prerequisites

- A user PAT minted from the paxai.app UI (Settings → Credentials → User
  Token, CLI scope).
- One of: [`dotenvx`](https://dotenvx.com/),
  [`sops`](https://github.com/getsops/sops), or
  [`pass`](https://www.passwordstore.org/) installed locally.
- The `--print` flag on `axctl login`, which verifies the PAT and emits it on
  stdout instead of writing `~/.ax/user.toml`. Status messages route to
  stderr, so stdout is a clean pipe target.

## Recipe: dotenvx (recommended)

`dotenvx` is the smoothest fit for `AX_TOKEN` because it transparently wraps
the child command and decrypts environment variables into the child process
on the fly — no shell-out, no manual unset on exit.

1. **Verify the PAT and capture it into an encrypted .env.**

   ```bash
   axctl login --print --token <paste> | dotenvx set AX_TOKEN --stdin --encrypt
   ```

   `axctl login --print` runs the full verification (token exchange, whoami)
   and prints the verified token on stdout. `dotenvx set --stdin --encrypt`
   reads the token from stdin (so the secret never appears as a process
   argument), writes the encrypted value into `.env`, and the decryption key
   into `.env.keys`.

   Commit `.env` to source control; keep `.env.keys` out of it (or in a
   separate vault).

2. **Run `axctl` through the wrapper.**

   ```bash
   dotenvx run -- axctl spaces list
   dotenvx run -- axctl auth whoami
   ```

   The CLI sees `AX_TOKEN` in its environment exactly as if you had exported
   it manually. The plaintext value is never written to disk.

3. **Confirm `~/.ax/user.toml` is empty (or missing) and the warning is silent.**

   ```bash
   ls ~/.ax/user.toml         # should not exist
   dotenvx run -- axctl auth doctor
   ```

   If the file exists from a prior plaintext `axctl login`, remove it:
   `rm ~/.ax/user.toml`. `auth doctor` emits a
   `user_pat_in_file_and_env` warning when both sources are populated; see
   "Verifying the migration" below.

## Recipe: sops

`sops` (Mozilla) supports age, gpg, and KMS backends. Use `sops exec-env` to
decrypt into a child process environment.

1. **Capture the verified PAT into an encrypted file.**

   ```bash
   axctl login --print --token <paste> > /tmp/ax-token.raw
   echo "AX_TOKEN=$(cat /tmp/ax-token.raw)" > /tmp/ax-token.env
   sops -e --age <your-age-recipient> /tmp/ax-token.env > ax-token.env.enc
   shred -u /tmp/ax-token.raw /tmp/ax-token.env
   ```

   `shred -u` overwrites and unlinks the plaintext intermediates. Adjust to
   `rm -P` (BSD) if `shred` is unavailable.

2. **Run `axctl` through `sops exec-env`.**

   ```bash
   sops exec-env ax-token.env.enc 'axctl spaces list'
   ```

   The decrypted environment lives only in the child process — once the
   command exits, the plaintext is gone.

3. **Delete any existing plaintext copy.**

   ```bash
   rm ~/.ax/user.toml
   ```

## Recipe: pass

`pass` stores secrets as gpg-encrypted files under `~/.password-store`. It is
the lightest backend — no daemons, no key servers beyond your gpg keyring.

1. **Capture the verified PAT into the password store.**

   ```bash
   axctl login --print --token <paste> | pass insert -e ax/user-token
   ```

   `pass insert -e` reads the secret from stdin without echoing.

2. **Run `axctl` with the secret exported into the environment for one
   command.**

   ```bash
   AX_TOKEN=$(pass ax/user-token) axctl spaces list
   ```

   For repeated use, define a shell function:

   ```bash
   ax() { AX_TOKEN=$(pass ax/user-token) command axctl "$@"; }
   ```

3. **Delete any existing plaintext copy.**

   ```bash
   rm ~/.ax/user.toml
   ```

## Verifying the migration

After cutover, `axctl auth doctor` is the integrity check. If
`~/.ax/user.toml` still contains a `token` field AND `AX_TOKEN` is set in the
environment, doctor surfaces a `user_pat_in_file_and_env` warning naming the
exact file path and the cleanup command:

```
warning: user_pat_in_file_and_env - two sources of truth for user PAT —
AX_TOKEN wins; consider deleting the file copy with:
rm /home/<user>/.ax/user.toml
```

A clean migration shows no `user_pat_in_file_and_env` in the doctor output.

Named-env logins (`axctl login --env dev`) point at `~/.ax/users/dev/user.toml`;
the warning names that path when the env is active.

## What this protects against

- **Filesystem reads** by sync clients (Dropbox, iCloud, OneDrive, Codespaces
  dotfiles) and tarball backups of `$HOME`.
- **Casual extraction** by other tools or assistants sharing the workspace —
  per `CLAUDE.md`, "multiple assistants can share a directory."
- **Routine `grep token ~/.ax/user.toml`** by anything with shell access.

## What this does NOT protect against

Once `AX_TOKEN` is in any process environment or HTTP request, it is exposed
to the calling code. Encryption at rest is one defense-in-depth layer; the
others are scoped PATs, fingerprint-based detection (see
[`credential-security.md`](../credential-security.md#fingerprinting)), short
PAT lifetimes, and not piping the secret into untrusted code.

## Related

- [`axctl login --print`](../../ax_cli/commands/auth.py) — the flag that
  unlocks every encrypted-store workflow.
- [`docs/credential-security.md`](../credential-security.md) — fingerprinting,
  honeypot keys, PAT rotation policy.
- [`ADR-005`](../adr/ADR-005-credentials-never-in-workspace.md) — the
  underlying trust-boundary decision.
