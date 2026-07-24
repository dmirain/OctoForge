# Security Policy

## Supported versions

OctoForge is pre-1.0 and moves fast. Security fixes land on `main` — there's no separate LTS branch to backport to.

## Reporting a vulnerability

Please don't open a public issue for a security problem. Use GitHub's private vulnerability reporting instead: go to the **Security** tab of this repository and click **Report a vulnerability**. That opens a private draft advisory visible only to the maintainer, so the issue can be discussed and fixed before it's public.

If that option isn't available for some reason, open a regular issue asking to be pointed to an alternative private channel — don't post details of the vulnerability itself.

## What's worth reporting

Anything that lets one user reach another user's data or credentials, bypass the `SsrfGuard` on outbound HTTP calls, execute arbitrary code through a tool argument, or leak secrets (`OF_LLM_API_KEY`, `OF_TELEGRAM_BOT_TOKEN`, etc.) is in scope. There's no bug bounty — just a thank-you and a fix.
