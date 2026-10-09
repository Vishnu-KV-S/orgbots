# Security policy

This project runs AI agents that browse real websites with real sign-ins, so security
reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/Vishnu-KV-S/orgbots/security/advisories/new),
or by email to vishnukv@stockeds.com.

Include what you found, how to reproduce it, and what an attacker could do with it.
You can expect an acknowledgement within a few days.

## In scope

- Prompt injection that makes a bot act outside its authority or leak a credential
- Bypassing an approval, a budget, the kill switch or the network allowlist
- Reading another team's bots, files, secrets or audit log
- Credentials reaching logs, prompts, model output or the database in plain text
