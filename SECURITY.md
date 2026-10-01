# Security policy

reinvent-planner signs you in to the AWS Events API and can change your event schedule, so
security reports are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue.** Use GitHub's private vulnerability reporting instead:
the repository's **Security** tab → **Report a vulnerability**.

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get
an acknowledgement within a few days. Once a fix is released, you'll be credited unless you'd
rather not be.

## Scope

In scope:

- Leaking an access or refresh token: to logs, tracebacks, files other users can read, URLs,
  or any host other than `api.awsevents.com` and `oauth.awsevents.com`
- Weaknesses in the sign-in flow: PKCE, `state` checking, the local callback server
- Changing someone's schedule without their confirmation
- Reading or writing the local database or token file across user boundaries
- Dependency or release supply-chain problems
- Spreadsheet formula injection through CSV exports, or terminal markup or escape-sequence
  injection through text from the API or config files
- The local browser mode (`rip ui`): reaching it without the one-time link, from another
  site or another device

Out of scope: the AWS Events API and AWS Builder ID themselves. Report those to AWS through
<https://aws.amazon.com/security/vulnerability-reporting/>.

## How tokens are handled

- Stored in the OS keychain by default. The opt-in file store is written with mode `0600` and
  refused if its permissions are loosened, if it's a symlink, or if another user owns it.
- The access token is sent only to `https://api.awsevents.com`, checked on every request, with
  redirects disabled. The refresh token goes only to `https://oauth.awsevents.com` (to refresh,
  and to revoke on `rip logout`).
- Never logged, never placed in URLs, and hidden from `repr()` and tracebacks.
- `rip logout` revokes the refresh token and deletes local copies.

## Supported versions

Only the latest release gets security fixes.
