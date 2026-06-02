# Store Your User PAT in an Encrypted Secret Store

Keep your bootstrap user PAT (`axp_u_…`) off the filesystem in plaintext by
routing `ax login` through an encrypted secret store. The CLI itself never
needs to know which backend you use — the integration point is the `AX_TOKEN`
environment variable.

Background: [`docs/credential-security.md`](../credential-security.md) and
[`ADR-005`](../adr/ADR-005-credentials-never-in-workspace.md) define the
"credentials never in workspace" trust boundary that motivates this workflow.

## Goal

Verify your PAT through `ax login`, capture the verified token into an
encrypted store (dotenvx, sops, or pass), and stop writing the plaintext copy
to `~/.ax/user.toml`. From then on, run `ax` commands through a wrapper that
decrypts the secret into the child process environment.

## Prerequisites

- A user PAT minted from the paxai.app UI (Settings → Credentials → User
  Token, CLI scope).
- One of: [`dotenvx`](https://dotenvx.com/),
  [`sops`](https://github.com/getsops/sops), or
  [`pass`](https://www.passwordstore.org/) installed locally.
- The `--print` flag on `ax login`, which verifies the PAT and emits it on
  stdout instead of writing `~/.ax/user.toml`. Status messages route to
  stderr, so stdout is a clean pipe target. Running `ax login --print` with
  no `--token` triggers a hidden prompt — keeping the secret out of shell
  history and process listings, which is the form used throughout this guide.

## Clear the on-disk plaintext copy first

This step is **load-bearing**, not cleanup. The user-PAT resolver
([#175](https://github.com/FulcrumDefense/fulcrum-gateway/issues/175))
reads `~/.ax/user.toml` *before* falling back to the environment for
user-login commands (`ax token mint`, `bootstrap`, `ax gateway login`).
So while the file is present, those commands silently ignore your
encrypted `AX_TOKEN`. The encrypted-env workflow does nothing for them
until the file's `token` field is cleared.

Clear only the `token` field — the file also carries `base_url`, `space_id`,
and (for named-env logins) `environment` defaults you want to keep:

```bash
# Inspect first.
cat ~/.ax/user.toml

# Drop the token line, keeping the rest.
sed -i.bak '/^token = /d' ~/.ax/user.toml && rm ~/.ax/user.toml.bak
```

For named-env logins (`ax login --env dev`), the file is at
`~/.ax/users/dev/user.toml`; clear its `token` line the same way.

Once the file's `token` is gone, the env var becomes authoritative for every
`ax` command.

## Recipe: dotenvx (recommended for daily use)

`dotenvx` transparently wraps the child command and decrypts environment
variables into the child process on the fly — no shell-out, no manual unset
on exit.

1. **Verify the PAT and capture it into an encrypted .env.**

   ```bash
   ax login --print | dotenvx set AX_TOKEN --stdin --encrypt
   ```

   `ax login --print` with no `--token` argument prompts hiddenly for the
   PAT, then verifies it via token exchange + whoami. The verified token
   goes to stdout (status messages go to stderr).
   `dotenvx set --stdin --encrypt` reads it from the pipe — the secret
   never appears as a process argument or in shell history.

2. **Protect `.env.keys` like the secret it holds.**

   `.env` is safe to commit (it contains only ciphertext). `.env.keys`
   contains the *decryption key* and must never leak:

   ```bash
   echo '.env.keys' >> .gitignore
   chmod 600 .env.keys
   ```

   If `.env.keys` is ever accidentally committed (even to a private repo),
   treat it as a PAT leak — every encrypted `AX_TOKEN` value in git history
   is now decryptable. Rotate the PAT through the paxai.app UI and re-run
   step 1 with the new token; deleting the file from the latest commit is
   not enough.

3. **Run `ax` through the wrapper.**

   ```bash
   dotenvx run -- ax spaces list
   dotenvx run -- ax auth whoami
   ```

   The CLI sees `AX_TOKEN` in its environment exactly as if you had exported
   it manually. The plaintext value is never written to disk.

4. **Verify nothing shadows the env var.**

   ```bash
   dotenvx run -- ax auth doctor
   ```

   A clean migration shows no `user_pat_in_file_and_env` warning. If it
   fires, the on-disk file still has a `token` field; re-run the
   "Clear the on-disk plaintext copy first" step.

## Recipe: pass (cleanest moving parts)

`pass` stores secrets as gpg-encrypted files under `~/.password-store`. It
is the smallest backend — no daemons, no extra keys to manage beyond your
gpg keyring, no committed-ciphertext story. For security-conscious operators
it is the most defensible of the three recipes here.

1. **Capture the verified PAT into the password store.**

   ```bash
   ax login --print | pass insert -e ax/user-token
   ```

   `pass insert -e` reads the secret from stdin without echoing.
   The on-disk file `~/.password-store/ax/user-token.gpg` is gpg-encrypted.

2. **Run `ax` with the secret exported into the environment for one
   command.**

   ```bash
   AX_TOKEN=$(pass ax/user-token) ax spaces list
   ```

   For repeated use, define a shell function so the unwrapping happens at
   call time rather than at shell startup:

   ```bash
   ax-pass() { AX_TOKEN=$(pass ax/user-token) command ax "$@"; }
   ```

3. **Verify nothing shadows the env var.**

   ```bash
   AX_TOKEN=$(pass ax/user-token) ax auth doctor
   ```

## Recipe: sops (when you need age, gpg, or KMS)

`sops` supports age, gpg, and KMS backends — useful when you want the
encrypted value committed alongside infrastructure code or when a team
shares a vault via KMS.

1. **Capture the verified PAT into an encrypted dotenv, never touching
   plaintext disk.**

   ```bash
   ax login --print \
     | sed 's/^/AX_TOKEN=/' \
     | sops -e --age <your-age-recipient> --input-type dotenv --output-type dotenv /dev/stdin \
     > ax-token.env.enc
   ```

   The token streams `ax login` → `sed` (wraps it as `AX_TOKEN=…`) →
   `sops` (encrypts from stdin) → `ax-token.env.enc`. Nothing plaintext is
   written to disk at any point.

   Avoid intermediate `/tmp` files. `shred` cannot guarantee erasure on
   journaled, CoW, or SSD-backed filesystems (ext4 with `data=journal`,
   btrfs, ZFS, APFS) — the plaintext can outlive the `shred` call. If you
   must use an intermediate, write it under `/dev/shm` (tmpfs, RAM-backed)
   so the value disappears at reboot.

2. **Run `ax` through `sops exec-env`.**

   ```bash
   sops exec-env ax-token.env.enc 'ax spaces list'
   ```

   The decrypted environment lives only in the child process — once the
   command exits, the plaintext is gone.

3. **Verify nothing shadows the env var.**

   ```bash
   sops exec-env ax-token.env.enc 'ax auth doctor'
   ```

## Non-interactive use

If you must run `ax login --print` non-interactively (CI, automation, a
provisioning script), the `--token <value>` form bypasses the hidden prompt:

```bash
ax login --print --token "$AX_BOOTSTRAP_TOKEN" | dotenvx set AX_TOKEN --stdin --encrypt
```

This is a real tradeoff: `--token "$VAR"` places the PAT into argv, where
it is visible in `ps` / `/proc/<pid>/cmdline` for the life of the `ax`
process and lands in shell history unless suppressed. Only use this form
when the calling environment is already trusted (a CI runner with a
short-lived bootstrap token, a one-shot provisioning step). For
interactive setup on a workstation, prefer the hidden-prompt form.

## Verifying the migration

`ax auth doctor` is the integrity check. If `~/.ax/user.toml` still has a
`token` field AND either `AX_TOKEN` or `AX_USER_TOKEN` is set in the
environment, doctor surfaces a `user_pat_in_file_and_env` warning naming
the exact file path and describing the precedence split:

```
warning: user_pat_in_file_and_env - two sources of truth for user PAT —
precedence differs by command path: user-PAT commands (e.g. `ax token mint`,
`bootstrap`, `ax gateway login`) read the file first; general runtime
commands read the env var first. To make the env var authoritative
everywhere, clear the `token` field in /home/<user>/.ax/user.toml — the
file also carries `base_url`, `space_id`, and other login defaults that
blanket `rm` would lose.
```

A clean migration shows no `user_pat_in_file_and_env` in the doctor output.
Named-env logins (`ax login --env dev`) point at `~/.ax/users/dev/user.toml`;
the warning names that path when the env is active.

## What this protects against

- **Filesystem reads** by sync clients (Dropbox, iCloud, OneDrive,
  Codespaces dotfiles) and tarball backups of `$HOME`.
- **Casual extraction** by other tools or assistants sharing the
  workspace — per `CLAUDE.md`, "multiple assistants can share a directory."
- **Routine `grep token ~/.ax/user.toml`** by anything with shell access.

## What this does NOT protect against

Once `AX_TOKEN` is in any process environment or HTTP request, it is
exposed to the calling code. Encryption at rest is one defense-in-depth
layer; the others are scoped PATs, fingerprint-based detection (see
[`credential-security.md`](../credential-security.md#fingerprinting)),
short PAT lifetimes, and not piping the secret into untrusted code.

## Related

- `ax login --print` — the flag that unlocks every encrypted-store
  workflow ([source](../../ax_cli/commands/auth.py)).
- [`docs/credential-security.md`](../credential-security.md) —
  fingerprinting, honeypot keys, PAT rotation policy.
- [`ADR-005`](../adr/ADR-005-credentials-never-in-workspace.md) — the
  underlying trust-boundary decision.
- [#175](https://github.com/FulcrumDefense/fulcrum-gateway/issues/175) —
  the user-PAT resolver precedence split that makes clearing the on-disk
  token field load-bearing.
