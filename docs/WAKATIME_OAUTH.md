# WakaTime OAuth migration

The daily workflow uses OAuth when the repository secret
`WAKATIME_ACCESS_TOKEN` is set. Until then, it continues using the existing
`athul/waka-readme` action and `WAKATIME_API_KEY`. WakaTime's announced API key
read cutoff is **2026-11-01**; complete the migration before that date.
IDE activity uploads still use the existing API key.

## Authorization and initial setup

1. Create or use an app at [WakaTime Apps](https://wakatime.com/apps).
   Register the redirect URI used for authorization and token exchange; the
   official example uses `https://wakatime.com/oauth/test`.
2. Use the authorization **code** flow (`response_type=code`), request only
   `read_stats.languages`, and generate and verify a random OAuth `state`.
   Exchange the returned code securely at `https://wakatime.com/oauth/token`
   with `grant_type=authorization_code`, `client_id`, `client_secret`,
   `redirect_uri`, and `code`.
3. Put only the returned access token in the repository Actions secret
   `WAKATIME_ACCESS_TOKEN`. Keep the client secret and refresh token in a
   secure store outside the repository and runner. Never paste credentials
   into code, commit messages, shell arguments, logs, or chat.
4. After merging this change into the default branch and configuring the secret, manually run
   **Waka Readme** once and inspect the resulting README change. Verify that
   the OAuth response supplies the date range, masked total and languages.
   The script fails without editing README if required fields are missing.

The authorization, token exchange, secret configuration, publishing and
Actions run are operator steps; preparing this change does not perform them.
Both scheduled and manual runs update the default-branch README, matching
the previous action, regardless of the branch or tag selected for dispatch.

## Expiration and renewal

According to [WakaTime's authentication documentation](https://wakatime.com/developers#authentication),
code-flow tokens expire after **365 days**. Implicit-flow tokens
(`response_type=token`) expire after **12 hours**, so avoid that flow for this
daily job. Record the issued token's expiry and renew before it expires.
Revocation can invalidate a token sooner.

Renew using a server-side POST to `https://wakatime.com/oauth/token` with
`grant_type=refresh_token`, `client_id`, `client_secret`, the same
`redirect_uri`, and the stored `refresh_token`. Securely replace
`WAKATIME_ACCESS_TOKEN` with the returned access token and retain any new
refresh token returned by the provider. Alternatively, repeat the code flow
if the refresh token is unavailable or invalid.

This minimal implementation **does not automatically refresh tokens** or
write GitHub secrets. It avoids adding client-secret storage and a separate
GitHub credential capable of changing secrets. A 401 fails the job with a
renewal message and leaves the last successful README intact. An OAuth error
does not silently fall back to the deprecated API key.

## Preserved display and access scope

The only request is `GET /api/v1/users/current/stats/all_time`, authenticated
with an `Authorization: Bearer ...` header. `read_stats.languages` is the
documented granular scope for language stats. No heartbeat, project, editor,
email, or write scope is requested.

| Existing display | Response fields used |
| --- | --- |
| Date range (`SHOW_TITLE`) | `start`, `end` |
| Total including Other (`SHOW_MASKED_TIME`) | `human_readable_total_including_other_language` |
| Language and time (`SHOW_TIME`) | `languages[].name`, `languages[].text` |
| Graph and percentage | `languages[].percent` |
| 10-entry boundary, ignoring Other | Original ordered language list; same upstream boundary behavior |

The `txt` fence, 25-cell `░▒▓█` bars, alignment and percentage precision match
[the checked upstream version](https://github.com/athul/waka-readme/blob/370590ad8da8f54282ed92aa42b7c25ecbd5a9a6/main.py).
Totals and percentages are not recalculated after hiding Other. Content
outside the WakaTime markers is preserved. Transient errors and pending or
stale all-time calculations get at most three attempts, ten seconds apart.
API response bodies and exception details are never printed.

## Local tests

Run `python3 -m unittest discover -s tests -v` (Python 3.9 or newer).
Tests use synthetic data and credentials. The golden fixture was generated
with the checked upstream formatter and the existing workflow settings.
No WakaTime or GitHub request is made by these tests.
